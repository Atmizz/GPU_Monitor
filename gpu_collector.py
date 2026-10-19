"""Agentless GPU collection with a separate failure boundary for every device."""

import csv
import logging
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collection_safety import RetryBackoff


GPU_FIELDS = (
    'uuid', 'pci.bus_id', 'index', 'name', 'temperature.gpu', 'utilization.gpu',
    'memory.used', 'memory.total', 'power.draw', 'power.limit',
)
PROC_FIELDS = ('gpu_uuid', 'pid', 'process_name', 'used_gpu_memory')
REMOTE_TIMEOUT = 3
SSH_TIMEOUT = 5
GPU_WORKERS = 4
MAX_SMI_PROCESSES = 32
GPU_BACKOFF = RetryBackoff()
PROCESS_BACKOFF = RetryBackoff()
logger = logging.getLogger(__name__)
PROCESS_COMMAND = 'LC_ALL=C ps -C nvidia-smi -o stat=,etimes=,args='

# Discover through Linux PCI/driver directories, without asking NVML to enumerate
# every device. A broken GPU handle must not prevent discovery of healthy GPUs.
DISCOVER_COMMAND = r'''
for device in /sys/bus/pci/devices/*; do
    [ -r "$device/vendor" ] && [ -r "$device/class" ] || continue
    read -r gpu_vendor < "$device/vendor"
    [ "$gpu_vendor" = "0x10de" ] || continue
    read -r gpu_class < "$device/class"
    case "$gpu_class" in
        0x03*) printf '%s\n' "${device##*/}" ;;
    esac
done
for device in /proc/driver/nvidia/gpus/*; do
    [ -d "$device" ] && printf '%s\n' "${device##*/}"
done
:
'''


def _parse_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_bus(value):
    match = re.fullmatch(r'([0-9a-f]{4,8}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])',
                         str(value).strip(), re.IGNORECASE)
    if not match:
        return None
    domain, bus, device, function = match.groups()
    return f'{int(domain, 16):04x}:{bus.lower()}:{device.lower()}.{function}'


def _run_command(client, command):
    """Bound remote execution and local reads; drain stdout/stderr together.

    A timeout bounds waiting and sends signals; it does not prove that a remote
    process blocked in the driver has exited. Inspect processes before retrying.
    """
    deadline = time.monotonic() + SSH_TIMEOUT
    wrapped = f'timeout -k 1s {REMOTE_TIMEOUT}s sh -c {shlex.quote(command)}'
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError('SSH connection unavailable')
    channel = transport.open_session(timeout=SSH_TIMEOUT)
    # Paramiko's channel timeout does not bound waiting for the server's exec
    # acknowledgement. Closing this channel at the deadline also releases that
    # wait, without closing other GPUs' channels on the shared transport.
    watchdog = threading.Timer(max(0, deadline - time.monotonic()), channel.close)
    watchdog.daemon = True
    output, errors = [], []
    try:
        channel.settimeout(SSH_TIMEOUT)
        watchdog.start()
        channel.exec_command(wrapped)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Query timed out after {SSH_TIMEOUT}s')
            if channel.recv_ready():
                output.append(channel.recv(65536))
            if channel.recv_stderr_ready():
                errors.append(channel.recv_stderr(65536))
            if (channel.exit_status_ready() and not channel.recv_ready()
                    and not channel.recv_stderr_ready()):
                return (b''.join(output).decode('utf-8', errors='replace').strip(),
                        b''.join(errors).decode('utf-8', errors='replace').strip(),
                        channel.recv_exit_status())
            time.sleep(0.01)
    except Exception as exc:
        if time.monotonic() >= deadline:
            raise TimeoutError(f'Query timed out after {SSH_TIMEOUT}s') from exc
        raise
    finally:
        watchdog.cancel()
        channel.close()


def _failure(output, errors, status):
    if status in (124, 137):
        return f'Query timed out after {REMOTE_TIMEOUT}s'
    detail = (errors or output).strip()[:500]
    return detail or f'Query failed (exit {status})'


