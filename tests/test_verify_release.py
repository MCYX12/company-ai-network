import json
from pathlib import Path
import tempfile
import unittest

from scripts import verify_release

ROOT = Path(__file__).resolve().parents[1]


class VerifyReleaseTests(unittest.TestCase):
    def fixture(self, root):
        from scripts.source_integrity import hashes
        import hashlib
        for name in ('BASELINE', 'ACCEPTANCE_MATRIX', 'REMAINING_WORK'):
            file=root/'docs'/(name+'.md');file.parent.mkdir(exist_ok=True);file.write_text('fixture')
        (root/'release').mkdir();(root/'offline-bundle').mkdir()
        (root/'.runtime/bin').mkdir(parents=True)
        binary=root/'.runtime/bin/nats-server';binary.write_bytes(b'fixture')
        metadata={'format':1,'nats_server':{'sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}}
        for name in ('manifest.json','sbom.json'):
            (root/'offline-bundle'/name).write_text(json.dumps(metadata))
        release={'product':'fixture','version':'fixture','baseline':'docs/BASELINE.md',
                 'git_commit':'a'*40,'evidence_levels':['设计'],'source_sha256':hashes(root)}
        (root/'release/RELEASE.json').write_text(json.dumps(release))
        return release

    def test_missing_or_malformed_hashes_block_even_with_matching_git_commit(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);release=self.fixture(root)
            for value in (None, {}, [], {'docs/BASELINE.md':None}, {'../outside':'a'*64}):
                with self.subTest(value=value), patch.object(verify_release,'current_commit',return_value='a'*40):
                    release['source_sha256']=value
                    (root/'release/RELEASE.json').write_text(json.dumps(release))
                    report=verify_release.verify(root)
                    self.assertFalse(report['ok'])
                    self.assertTrue(verify_release.gate_problems(report,root))

    def test_missing_artifact_descriptors_are_never_deferred(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root)
            for name in ('manifest.json','sbom.json'):
                with self.subTest(name=name):
                    file=root/'offline-bundle'/name;content=file.read_text();file.unlink()
                    for installed in (True,False):
                        binary=root/'.runtime/bin/nats-server'
                        if not installed:binary.unlink()
                        report=verify_release.verify(root)
                        self.assertFalse(report['ok'])
                        self.assertIn('missing offline-bundle/'+name,verify_release.gate_problems(report,root))
                    binary.write_bytes(b'fixture');file.write_text(content)

    def test_strict_cli_requires_successful_content_verification(self):
        import contextlib, io
        from unittest.mock import patch
        report={'ok':True,'problems':[],'source_matches_release':True,'source_content_matches':None}
        with patch.object(verify_release,'verify',return_value=report), patch('sys.argv',['verify_release','--strict']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify_release.main(),1)

    def test_malformed_release_is_structured_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'release').mkdir()
            for content in ('not json', '[]', 'null', '{"git_commit": true}'):
                (root/'release/RELEASE.json').write_text(content)
                report=verify_release.verify(root)
                self.assertFalse(report['ok'])
                self.assertTrue(verify_release.gate_problems(report,root))

    def test_dependency_lock_and_offline_build_recipe_are_bound(self):
        from scripts.source_integrity import hashes
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'offline-bundle').mkdir()
            for name in ('uv.lock','requirements.lock','offline-bundle/Dockerfile'):
                file=root/name;file.write_text('before')
            before=hashes(root)
            for name in ('uv.lock','requirements.lock','offline-bundle/Dockerfile'):
                (root/name).write_text('after')
                self.assertNotEqual(before[name],hashes(root)[name])

    def test_missing_binary_does_not_excuse_invalid_artifact_metadata(self):
        from scripts.source_integrity import hashes
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ('BASELINE','ACCEPTANCE_MATRIX','REMAINING_WORK'):
                file=root/'docs'/ (name+'.md');file.parent.mkdir(exist_ok=True);file.write_text('fixture')
            (root/'release').mkdir();(root/'offline-bundle').mkdir()
            (root/'release/RELEASE.json').write_text(json.dumps({'product':'p','version':'v','baseline':'docs/BASELINE.md','git_commit':'a'*40,'evidence_levels':['设计'],'source_sha256':hashes(root)}))
            for name in ('manifest.json','sbom.json'):
                file=root/'offline-bundle'/name
                for content in ('{}','[]','{"format":1,"nats_server":null}', '{"format":1,"nats_server":{"sha256":null}}'):
                    file.write_text(content)
                    report=verify_release.verify(root)
                    hard=verify_release.gate_problems(report,root)
                    self.assertTrue(any('malformed' in x for x in hard),hard)
                # A valid descriptor with an absent binary remains explicitly tolerated in source-only CI.
                file.write_text(json.dumps({'format':1,'nats_server':{'sha256':'a'*64}}))
            self.assertEqual(verify_release.gate_problems(verify_release.verify(root),root),[])
            report=verify_release.verify(root)
            self.assertTrue(report['source_matches_release'])
            self.assertFalse(report['git_metadata_available'])
            # Archive acceptance is bound to actual content; editing it still fails.
            (root/'docs/BASELINE.md').write_text('changed')
            report=verify_release.verify(root)
            self.assertFalse(report['source_matches_release'])
            self.assertFalse(report['source_content_matches'])

    def test_current_repo_release_is_valid(self):
        report = verify_release.verify(ROOT)
        # Docs and manifest exist; the NATS binary check may fail if .runtime/bin is absent.
        self.assertNotIn('missing release/RELEASE.json', report['problems'])
        self.assertNotIn('missing docs/BASELINE.md', report['problems'])
        self.assertTrue(report.get('version'))

    def test_gate_tolerates_missing_binary_but_blocks_malformed_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)                      # no .runtime/bin/nats-server
            report = {'problems': ['offline bundle manifest does not match the NATS binary',
                                   'artifact manifest is malformed'],
                      'source_content_matches': True}
            self.assertEqual(verify_release.gate_problems(report, root),
                             ['artifact manifest is malformed'])

    def test_gate_blocks_when_source_hashes_are_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            report = {'problems': [], 'source_content_matches': None}
            hard = verify_release.gate_problems(report, Path(folder))
            self.assertTrue(any('hashes' in problem for problem in hard))

    def test_source_and_released_commit_are_reported(self):
        report = verify_release.verify(ROOT)
        self.assertIn('released_commit', report)
        self.assertIn('current_commit', report)
        self.assertIn(report.get('source_matches_release'), (True, False))
        self.assertTrue(report.get('release_status'))

    def test_current_repo_passes_the_public_source_gate(self):
        report = verify_release.verify(ROOT)
        self.assertIs(report.get('public_source_ok'), True)
        self.assertEqual([p for p in report['problems'] if 'public source' in p], [])

    def test_tracked_runtime_artifact_fails_the_public_source_gate(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('docs/BASELINE.md', 'docs/ACCEPTANCE_MATRIX.md', 'docs/REMAINING_WORK.md'):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text('x')
            (root / 'release').mkdir()
            (root / 'release/RELEASE.json').write_text(json.dumps({
                'product': 'p', 'version': 'v', 'baseline': 'docs/BASELINE.md',
                'acceptance_matrix': 'docs/ACCEPTANCE_MATRIX.md',
                'remaining_work': 'docs/REMAINING_WORK.md', 'evidence_levels': ['设计']}))
            (root / 'tasks.sqlite3').write_bytes(b'ledger')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', 'tasks.sqlite3'], cwd=root, check=True)
            report = verify_release.verify(root)
            self.assertIs(report['public_source_ok'], False)
            self.assertFalse(report['ok'])
            self.assertTrue(any('public source check failed' in p for p in report['problems']))

    def test_missing_manifest_and_docs_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            report = verify_release.verify(Path(folder))
            self.assertFalse(report['ok'])
            self.assertIn('missing release/RELEASE.json', report['problems'])


if __name__ == '__main__':
    unittest.main()
