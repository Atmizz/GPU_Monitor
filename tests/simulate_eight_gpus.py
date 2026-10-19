"""Exercise the production collector/API against a local fake SSH transport.

No sockets, real credentials, GPU tools, or remote machines are used. Only SSH
channel responses are simulated; collection, parsing, timeouts, backoff, and
the Flask API run their production code. Additional failure modes are injected
explicitly and must not be inferred from the reference screenshot.

Run: ./venv/bin/python tests/simulate_eight_gpus.py --output reports/eight_gpu_simulation
"""

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import importlib.util
import json
import logging
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gpu_collector as collector
from collection_safety import RetryBackoff


BUSES = [f'0000:{number:02x}:00.0' for number in range(1, 8)] + ['0000:1b:00.0']
BAD_BUS = BUSES[-1]
ERROR = 'Unable to determine the device handle for GPU0000:1B:00.0: Unknown Error'
HOST = 'simulated-eight-gpu-node'


def load_app():
    """Load routes without reading workspace credentials or starting monitoring."""
    spec = importlib.util.spec_from_file_location('eight_gpu_simulated_app', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    original_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'rumps': None}):
            os.chdir(directory)
            spec.loader.exec_module(module)
    finally:
        os.chdir(original_cwd)
    return module


@dataclass
class Clock:
    now: float = 0


@dataclass
class Response:
    output: str = ''
    errors: str = ''
    status: int = 0
    never_finishes: bool = False
    block_close: bool = False


class FakeChannel:
    def __init__(self, node):
        self.node = node
        self.closed = False
        self.response = Response()
        self.output = b''
        self.errors = b''

    def settimeout(self, timeout):
        self.timeout = timeout

    def exec_command(self, wrapped):
        args = shlex.split(wrapped)
        assert args[:4] == ['timeout', '-k', '1s', '3s'], args
        assert args[4:6] == ['sh', '-c'], args
        self.response = self.node.respond(args[6])
        self.output = self.response.output.encode()
        self.errors = self.response.errors.encode()

    def recv_ready(self):
        return bool(self.output)

    def recv_stderr_ready(self):
        return bool(self.errors)

    def recv(self, size):
        value, self.output = self.output[:size], self.output[size:]
        return value

    def recv_stderr(self, size):
        value, self.errors = self.errors[:size], self.errors[size:]
        return value

    def exit_status_ready(self):
        return self.closed or not self.response.never_finishes

    def recv_exit_status(self):
        return -1 if self.closed and self.response.never_finishes else self.response.status

    def close(self):
        if self.response.block_close and not self.closed:
            self.node.close_entered.set()
            # The test itself has a safety release; no test thread is left stuck.
            if not self.node.release_close.wait(3):
                raise RuntimeError('Simulation safety deadline released blocked close')
        with self.node.lock:
            if not self.closed:
                self.closed = True
                self.node.closed_channels += 1
                self.node.active_channels.remove(self)


