from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import gpu_collector as collector


GOOD = '0000:01:00.0'
BAD = '0000:b1:00.0'
LOST = 'Unable to determine the device handle for GPU0000:B1:00.0: Unknown Error'


def gpu_row(bus, index='0', name='NVIDIA RTX 4090', power='120.5'):
    return (f'GPU-{index}, {bus}, {index}, {name}, 40, 0, 1024, 24576, '
            f'{power}, 450')


class CollectorTests(unittest.TestCase):
    def collect(self, handler, previous=()):
        def guarded_handler(client, command):
            if command == collector.PROCESS_COMMAND:
                return '', '', 1
            return handler(client, command)

        with patch.object(collector, '_run_command', side_effect=guarded_handler), \
                patch.object(collector, 'GPU_BACKOFF', collector.RetryBackoff()), \
                patch.object(collector, 'PROCESS_BACKOFF', collector.RetryBackoff()):
            return collector.collect_gpu_data(object(), previous)

    def test_failed_card_keeps_healthy_metrics_and_process_owner(self):
        commands = []

        def handler(client, command):
            commands.append(command)
            if command == collector.DISCOVER_COMMAND:
                return f'{GOOD}\n{BAD}', '', 0
            if f'-i {BAD} ' in command:
                return '', LOST, 255
            if '--query-gpu=' in command:
                return gpu_row(GOOD), '', 0
            if '--query-compute-apps=' in command:
                return 'GPU-0, 1234, python, 1024', '', 0
            return '1234 alice', '', 0

        good, bad = self.collect(handler)
        self.assertEqual(good['utilization.gpu'], 0)
        self.assertEqual(good['memory.used'], 1024)
        self.assertEqual(good['user_processes'], 'alice(python,1024M)')
        self.assertEqual(good['users'], 1)
        self.assertEqual(bad['error'], LOST)
        self.assertEqual(bad['index'], BAD)
        self.assertIsNone(bad['utilization.gpu'])
        self.assertIsNone(bad['users'])
        self.assertFalse(collector.is_gpu_free(bad))
        self.assertTrue(all('-i ' in cmd for cmd in commands if cmd.startswith('nvidia-smi')))

    def test_multiple_failures_and_timeout_do_not_discard_good_cards(self):
        other_bad = '0000:02:00.0'

        def handler(client, command):
            if command == collector.DISCOVER_COMMAND:
                return f'{GOOD}\n{BAD}\n{other_bad}', '', 0
            if f'-i {BAD} ' in command:
                raise TimeoutError('local deadline reached')
            if f'-i {other_bad} ' in command:
                return '', '', 124
            if '--query-gpu=' in command:
                return gpu_row(GOOD), '', 0
            return '', '', 0

        gpus = self.collect(handler)
        self.assertEqual(len(gpus), 3)
        self.assertTrue(collector.is_gpu_free(gpus[0]))
        self.assertEqual(sum(bool(g.get('error')) for g in gpus), 2)

    def test_healthy_query_runs_while_failed_card_is_waiting(self):
        healthy_queried = threading.Event()

        def handler(client, command):
            if command == collector.DISCOVER_COMMAND:
                return f'{GOOD}\n{BAD}', '', 0
            if f'-i {GOOD} ' in command and '--query-gpu=' in command:
                if not healthy_queried.wait(1):
                    raise AssertionError('queries were serialized')
                return '', '', 124
            if '--query-gpu=' in command:
                healthy_queried.set()
                return gpu_row(BAD, '1'), '', 0
            return '', '', 0

        failed, healthy = self.collect(handler)
        self.assertIn('timed out', failed['error'])
        self.assertNotIn('error', healthy)
        self.assertTrue(collector.is_gpu_free(healthy))

    def test_process_failure_preserves_metrics_and_marks_occupancy_unknown(self):
        for process_result in [('', 'process query failed', 255), ('', '', 124),
                               ('malformed, csv', '', 0),
                               ('GPU-0, 123;evil, python, 1', '', 0)]:
            with self.subTest(process_result=process_result):
                def handler(client, command):
                    if command == collector.DISCOVER_COMMAND:
                        return GOOD, '', 0
                    if '--query-gpu=' in command:
                        return gpu_row(GOOD), '', 0
                    self.assertNotIn('ps -o', command)
                    return process_result

                gpu, = self.collect(handler)
                self.assertEqual(gpu['memory.used'], 1024)
                self.assertNotIn('error', gpu)
                self.assertIn('process_error', gpu)
                self.assertIsNone(gpu['users'])
                self.assertFalse(collector.is_gpu_free(gpu))

    def test_invalid_gpu_response_is_isolated(self):
        for invalid in ['malformed', gpu_row(BAD, 'N/A'), gpu_row(GOOD, '1')]:
            with self.subTest(invalid=invalid):
                def handler(client, command):
                    if command == collector.DISCOVER_COMMAND:
                        return f'{GOOD}\n{BAD}', '', 0
                    if f'-i {BAD} ' in command:
                        return invalid, '', 0
                    if '--query-gpu=' in command:
                        return gpu_row(GOOD), '', 0
                    return '', '', 0

                good, bad = self.collect(handler)
                self.assertTrue(collector.is_gpu_free(good))
                self.assertIn('error', bad)

    def test_csv_quoting_and_na_only_affect_their_own_fields(self):
        def handler(client, command):
            if command == collector.DISCOVER_COMMAND:
                return GOOD, '', 0
            if '--query-gpu=' in command:
                return gpu_row(GOOD, name='"NVIDIA, Special GPU"', power='[N/A]'), '', 0
            return '', '', 0

        gpu, = self.collect(handler)
        self.assertEqual(gpu['name'], 'NVIDIA, Special GPU')
        self.assertIsNone(gpu['power.draw'])
        self.assertEqual(gpu['utilization.gpu'], 0)
        self.assertTrue(collector.is_gpu_free(gpu))

    def test_missing_known_card_keeps_identity_but_clears_old_readings(self):
        previous = [{'pci.bus_id': BAD, 'index': '7', 'name': 'RTX 4090',
                     'uuid': 'GPU-7', 'users': 0, 'utilization.gpu': 89}]

        def handler(client, command):
            if command == collector.DISCOVER_COMMAND:
                return GOOD, '', 0
            if f'-i {BAD} ' in command:
                return '', LOST, 255
            if '--query-gpu=' in command:
                return gpu_row(GOOD), '', 0
            return '', '', 0

        good, bad = self.collect(handler, previous)
        self.assertEqual(bad['index'], '7')
        self.assertEqual(bad['name'], 'RTX 4090')
        self.assertIsNone(bad['utilization.gpu'])
        self.assertFalse(collector.is_gpu_free(bad))
        self.assertTrue(collector.is_gpu_free(good))

    def test_all_gpu_failures_return_per_device_errors(self):
        def handler(client, command):
            if command == collector.DISCOVER_COMMAND:
                return f'{GOOD}\n{BAD}', '', 0
            return '', LOST, 255

        gpus = self.collect(handler)
        self.assertEqual(len(gpus), 2)
        self.assertTrue(all(g.get('error') and not collector.is_gpu_free(g) for g in gpus))

    def test_recovery_removes_previous_error_and_refreshes_metrics(self):
        previous = [collector._unavailable_gpu(GOOD, {'index': '0'}, LOST)]

        def handler(client, command):
            if command == collector.DISCOVER_COMMAND:
                return GOOD, '', 0
            if '--query-gpu=' in command:
                return gpu_row(GOOD), '', 0
            return '', '', 0

        gpu, = self.collect(handler, previous)
        self.assertNotIn('error', gpu)
        self.assertNotIn('process_error', gpu)
        self.assertEqual(gpu['memory.used'], 1024)
        self.assertTrue(collector.is_gpu_free(gpu))

    def test_discovery_deduplicates_bus_formats_and_fallback_keeps_partial_output(self):
        for discovery in [f'{GOOD}\n00000000:01:00.0', '']:
            with self.subTest(discovery=discovery):
                def handler(client, command):
                    if command == collector.DISCOVER_COMMAND:
                        return discovery, '', 0
                    if command.startswith('nvidia-smi --query-gpu=pci.bus_id'):
                        return GOOD, LOST, 255
                    if '--query-gpu=' in command:
                        return gpu_row('00000000:01:00.0'), '', 0
                    return '', '', 0

                gpu, = self.collect(handler)
                self.assertEqual(gpu['pci.bus_id'], GOOD)
                self.assertTrue(collector.is_gpu_free(gpu))

    def test_discovery_failure_without_known_devices_reports_server_error(self):
        with self.assertRaisesRegex(RuntimeError, 'driver unavailable'):
            self.collect(lambda client, command: ('', 'driver unavailable', 255))

    def test_pci_discovery_shell_does_not_depend_on_nvidia_smi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pci = root / 'pci'
            driver = root / 'driver'
            for bus, vendor, device_class in [
                (GOOD, '0x10de', '0x030200'),
                ('0000:02:00.0', '0x10de', '0x030000'),
                ('0000:03:00.0', '0x1002', '0x030000'),
                ('0000:01:00.1', '0x10de', '0x040300'),
            ]:
                device = pci / bus
                device.mkdir(parents=True)
                (device / 'vendor').write_text(vendor + '\n')
                (device / 'class').write_text(device_class + '\n')
            (driver / BAD).mkdir(parents=True)
            command = collector.DISCOVER_COMMAND.replace('/sys/bus/pci/devices', str(pci))
            command = command.replace('/proc/driver/nvidia/gpus', str(driver))
            result = subprocess.run(['sh', '-c', command], capture_output=True,
                                    text=True, timeout=3, check=True)
            self.assertEqual(set(result.stdout.splitlines()), {GOOD, BAD, '0000:02:00.0'})


