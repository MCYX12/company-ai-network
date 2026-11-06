import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from app.project_records import read_progress

class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patch=patch('app.project_records.RUNTIME',self.root);self.patch.start()
        self.row={'agent_id':'test','projects':['demo-project']}
        self.folder=self.root/'project-records';self.folder.mkdir();self.file=self.folder/'demo-project.json'
        self.record={'schema_version':1,'project_id':'demo-project','source_id':'published/test','updated_at':datetime.now(timezone.utc).isoformat(),'summary':'verified facts','completed':['test'],'blockers':[],'evidence':['record-1']}
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def read(self):return read_progress(self.row,{'project_id':'demo-project'})
    def write(self):self.file.write_text(json.dumps(self.record))
    def test_missing_is_unknown(self):self.assertEqual(self.read()[0],'DATA_UNAVAILABLE')
    def test_record_has_provenance_without_invented_percentage(self):
        self.write();code,data=self.read();self.assertEqual(code,'OK');self.assertIsNone(data['progress']);self.assertEqual(data['source_id'],'published/test');self.assertFalse(data['synthetic'])
    def test_stale_is_visible(self):
        self.record['updated_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();self.write();self.assertEqual(self.read()[1]['freshness'],'stale')
    def test_cross_project_and_path_traversal(self):
        for p in ['private-project','../demo-project','/etc/passwd']:
            self.assertEqual(read_progress(self.row,{'project_id':p})[0],'FORBIDDEN')
    def test_unknown_fields_rejected(self):
        self.record['secret']='should never return';self.write();self.assertEqual(self.read(),('DATA_INVALID',None))
    def test_symlinks_rejected(self):
        other=self.root/'other.json';other.write_text(json.dumps(self.record));self.file.symlink_to(other);self.assertEqual(self.read()[0],'DATA_INVALID')
    def test_bad_timestamp_and_oversize(self):
        self.record['updated_at']='2026-01-01';self.write();self.assertEqual(self.read()[0],'DATA_INVALID')
        self.file.write_text(' '*16385);self.assertEqual(self.read()[0],'DATA_INVALID')
    def test_hardlinked_record_is_rejected(self):
        other=self.root/'other.json';other.write_text(json.dumps(self.record));os.link(other,self.file)
        self.assertEqual(self.read()[0],'DATA_INVALID')
if __name__=='__main__':unittest.main()