class FakeNode:
    def __init__(self, clock, mode='error'):
        self.clock = clock
        self.mode = mode
        self.visible_buses = list(BUSES)
        self.process_override = None
        self.remote_query_alive = False
        self.block_close = False
        self.commands = []
        self.lock = threading.RLock()
        self.opened_channels = 0
        self.closed_channels = 0
        self.active_channels = set()
        self.max_active_channels = 0
        self.healthy_finished_at = None
        self.healthy_finished = threading.Event()
        self.owners_queried = threading.Event()
        self.close_entered = threading.Event()
        self.release_close = threading.Event()

    def get_transport(self):
        return self

    def is_active(self):
        return True

    def open_session(self, timeout):
        with self.lock:
            channel = FakeChannel(self)
            self.opened_channels += 1
            self.active_channels.add(channel)
            self.max_active_channels = max(self.max_active_channels, len(self.active_channels))
            return channel

    def close(self):
        for channel in list(self.active_channels):
            channel.close()

    def respond(self, command):
        with self.lock:
            record = {'second': self.clock.now, 'command': command}
            self.commands.append(record)
            if command == collector.PROCESS_COMMAND:
                record['kind'] = 'inspect_processes'
                output = self.process_override
                if output is None:
                    output = (f'D 60 nvidia-smi -i {BAD_BUS} --query-gpu=name'
                              if self.remote_query_alive else '')
                return Response(output=output, status=0 if output else 1)
            if command == collector.DISCOVER_COMMAND:
                record['kind'] = 'discover_pci'
                return Response(output='\n'.join(self.visible_buses))
            if command.startswith('ps -o pid=,user= -p '):
                record['kind'] = 'process_owners'
                self.owners_queried.set()
                return Response(output='\n'.join(f'{1000 + index} user{index}' for index in range(3)))
            if command.startswith('nvidia-smi --query-gpu=pci.bus_id '):
                record['kind'] = 'global_enumeration'
                # Deliberately return no usable address, matching the fallback failure case.
                return Response(errors=ERROR, status=255)
            args = shlex.split(command)
            assert args[0] == 'nvidia-smi' and args[1] == '-i', command
            bus = args[2]
            index = BUSES.index(bus)
            metrics = any(arg.startswith('--query-gpu=') for arg in args)
            record.update(kind='metrics' if metrics else 'compute_processes', bus=bus)
            if self.mode == 'all_error' or (bus == BAD_BUS and self.mode == 'error'):
                record['failed'] = True
                return Response(errors=ERROR, status=255,
                                block_close=self.block_close and bus == BAD_BUS)
            if bus == BAD_BUS and self.mode == 'hang':
                record['failed'] = True
                self.remote_query_alive = True
                return Response(never_finishes=True)
            if metrics:
                busy = index < 3
                return Response(output=(
                    f'GPU-sim-{index}, {bus}, {index}, Simulated NVIDIA GPU, '
                    f'{45 + index}, {60 + index if busy else 0}, '
                    f'{4096 if busy else 0}, 24576, {180 if busy else 30}, 450'))
            if bus != BAD_BUS:
                completed = {entry['bus'] for entry in self.commands
                             if entry.get('kind') == 'compute_processes'
                             and entry.get('bus') != BAD_BUS}
                if len(completed) == 7 and not self.healthy_finished.is_set():
                    self.healthy_finished_at = time.monotonic()
                    self.healthy_finished.set()
            if index < 3:
                return Response(output=f'GPU-sim-{index}, {1000 + index}, python, 4096')
            return Response()

    def stats(self):
        with self.lock:
            return {
                'command_counts': dict(Counter(record['kind'] for record in self.commands)),
                'channels_opened': self.opened_channels,
                'channels_closed': self.closed_channels,
                'channels_remaining': len(self.active_channels),
                'peak_live_channels': self.max_active_channels,
                'bad_metric_attempt_seconds': [record['second'] for record in self.commands
                    if record.get('kind') == 'metrics' and record.get('bus') == BAD_BUS],
                'simulated_remote_queries_remaining': (
                    len(self.process_override.splitlines()) if self.process_override is not None
                    else int(self.remote_query_alive)),
            }


def previous_node(host=HOST):
    gpus = []
    for index, bus in enumerate(BUSES):
        busy = index < 3
        gpus.append({
            'uuid': f'GPU-sim-{index}', 'pci.bus_id': bus, 'index': str(index),
            'name': 'Simulated NVIDIA GPU', 'temperature.gpu': 35,
            'utilization.gpu': 10 + index if busy else 0,
            'memory.used': 4096 if busy else 0, 'memory.total': 24576,
            'memory': 17 if busy else 0, 'power.draw': 30, 'enforced.power.limit': 450,
            'users': 1 if busy else 0,
            'user_processes': f'user{index}(python,4096M)' if busy else '',
        })
    return {'hostname': host, 'alias': 'Simulated node', 'gpus': gpus}


