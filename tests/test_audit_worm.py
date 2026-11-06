"""Chained append-only WORM anchors detect a rewritten audit prefix."""
import tempfile
import unittest
from pathlib import Path

from scripts import audit_chain


class WormTests(unittest.TestCase):
    def test_rewriting_an_anchored_prefix_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            audit = folder / 'audit.jsonl'
            audit.write_text('{"t": 1, "e": "A"}\n{"t": 2, "e": "B"}\n')
            worm = folder / 'worm'
            audit_chain.export_worm(audit, worm)
            self.assertTrue(audit_chain.check_worm(audit, worm)['ok'])
            # A legitimate extension stays valid.
            audit.write_text(audit.read_text() + '{"t": 3, "e": "C"}\n')
            self.assertTrue(audit_chain.check_worm(audit, worm)['ok'])
            # Rewriting an already-anchored line is detected, even with a replaced anchor file.
            audit.write_text(audit.read_text().replace('"A"', '"X"'))
            result = audit_chain.check_worm(audit, worm)
            self.assertFalse(result['ok'])
            self.assertIn('rewritten', ' '.join(result['problems']))

    def test_appending_a_new_anchor_does_not_launder_an_earlier_rewrite(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            audit = folder / 'audit.jsonl'
            audit.write_text('{"t": 1, "e": "A"}\n{"t": 2, "e": "B"}\n')
            worm = folder / 'worm'
            audit_chain.export_worm(audit, worm)
            audit.write_text(audit.read_text().replace('"A"', '"X"'))   # rewrite the anchored prefix
            audit_chain.export_worm(audit, worm)                        # append a fresh, valid anchor
            result = audit_chain.check_worm(audit, worm)
            self.assertFalse(result['ok'])
            self.assertIn('rewritten', ' '.join(result['problems']))

    def test_replacing_a_worm_entry_breaks_the_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            audit = folder / 'audit.jsonl'
            audit.write_text('{"t": 1}\n')
            worm = folder / 'worm'
            audit_chain.export_worm(audit, worm)
            log = audit_chain.worm_log(worm)
            lines = log.read_text().splitlines()
            log.write_text('{"seq": 1, "prev": "' + '0' * 64 + '", "file": "/nope", "head": "x",'
                           ' "records": 1}\n' + '\n'.join(lines))
            self.assertFalse(audit_chain.check_worm(audit, worm)['ok'])


if __name__ == '__main__':
    unittest.main()
