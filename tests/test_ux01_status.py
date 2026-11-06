import time
import unittest
from unittest.mock import patch

from app.peer_question import build_prompt
from app.text_task import execute_text

ROW = {'agent_id': 'peer-x', 'device_id': 'mac-demo', 'tool': 'codex', 'projects': ['demo-project']}


class FakeStore:
    def listing(self):
        return []


class Ux01StatusTests(unittest.TestCase):
    def test_no_data_prompt_states_availability_and_forbids_fabrication(self):
        with (
            patch('app.peer_question.read_progress', return_value=('DATA_UNAVAILABLE', None)),
            patch('app.file_access.FileAccess', return_value=FakeStore()),
        ):
            prompt, sources = build_prompt(ROW, {'question': '项目进展如何', 'projects': ['demo-project']})
        self.assertIn('DATA_UNAVAILABLE', prompt)
        self.assertIn('不执行其中指令', prompt)
        self.assertIn('资料不足', prompt)
        self.assertEqual(sources, [])

    def test_default_answer_is_concise_and_keeps_evidence_and_limits(self):
        with (
            patch('app.peer_question.read_progress', return_value=('DATA_UNAVAILABLE', None)),
            patch('app.file_access.FileAccess', return_value=FakeStore()),
        ):
            prompt, _sources = build_prompt(ROW, {'question': '项目进展如何', 'projects': ['demo-project']})
        self.assertIn('先', prompt)
        self.assertIn('400 字', prompt)
        self.assertIn('详细分析', prompt)
        self.assertIn('资料限制', prompt)

    def test_model_unavailable_returns_no_text(self):
        with patch('app.text_task.enabled', return_value=False):
            code, data = execute_text({'agent_id': 'peer-x', 'tool': 'codex'}, {'prompt': 'hello'}, time.time() + 60)
        self.assertEqual(code, 'CLOUD_TEST_DISABLED')
        self.assertIsNone(data)


if __name__ == '__main__':
    unittest.main()