def summary(entry):
    gpus = entry.get('gpus', [])
    return {
        'node_error': entry.get('error'), 'gpu_count': len(gpus),
        'readable_gpus': sum(not gpu.get('error') for gpu in gpus),
        'unavailable_gpus': sum(bool(gpu.get('error')) for gpu in gpus),
        'free_gpus': sum(collector.is_gpu_free(gpu) for gpu in gpus),
    }


@contextmanager
def environment(app, nodes, clock, previous=(), ssh_timeout=None):
    with ExitStack() as stack:
        changes = {
            'SERVERS': [{'hostname': host} for host in nodes],
            'GLOBAL_GPU_STATS': list(previous), 'CACHE_UPDATED_AT': 0,
            'LAST_CLIENT_REQUEST': 0, 'SSH_CLIENTS': {}, 'WAKE_EVENT': threading.Event(),
            'NODE_BACKOFF': RetryBackoff(clock=lambda: clock.now),
            'get_ssh_client': lambda details: nodes[details['hostname']],
        }
        for name, value in changes.items():
            stack.enter_context(patch.object(app, name, value))
        for name in ('GPU_BACKOFF', 'PROCESS_BACKOFF'):
            stack.enter_context(patch.object(collector, name, RetryBackoff(clock=lambda: clock.now)))
        if ssh_timeout is not None:
            stack.enter_context(patch.object(collector, 'SSH_TIMEOUT', ssh_timeout))
        yield


def publish(app, host=HOST):
    entry = app.fetch_single_server_data({'hostname': host})
    app.GLOBAL_GPU_STATS = [entry]
    app.CACHE_UPDATED_AT = time.time()
    return entry


def read_api(app):
    response = app.app.test_client().get('/api/gpustat/all')
    assert response.status_code == 200
    return response.get_json()


def unknown_error_and_recovery(app):
    clock = Clock()
    node = FakeNode(clock)
    with environment(app, {HOST: node}, clock, [previous_node()]):
        first = publish(app)
        assert summary(first) == dict(node_error=None, gpu_count=8, readable_gpus=7,
                                     unavailable_gpus=1, free_gpus=4)
        first_api = read_api(app)[0]
        bad = next(gpu for gpu in first_api['gpus'] if gpu['pci.bus_id'] == BAD_BUS)
        assert bad['index'] == '7' and bad['error'] == ERROR
        assert all(bad[field] is None for field in (
            'users', 'temperature.gpu', 'utilization.gpu', 'memory.used', 'memory.total',
            'memory', 'power.draw', 'enforced.power.limit'))
        for second in range(1, 601):
            clock.now = second
            entry = publish(app)
            assert summary(entry) == summary(first)
        before_recovery = node.stats()
        assert before_recovery['bad_metric_attempt_seconds'] == [0, 30, 90, 210, 450]
        assert before_recovery['channels_remaining'] == 0
        assert before_recovery['channels_opened'] == before_recovery['channels_closed']
        assert before_recovery['peak_live_channels'] <= collector.GPU_WORKERS
        assert before_recovery['command_counts'].get('global_enumeration', 0) == 0
        assert before_recovery['command_counts']['compute_processes'] == 7 * 601
        node.mode = 'healthy'
        clock.now = 750
        recovered = publish(app)
        assert summary(recovered) == dict(node_error=None, gpu_count=8, readable_gpus=8,
                                         unavailable_gpus=0, free_gpus=5)
        return {'first_api': first_api, 'during_failure': before_recovery,
                'recovered': summary(recovered), 'after_recovery': node.stats()}


def cold_start(app):
    clock = Clock()
    node = FakeNode(clock)
    with environment(app, {HOST: node}, clock):
        entry = publish(app)
        bad = next(gpu for gpu in entry['gpus'] if gpu.get('error'))
        assert bad['index'] == BAD_BUS
        assert summary(entry)['readable_gpus'] == 7
        return {'summary': summary(entry), 'failed_card_index': bad['index'], **node.stats()}