def _inspect_query_processes(client, previous_gpus):
    """Never spawn another GPU probe over an existing blocked/overdue probe.

    This is checked on every collection, including after monitor restarts. Use
    ps rather than NVML: the check must not enter the damaged GPU driver itself.
    Fail closed when process inspection is unavailable or a target is unknown.
    """
    output, errors, status = _run_command(client, PROCESS_COMMAND)
    if status == 1 and not output and not errors:
        return set()  # procps returns 1 when no matching processes exist.
    if status:
        raise RuntimeError(f'GPU process inspection failed: {_failure(output, errors, status)}')
    lines = output.splitlines()
    if len(lines) >= MAX_SMI_PROCESSES:
        raise RuntimeError(f'GPU collection paused: {len(lines)} existing nvidia-smi processes '
                           f'(limit {MAX_SMI_PROCESSES}); no new GPU probes started')
    aliases = {}
    for gpu in previous_gpus:
        bus = _normalize_bus(gpu.get('pci.bus_id'))
        if bus:
            for alias in (gpu.get('index'), gpu.get('uuid'), bus):
                if alias is not None:
                    aliases[str(alias)] = bus
    blocked = set()
    for line in lines:
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[1].isdigit():
            raise RuntimeError('GPU process inspection returned invalid output; collection paused')
        state, age, command = parts
        if not state.startswith(('D', 'Z')) and int(age) < SSH_TIMEOUT:
            continue
        try:
            args = shlex.split(command)
        except ValueError as exc:
            raise RuntimeError('Cannot identify an existing GPU query; collection paused') from exc
        # Explicit sampling loops are expected to be long lived. A loop in D/Z
        # state is still unsafe, as are overdue ordinary one-shot queries.
        continuous = any(arg in ('-l', '--loop', '-lms', '--loop-ms', 'dmon', 'pmon', 'daemon')
                         or arg.startswith(('--loop=', '--loop-ms=')) for arg in args[1:])
        if continuous and not state.startswith(('D', 'Z')):
            continue
        target = None
        for index, arg in enumerate(args[1:], start=1):
            if arg in ('-i', '--id') and index + 1 < len(args):
                target = args[index + 1]
                break
            if arg.startswith(('--id=', '-i=')):
                target = arg.split('=', 1)[1]
                break
        if target is None:
            raise RuntimeError('GPU collection paused: an existing blocked/overdue nvidia-smi '
                               'query has no identifiable GPU target')
        for identifier in target.split(','):
            bus = _normalize_bus(identifier) or aliases.get(identifier)
            if bus is None:
                raise RuntimeError('GPU collection paused: cannot map an existing '
                                   'blocked/overdue nvidia-smi query to a PCI device')
            blocked.add(bus)
    return blocked


def _discover_gpus(client, previous_gpus, blocked_buses=()):
    previous = {}
    for gpu in previous_gpus:
        bus = _normalize_bus(gpu.get('pci.bus_id'))
        if bus:
            previous[bus] = gpu
    buses = set(previous) | set(blocked_buses)
    detail = 'No NVIDIA GPUs found'
    try:
        output, errors, status = _run_command(client, DISCOVER_COMMAND)
        discovered = {bus for line in output.splitlines()
                      if (bus := _normalize_bus(line))}
        buses.update(discovered)
        if not discovered and not buses:
            # Fallback for systems without Linux PCI directories. Keep partial
            # valid output even when nvidia-smi also reports a failed device.
            output, errors, status = _run_command(
                client, 'nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader,nounits')
            buses.update(bus for line in output.splitlines()
                         if (bus := _normalize_bus(line)))
        if status:
            detail = _failure(output, errors, status)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
    if not buses:
        raise RuntimeError(detail)
    return sorted(buses), previous


def _unavailable_gpu(bus, previous, error):
    # Retain identity, never stale utilization/process readings. Unknown devices
    # use their PCI address until a successful query supplies the real index.
    return {
        'uuid': previous.get('uuid'), 'pci.bus_id': bus,
        'index': previous.get('index', bus),
        'name': previous.get('name') or f'GPU {bus}',
        'temperature.gpu': None, 'utilization.gpu': None,
        'memory.used': None, 'memory.total': None, 'memory': None,
        'power.draw': None, 'enforced.power.limit': None,
        'users': None, 'user_processes': '', 'error': error,
    }


