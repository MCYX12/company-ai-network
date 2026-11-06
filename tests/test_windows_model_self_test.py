import unittest
from unittest.mock import Mock
from scripts import windows_model_self_test as probe


class ModelProbeTests(unittest.TestCase):
    def test_mismatch_keeps_synthetic_output_and_remains_failure(self):
        row=probe.trial(Mock(return_value=('OK',{'text':'BATCH_OK plus extra'})),{},10)
        self.assertFalse(row['answer_matches'])
        self.assertEqual(row['answer_text'],'BATCH_OK plus extra')
        self.assertTrue(row['answer_sha256'])

    def test_nonzero_and_exception_never_publish_error_body(self):
        rows=[probe.trial(Mock(return_value=('EXECUTION_FAILED',{'stderr_tail':'credential-secret'})),{},10),
              probe.trial(Mock(side_effect=ValueError('credential-secret')),{},10)]
        for row in rows:
            self.assertFalse(row['answer_matches'])
            self.assertNotIn('credential-secret',str(row))

    def test_success_requires_both_code_and_exact_text(self):
        for code,text,expected in [('OK','BATCH_OK',True),('OK','BATCH_OK.',False),
                                   ('EXECUTION_FAILED','BATCH_OK',False),('OK',None,False)]:
            self.assertEqual(probe.trial(Mock(return_value=(code,{'text':text})),{},10)['answer_matches'],expected)

    def test_only_fixed_authorized_material_is_supplied(self):
        run=Mock(return_value=('OK',{'text':'BATCH_OK'}))
        probe.trial(run,{},10)
        self.assertEqual(run.call_count,1)
        self.assertEqual(run.call_args.args[1],probe.PROMPT)