def timed_out_with_residual_process(app):
    clock = Clock()
    node = FakeNode(clock, mode='hang')
    with environment(app, {HOST: node}, clock, [previous_node()]):
        started = time.monotonic()
        entry = publish(app)
        elapsed = time.monotonic() - started
        assert summary(entry)['readable_gpus'] == 7
        bad = next(gpu for gpu in entry['gpus'] if gpu.get('error'))
        assert 'timed out' in bad['error']
        assert node.healthy_finished_at is not None
        assert node.healthy_finished_at - started < elapsed
        first_duration = elapsed
        # Let backoff expire repeatedly; D-state detection must still prevent probes.
        for second in range(60, 361, 60):
            clock.now = second
            entry = publish(app)
            assert summary(entry)['readable_gpus'] == 7
            assert 'no new probe' in next(gpu for gpu in entry['gpus'] if gpu.get('error'))['error']
        # Model monitor restart: discard local cache and all in-memory backoff.
        app.GLOBAL_GPU_STATS = []
        collector.GPU_BACKOFF = RetryBackoff(clock=lambda: clock.now)
        collector.PROCESS_BACKOFF = RetryBackoff(clock=lambda: clock.now)
        app.NODE_BACKOFF = RetryBackoff(clock=lambda: clock.now)
        restarted = publish(app)
        stats = node.stats()
        assert stats['bad_metric_attempt_seconds'] == [0]
        assert stats['channels_remaining'] == 0
        assert stats['simulated_remote_queries_remaining'] == 1
        return {'first_round_seconds': round(first_duration, 3),
                'configured_ssh_timeout_seconds': collector.SSH_TIMEOUT,
                'summary_after_restart': summary(restarted), **stats}


def unidentified_residual_process(app):
    clock = Clock()
    node = FakeNode(clock)
    node.process_override = 'D 120 nvidia-smi'
    with environment(app, {HOST: node}, clock, [previous_node()]):
        entry = publish(app)
        assert entry['error'] and entry['gpus'] == []
        assert node.stats()['command_counts'] == {'inspect_processes': 1}
        return {'api': read_api(app)[0], **node.stats()}


def fallback_enumeration_failure(app):
    clock = Clock()
    node = FakeNode(clock)
    node.visible_buses = []
    with environment(app, {HOST: node}, clock):
        for second in range(601):
            clock.now = second
            entry = publish(app)
            assert entry.get('error') and not entry['gpus']
        stats = node.stats()
        enumeration_seconds = [entry['second'] for entry in node.commands
                               if entry['kind'] == 'global_enumeration']
        assert enumeration_seconds == [0, 30, 90, 210, 450]
        assert stats['channels_remaining'] == 0
        return {'summary': summary(entry), 'enumeration_seconds': enumeration_seconds, **stats}


def disappearing_card_after_disconnect(app):
    clock = Clock()
    node = FakeNode(clock)
    with environment(app, {HOST: node}, clock, [previous_node()]):
        # A node-level error replaces the cache, losing the known device inventory.
        with patch.object(app, 'get_ssh_client', side_effect=RuntimeError('simulated SSH disconnect')):
            failed = publish(app)
        assert failed['gpus'] == []
        node.visible_buses = BUSES[:-1]
        clock.now = 30
        recovered = publish(app)
        assert not recovered.get('error') and len(recovered['gpus']) == 7
        assert BAD_BUS not in [gpu['pci.bus_id'] for gpu in recovered['gpus']]
        assert node.stats()['bad_metric_attempt_seconds'] == []
        return {'after_disconnect': summary(failed), 'after_reconnect': summary(recovered),
                'bad_card_still_listed': False, **node.stats()}


def all_targeted_queries_fail(app):
    clock = Clock()
    node = FakeNode(clock, mode='all_error')
    with environment(app, {HOST: node}, clock, [previous_node()]):
        entry = publish(app)
        assert summary(entry) == dict(node_error=None, gpu_count=8, readable_gpus=0,
                                     unavailable_gpus=8, free_gpus=0)
        return {'summary': summary(entry), **node.stats()}