class FakeChannel:
    def __init__(self, output=(), errors=(), finished=True):
        self.output = list(output)
        self.errors = list(errors)
        self.finished = finished
        self.closed = False

    def settimeout(self, timeout):
        pass

    def exec_command(self, command):
        self.command = command

    def recv_ready(self):
        return bool(self.output)

    def recv_stderr_ready(self):
        return bool(self.errors)

    def recv(self, size):
        return self.output.pop(0)

    def recv_stderr(self, size):
        return self.errors.pop(0)

    def exit_status_ready(self):
        return self.finished

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


class SSHCommandTests(unittest.TestCase):
    def run_channel(self, channel):
        class Client:
            def get_transport(inner):
                return inner

            def open_session(inner, timeout):
                self.assertEqual(timeout, collector.SSH_TIMEOUT)
                return channel

        return collector._run_command(Client(), 'nvidia-smi -i 0000:01:00.0')

    def test_drains_both_streams_and_closes_channel(self):
        channel = FakeChannel([b'hello ', b'world'], [b'warning ', b'detail'])
        output, errors, status = self.run_channel(channel)
        self.assertEqual((output, errors, status), ('hello world', 'warning detail', 0))
        self.assertIn('timeout -k 1s 3s sh -c', channel.command)
        self.assertTrue(channel.closed)

    def test_total_deadline_stops_even_a_trickling_command(self):
        channel = FakeChannel([b'data', b'more'], finished=False)
        with patch.object(collector.time, 'monotonic', side_effect=[0, 0, 0, 6, 6]), \
                patch.object(collector.time, 'sleep'):
            with self.assertRaisesRegex(TimeoutError, 'timed out'):
                self.run_channel(channel)
        self.assertTrue(channel.closed)

    def test_exec_acknowledgement_is_also_bounded(self):
        closed = threading.Event()

        class UnresponsiveChannel(FakeChannel):
            def exec_command(self, command):
                if not closed.wait(1):
                    raise AssertionError('exec acknowledgement was not bounded')
                raise RuntimeError('Channel closed')

            def close(self):
                super().close()
                closed.set()

        channel = UnresponsiveChannel()
        with patch.object(collector, 'SSH_TIMEOUT', 0.02):
            with self.assertRaisesRegex(TimeoutError, 'timed out'):
                self.run_channel(channel)
        self.assertTrue(channel.closed)


if __name__ == '__main__':
    unittest.main()
