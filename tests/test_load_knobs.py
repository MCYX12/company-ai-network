import json
import os
import time
import unittest
from unittest.mock import patch

from app.adapters import execute
from tests.load_capacity import percentile

ROW = {'agent_id': 'ag-a', 'tool': 'fixture', 'projects': ['demo-project']}


class LoadKnobTests(unittest.TestCase):
    def test_response_padding_reaches_about_one_kib(self):
        with patch.dict(os.environ, {'POC_FIXTURE_RESPONSE_BYTES': '1024'}):
            code, data = execute(ROW, 'get_project_progress', {})
        self.assertEqual(code, 'OK')
        self.assertGreaterEqual(len(json.dumps(data, ensure_ascii=False).encode()), 1000)

    def test_default_response_has_no_padding(self):
        code, data = execute(ROW, 'get_status', {})
        self.assertEqual(code, 'OK')
        self.assertNotIn('padding', data)

    def test_synthetic_model_delay_is_honoured(self):
        with patch.dict(os.environ, {'POC_FIXTURE_DELAY_MS': '50'}):
            started = time.perf_counter()
            execute(ROW, 'get_status', {})
            elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.04)

    def test_capacity_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertIsNone(percentile([], 0.5))


if __name__ == '__main__':
    unittest.main()