def blocked_channel_close(app):
    """Inject a bounded close hang, observe actual monitor-loop/API behavior."""
    class StopLoop(BaseException):
        pass

    class OneRoundWakeEvent:
        def set(self):
            pass

        def wait(self, timeout):
            raise StopLoop()

        def clear(self):
            pass

    clock = Clock()
    faulty = FakeNode(clock)
    faulty.block_close = True
    healthy = FakeNode(clock, mode='healthy')
    other_host = 'simulated-healthy-node'
    nodes = {HOST: faulty, other_host: healthy}
    old_cache = [previous_node(HOST), previous_node(other_host)]
    finished = threading.Event()
    errors = []

    def run_one_round():
        try:
            app.background_monitor_loop()
        except StopLoop:
            pass
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            finished.set()

    # Accelerate only this injected SSH-close scenario; the timeout scenario
    # above uses the unmodified production 5-second deadline.
    with environment(app, nodes, clock, old_cache, ssh_timeout=0.05), \
            patch.object(app, 'WAKE_EVENT', OneRoundWakeEvent()):
        worker = threading.Thread(target=run_one_round, daemon=True)
        worker.start()
        try:
            assert faulty.close_entered.wait(1), 'fault was not injected'
            assert faulty.healthy_finished.wait(1), 'healthy GPUs did not finish'
            assert healthy.owners_queried.wait(1), 'healthy node did not finish its queries'
            assert not finished.wait(0.25), 'unexpectedly recovered before close was released'
            snapshots = [read_api(app) for _ in range(3)]
            assert all(snapshot == old_cache for snapshot in snapshots)
            assert app.CACHE_UPDATED_AT == 0
            blocked_stats = faulty.stats()
            assert blocked_stats['channels_remaining'] == 1
            assert blocked_stats['bad_metric_attempt_seconds'] == [0]
            assert healthy.stats()['command_counts']['inspect_processes'] == 1
            observation = {
                'injected_ssh_timeout_seconds': 0.05,
                'observed_blocked_for_at_least_seconds': 0.25,
                'api_http_status': 200,
                'api_reads_of_unchanged_cache': len(snapshots),
                'cache_timestamp_unchanged': True,
                'healthy_node_old_utilization_returned': snapshots[0][1]['gpus'][0]['utilization.gpu'],
                'next_monitor_round_started': False,
                'faulty_node_while_blocked': blocked_stats,
            }
        finally:
            faulty.release_close.set()
            worker.join(2)
        assert not worker.is_alive() and not errors, errors
        assert app.CACHE_UPDATED_AT > 0
        assert app.GLOBAL_GPU_STATS[1]['gpus'][0]['utilization.gpu'] == 60
        observation['healthy_node_utilization_after_release'] = 60
        observation['channels_remaining_after_release'] = len(faulty.active_channels)
        return observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/eight_gpu_simulation')
    args = parser.parse_args()
    app = load_app()
    scenarios = [unknown_error_and_recovery, cold_start, timed_out_with_residual_process,
                 unidentified_residual_process, fallback_enumeration_failure,
                 disappearing_card_after_disconnect, all_targeted_queries_fail, blocked_channel_close]
    results = {
        'reference': {'bus': BAD_BUS, 'error': ERROR,
                      'exit_status_note': '255 is injected; the screenshot does not show an exit status.',
                      'scope': 'Local fake SSH channels; real collector, Flask routes and monitor loop. '
                               'Not a real NVIDIA driver, kernel D-state or SSH server test.'},
    }
    logging.disable(logging.CRITICAL)
    try:
        for scenario in scenarios:
            started = time.monotonic()
            results[scenario.__name__] = scenario(app)
            print(f'PASS {scenario.__name__} ({time.monotonic() - started:.3f}s)', flush=True)
    finally:
        logging.disable(logging.NOTSET)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'frontend_fixture.json').write_text(json.dumps(
        results['unknown_error_and_recovery']['first_api'], ensure_ascii=False, indent=2) + '\n')
    print(f'Results: {args.output.resolve() / "results.json"}')


if __name__ == '__main__':
    main()
