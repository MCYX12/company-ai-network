"""Web entry follow-up: the chat must follow a recovery child, keep a retrievable id, and
not use the old ~90s fixed wait loop."""
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class WebRecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.app=(ROOT/'web/app.js').read_text()
        self.html=(ROOT/'web/index.html').read_text()

    def test_chat_follows_the_platform_retry_child(self):
        self.assertIn('retry_task_id',self.app)
        self.assertIn('恢复子任务',self.app)

    def test_wait_extends_and_keeps_the_task_retrievable(self):
        self.assertIn('chatWaitSeconds',self.app)
        self.assertIn('companyAgentLastTask',self.app)
        self.assertIn('取回结果',self.app)
        self.assertNotIn('i<45',self.app)

    def test_stop_waiting_is_not_cancel(self):
        self.assertIn('id="chat-stop"',self.html)
        self.assertIn('id="chat-wait"',self.html)
        self.assertIn('未取消任务',self.app)

    def test_wait_timeout_is_not_reported_as_failure(self):
        self.assertIn('等待窗口结束',self.app)
        self.assertIn('未确认失败',self.app)

    def test_history_retrieval_resolves_the_recovery_chain(self):
        self.assertIn('async function resolveChatTask',self.app)
        self.assertIn('await resolveChatTask(id)',self.app)
        self.assertIn('retry_task_id',self.app)

    def test_local_task_record_is_scoped_to_the_identity(self):
        self.assertIn('function chatStoreKey()',self.app)
        self.assertIn("'companyAgentLastTask'+(chatIdentity",self.app)
        self.assertIn('function setChatIdentity',self.app)
        self.assertIn('setChatIdentity(b.account||b.identity)',self.app)


if __name__=='__main__':
    unittest.main()
