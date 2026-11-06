import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app.directory_requests import DirectoryRequests,RootRejected,validate_root,canonical_root,case_candidates
from app.directory_context import validate_grants

class WindowsDirectoryRootTests(unittest.TestCase):
    def test_project_path_round_trip_and_permission_binding(self):
        root=r'C:\Users\Fei\Documents\中文项目'
        self.assertEqual(validate_root(root),root)
        with tempfile.TemporaryDirectory() as tmp:
            store=DirectoryRequests(Path(tmp)/'requests.sqlite3')
            r=store.request('caller','windows',root)
            self.assertEqual(r['state'],'pending')
            store.decide(r['id'],'approved',60,'isolated fixture','fixture-admin')
            grants=store.dispatch('caller','windows',time.time()+20)
            self.assertEqual(grants[0]['root'],root)
            self.assertEqual(validate_grants({'agent_id':'windows'},{'caller':'caller'},grants)[0],grants)
            self.assertEqual(store.dispatch('other','windows',time.time()+20),[])
            store.decide(r['id'],'revoked',60,'isolated fixture','fixture-admin')
            self.assertFalse(store.permits_ids('caller','windows',[r['id']]))

    def test_windows_unsafe_forms_are_refused(self):
        cases={'C:\\':'TOO_SHORT',r'C:\Users\Fei':'TOO_BROAD',r'C:\Windows\System32':'TOO_BROAD',
               r'C:Users\Fei\project':'NOT_ABSOLUTE',r'C:\Users\Fei\project\..\secret':'PARENT_TRAVERSAL',
               r'C:\Users\Fei\.ssh':'HIDDEN_COMPONENT',r'C:\Users\Fei\project:stream':'WINDOWS_UNSAFE',
               r'C:\Projects\NUL.txt':'WINDOWS_UNSAFE',r'C:\Projects\COM1':'WINDOWS_UNSAFE',
               'C:\\Projects\\test ':'WINDOWS_UNSAFE',r'C:\Projects\test.':'WINDOWS_UNSAFE',
               r'\\server\share\project':'WINDOWS_UNSAFE',r'\\?\C:\Users\Fei\project':'WINDOWS_UNSAFE',
               'C:/Users/Fei/project':'NOT_NORMALIZED', 'C:\\Users\\Fei\\project\\':'NOT_NORMALIZED'}
        for root,code in cases.items():
            with self.subTest(root=root), self.assertRaises(RootRejected) as caught:validate_root(root)
            self.assertEqual(caught.exception.code,code)

    def test_router_never_probes_windows_path_on_linux(self):
        root=r'C:\Users\Fei\Documents\project'
        with patch('app.directory_requests.os.listdir',side_effect=AssertionError('remote path probe')):
            self.assertEqual(canonical_root(root),(root,False))
            self.assertEqual(case_candidates(root),[])