def _query_gpu(client, bus, previous, source):
    key = (source, bus)
    remaining, last_error = GPU_BACKOFF.pending(key)
    if remaining:
        return _unavailable_gpu(bus, previous,
                                f'Collection backoff ({remaining:.0f}s remaining): {last_error}'), []
    target = shlex.quote(bus)
    try:
        output, errors, status = _run_command(
            client, f'nvidia-smi -i {target} --query-gpu={",".join(GPU_FIELDS)} '
                    '--format=csv,noheader,nounits')
        if status:
            raise RuntimeError(_failure(output, errors, status))
        rows = list(csv.reader(output.splitlines(), skipinitialspace=True))
        if len(rows) != 1 or len(rows[0]) != len(GPU_FIELDS):
            raise ValueError(f'Invalid GPU response: {output[:300]}')
        raw = dict(zip(GPU_FIELDS, (value.strip() for value in rows[0])))
        if (not raw['index'].isdigit() or not raw['uuid'].startswith('GPU-')
                or _normalize_bus(raw['pci.bus_id']) != bus):
            raise ValueError(f'Invalid GPU identity: {output[:300]}')
        mem_used, mem_total = _parse_int(raw['memory.used']), _parse_int(raw['memory.total'])
        gpu = {
            'uuid': raw['uuid'], 'pci.bus_id': bus, 'index': raw['index'], 'name': raw['name'],
            'temperature.gpu': _parse_int(raw['temperature.gpu']),
            'utilization.gpu': _parse_int(raw['utilization.gpu']),
            'memory.used': mem_used, 'memory.total': mem_total,
            'memory': round(mem_used / mem_total * 100)
                      if mem_total and mem_used is not None else None,
            'power.draw': _parse_int(raw['power.draw']),
            'enforced.power.limit': _parse_int(raw['power.limit']),
            'users': None, 'user_processes': '',
        }
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        delay = GPU_BACKOFF.failure(key, error)
        logger.warning('GPU %s collection failed; retry in %ss: %s', bus, delay, error)
        return _unavailable_gpu(bus, previous, error), []
    GPU_BACKOFF.success(key)

    # A process query may fail independently of the metrics query. Retain the
    # metrics, but never interpret missing process information as an idle GPU.
    remaining, last_error = PROCESS_BACKOFF.pending(key)
    if remaining:
        gpu['process_error'] = f'Process query backoff ({remaining:.0f}s remaining): {last_error}'
        return gpu, []
    try:
        output, errors, status = _run_command(
            client, f'nvidia-smi -i {target} --query-compute-apps={",".join(PROC_FIELDS)} '
                    '--format=csv,noheader,nounits')
        if status:
            raise RuntimeError(_failure(output, errors, status))
        processes = []
        for row in csv.reader(output.splitlines(), skipinitialspace=True):
            if not row:
                continue
            if len(row) != len(PROC_FIELDS):
                raise ValueError(f'Invalid process response: {output[:300]}')
            proc = dict(zip(PROC_FIELDS, (value.strip() for value in row)))
            if not proc['pid'].isdigit() or proc['gpu_uuid'] != gpu['uuid']:
                raise ValueError(f'Invalid process identity: {output[:300]}')
            processes.append(proc)
        gpu['users'] = len(processes)
        PROCESS_BACKOFF.success(key)
        return gpu, processes
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        delay = PROCESS_BACKOFF.failure(key, error)
        logger.warning('GPU %s process query failed; retry in %ss: %s', bus, delay, error)
        gpu['process_error'] = error
        return gpu, []


def collect_gpu_data(client, previous_gpus=(), source=None):
    source = source if source is not None else id(client)
    blocked = _inspect_query_processes(client, previous_gpus)
    buses, previous = _discover_gpus(client, previous_gpus, blocked)

    def query(bus):
        if bus in blocked:
            return _unavailable_gpu(bus, previous.get(bus, {}),
                                    'Collection paused: an existing nvidia-smi process is '
                                    'blocked or overdue; no new probe started'), []
        return _query_gpu(client, bus, previous.get(bus, {}), source)
    # Separate SSH channels allow healthy queries to finish while another GPU
    # hangs. Bound concurrency to avoid flooding the driver or SSH server.
    with ThreadPoolExecutor(max_workers=min(GPU_WORKERS, len(buses))) as executor:
        results = list(executor.map(query, buses))

    pids = sorted({proc['pid'] for _, processes in results for proc in processes}, key=int)
    pid_to_user = {}
    if pids:
        try:
            output, _, _ = _run_command(client, f'ps -o pid=,user= -p {",".join(pids)}')
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit():
                    pid_to_user[parts[0]] = parts[1]
        except Exception:
            pass  # Occupancy is known even when the process owner is unknown.

    for gpu, processes in results:
        descriptions = []
        for proc in processes:
            owner = pid_to_user.get(proc['pid'], 'unknown')
            memory = _parse_int(proc['used_gpu_memory'])
            name = proc['process_name'].replace(' ', '')
            descriptions.append(f'{owner}({name},{memory if memory is not None else "?"}M)')
        gpu['user_processes'] = ' '.join(descriptions)
    return [gpu for gpu, _ in results]


def is_gpu_free(gpu):
    return not gpu.get('error') and not gpu.get('process_error') and gpu.get('users') == 0
