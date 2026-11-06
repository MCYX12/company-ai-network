import json
from pathlib import Path
import tempfile
import unittest

from scripts import audit_chain


class AuditAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'channel.jsonl'
        self.path.write_text('\n'.join(json.dumps({'event': e, 'seq': i}) for i, e in enumerate(['a', 'b', 'c'])) + '\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_and_check_anchor(self):
        audit_chain.seal(self.path)
        anchor = Path(self.tmp.name) / 'anchor.json'
        exported = audit_chain.export_anchor(self.path, anchor)
        self.assertTrue(anchor.exists())
        self.assertTrue(audit_chain.check_anchor(self.path, anchor)['ok'])
        self.assertEqual(audit_chain.verify(self.path)['head'], exported['head'])

    def test_check_anchor_detects_tampering(self):
        audit_chain.seal(self.path)
        anchor = Path(self.tmp.name) / 'anchor.json'
        audit_chain.export_anchor(self.path, anchor)
        lines = self.path.read_text().splitlines()
        lines[1] = json.dumps({'event': 'tampered', 'seq': 1})
        self.path.write_text('\n'.join(lines) + '\n')
        self.assertFalse(audit_chain.check_anchor(self.path, anchor)['ok'])


if __name__ == '__main__':
    unittest.main()
