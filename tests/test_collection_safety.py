import unittest
from unittest.mock import patch

from collection_safety import RetryBackoff
import gpu_collector as collector


GOOD = '0000:01:00.0'
BAD = '0000:b1:00.0'
GPU_ROW = f'GPU-0, {GOOD}, 0, RTX 4090, 40, 0, 0, 24576, 120, 450'


class BackoffTests(unittest.TestCase):
    def test_failure_delays_increase_and_success_resets_them(self):
        now = [0]
        gate = RetryBackoff(clock=lambda: now[0])
        self.assertEqual(gate.pending('node'), (0, ''))
        for expected in (30, 60, 120, 240, 300, 300):
            self.assertEqual(gate.failure('node', 'SSH failed'), expected)
            self.assertEqual(gate.pending('node'), (expected, 'SSH failed'))
            self.assertEqual(gate.pending('other-node'), (0, ''))
            now[0] += expected
            self.assertEqual(gate.pending('node')[0], 0)
        gate.success('node')
        self.assertEqual(gate.failure('node', 'failed again'), 30)


class ProcessGuardTests(unittest.TestCase):
    def run_collect(self, processes, previous=()):
        commands = []

        def handler(client, command):
            commands.append(command)
            if command == collector.PROCESS_COMMAND:
                return processes, '', 0
            if command == collector.DISCOVER_COMMAND:
                return f'{GOOD}\n{BAD}', '', 0
            if '--query-gpu=' in command:
                return GPU_ROW, '', 0
            return '', '', 0

        with patch.object(collector, '_run_command', side_effect=handler), \
                patch.object(collector, 'GPU_BACKOFF', RetryBackoff()), \
                patch.object(collector, 'PROCESS_BACKOFF', RetryBackoff()):
            return collector.collect_gpu_data(object(), previous, source='test'), commands

    def test_d_state_query_is_never_reissued_and_healthy_card_still_works(self):
        processes = f'D 10000 /usr/bin/nvidia-smi -i {BAD} --query-gpu=name'
        gpus, commands = self.run_collect(processes)
        self.assertEqual(gpus[0]['pci.bus_id'], GOOD)
        self.assertNotIn('error', gpus[0])
        self.assertIn('no new probe', gpus[1]['error'])
        self.assertFalse(any(f'-i {BAD}' in cmd for cmd in commands))

    def test_guard_also_works_after_monitor_restart_without_cached_identity(self):
        for state in ('D', 'Z', 'S'):
            with self.subTest(state=state):
                gpus, commands = self.run_collect(f'{state} 60 nvidia-smi --id={BAD}')
                self.assertIn('error', gpus[1])
                self.assertFalse(any(f'-i {BAD}' in cmd for cmd in commands))

    def test_gpu_index_and_uuid_are_mapped_to_previous_pci_address(self):
        previous = [{'pci.bus_id': BAD, 'index': '7', 'uuid': 'GPU-bad'}]
        for target in ('7', 'GPU-bad'):
            with self.subTest(target=target):
                gpus, _ = self.run_collect(f'D 50 nvidia-smi -i {target}', previous)
                self.assertIn('error', gpus[1])

    def test_unknown_target_blocks_all_gpu_probes(self):
        for command in ('nvidia-smi', 'nvidia-smi -i 7'):
            with self.subTest(command=command):
                with patch.object(collector, '_run_command', return_value=(f'D 60 {command}', '', 0)) as run:
                    with self.assertRaisesRegex(RuntimeError, 'collection.*paused'):
                        collector.collect_gpu_data(object())
                    self.assertEqual(run.call_count, 1)

    def test_large_existing_process_count_stops_before_inventory_or_gpu_queries(self):
        processes = '\n'.join(f'D 100 nvidia-smi -i {BAD}' for _ in range(10000))
        with patch.object(collector, '_run_command', return_value=(processes, '', 0)) as run:
            with self.assertRaisesRegex(RuntimeError, '10000 existing nvidia-smi'):
                collector.collect_gpu_data(object())
            self.assertEqual(run.call_count, 1)

    def test_running_sampling_loops_are_allowed_but_blocked_loops_are_not(self):
        with patch.object(collector, '_run_command', return_value=('S 3600 nvidia-smi -l 1', '', 0)):
            self.assertEqual(collector._inspect_query_processes(object(), []), set())
        with patch.object(collector, '_run_command', return_value=(f'D 3600 nvidia-smi -i {BAD} -l 1', '', 0)):
            self.assertEqual(collector._inspect_query_processes(object(), []), {BAD})

    def test_failed_process_inspection_fails_closed(self):
        for result in [('', 'fork failed', 1), ('', '', 124), ('invalid output', '', 0)]:
            with self.subTest(result=result):
                with patch.object(collector, '_run_command', return_value=result) as run:
                    with self.assertRaises(RuntimeError):
                        collector.collect_gpu_data(object())
                    self.assertEqual(run.call_count, 1)

    def test_timeout_is_not_retried_during_backoff_but_other_sources_are_independent(self):
        now = [0]
        gate = RetryBackoff(clock=lambda: now[0])
        with patch.object(collector, 'GPU_BACKOFF', gate), \
                patch.object(collector, '_run_command', return_value=('', '', 124)) as run:
            collector._query_gpu(object(), BAD, {}, 'node-a')
            for _ in range(10000):
                gpu, _ = collector._query_gpu(object(), BAD, {}, 'node-a')
            self.assertEqual(run.call_count, 1)
            self.assertIn('backoff', gpu['error'])
            collector._query_gpu(object(), BAD, {}, 'node-b')
            self.assertEqual(run.call_count, 2)
            now[0] = 30
            collector._query_gpu(object(), BAD, {}, 'node-a')
            self.assertEqual(run.call_count, 3)
            self.assertEqual(gate.pending(('node-a', BAD))[0], 60)

    def test_process_query_backoff_retains_fresh_metrics(self):
        now = [0]
        commands = []

        def handler(client, command):
            commands.append(command)
            if '--query-gpu=' in command:
                return GPU_ROW, '', 0
            return '', '', 124

        with patch.object(collector, 'GPU_BACKOFF', RetryBackoff(clock=lambda: now[0])), \
                patch.object(collector, 'PROCESS_BACKOFF', RetryBackoff(clock=lambda: now[0])), \
                patch.object(collector, '_run_command', side_effect=handler):
            collector._query_gpu(object(), GOOD, {}, 'node')
            gpu, _ = collector._query_gpu(object(), GOOD, {}, 'node')
            self.assertEqual(gpu['utilization.gpu'], 0)
            self.assertIn('backoff', gpu['process_error'])
            self.assertEqual(sum('--query-compute-apps=' in cmd for cmd in commands), 1)
            self.assertEqual(sum('--query-gpu=' in cmd for cmd in commands), 2)
