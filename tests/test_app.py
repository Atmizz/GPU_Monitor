import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


# Load the web app without macOS menu-bar side effects or real node credentials.
root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gpu_monitor_test_app', root / 'app.py')
monitor = importlib.util.module_from_spec(spec)
original_cwd = os.getcwd()
try:
    with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'rumps': None}):
        os.chdir(directory)
        spec.loader.exec_module(monitor)
finally:
    os.chdir(original_cwd)


class AppTests(unittest.TestCase):
    def setUp(self):
        backoff = patch.object(monitor, 'NODE_BACKOFF', monitor.RetryBackoff())
        backoff.start()
        self.addCleanup(backoff.stop)

    def test_partial_failure_remains_a_successful_node_and_api_response(self):
        previous = [{'pci.bus_id': '0000:b1:00.0', 'index': '1'}]
        gpus = [
            {'index': '0', 'users': 0, 'utilization.gpu': 25},
            {'index': '1', 'users': None, 'utilization.gpu': None, 'error': 'GPU lost'},
        ]
        with patch.object(monitor, 'GLOBAL_GPU_STATS', [{'hostname': 'test-host', 'gpus': previous}]), \
                patch.object(monitor, 'get_ssh_client', return_value='fake-ssh'), \
                patch.object(monitor, 'collect_gpu_data', return_value=gpus) as collect:
            entry = monitor.fetch_single_server_data({'hostname': 'test-host', 'alias': 'Lab'})
            collect.assert_called_once_with('fake-ssh', previous, source=('test-host', '22', ''))
        self.assertNotIn('error', entry)
        self.assertEqual(entry['alias'], 'Lab')
        with patch.object(monitor, 'GLOBAL_GPU_STATS', [entry]):
            response = monitor.app.test_client().get('/api/gpustat/all')
            self.assertEqual(response.status_code, 200)
            data = response.get_json()[0]
            self.assertEqual(data['gpus'][0]['utilization.gpu'], 25)
            self.assertEqual(data['gpus'][1]['error'], 'GPU lost')
            self.assertIn('1/1 台在线', monitor.format_panel_stats())
            self.assertIn('平均 25%', monitor.format_panel_stats())

    def test_ssh_connection_failure_is_still_a_node_error(self):
        with patch.object(monitor, 'get_ssh_client', side_effect=RuntimeError('SSH unavailable')):
            entry = monitor.fetch_single_server_data({'hostname': 'test-host'})
        self.assertEqual(entry['error'], 'SSH unavailable')
        self.assertEqual(entry['gpus'], [])

    def test_unavailable_metrics_are_not_reported_as_zero_utilization(self):
        entry = {'hostname': 'test-host', 'gpus': [
            {'index': '0', 'utilization.gpu': None, 'error': 'GPU unavailable'}
        ]}
        with patch.object(monitor, 'GLOBAL_GPU_STATS', [entry]):
            self.assertIn('平均 -', monitor.format_panel_stats())

    def test_repeated_polling_does_not_repeat_failed_ssh_connections(self):
        now = [0]
        gate = monitor.RetryBackoff(clock=lambda: now[0])
        with patch.object(monitor, 'NODE_BACKOFF', gate), \
                patch.object(monitor, 'get_ssh_client', side_effect=RuntimeError('SSH banner timeout')) as connect:
            for _ in range(10000):
                monitor.fetch_single_server_data({'hostname': 'test-host'})
            self.assertEqual(connect.call_count, 1)
            now[0] = 30
            monitor.fetch_single_server_data({'hostname': 'test-host'})
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(gate.pending(('test-host', '22', ''))[0], 60)

    def test_failed_client_is_removed_and_closed(self):
        client = MagicMock()
        with patch.object(monitor, 'SSH_CLIENTS', {'test-host': client}), \
                patch.object(monitor, 'get_ssh_client', return_value=client), \
                patch.object(monitor, 'collect_gpu_data', side_effect=RuntimeError('query failed')):
            entry = monitor.fetch_single_server_data({'hostname': 'test-host'})
            self.assertNotIn('test-host', monitor.SSH_CLIENTS)
            self.assertEqual(entry['error'], 'query failed')
        client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
