import contextlib
import io
import json
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from app import peer_client


class PeerCliStdinTests(unittest.TestCase):
    def test_reply_timeout_after_connect_keeps_response_phase(self):
        args=SimpleNamespace(identity='ag-a',doctor=False,wait=False,access_status=False,request_access=False,
                             list=False,task_id=None,project_progress=None,status=True,target='windows-peer')
        nc=SimpleNamespace(close=AsyncMock())
        with patch.object(peer_client,'connect',AsyncMock(return_value=nc)),patch.object(peer_client,'query',AsyncMock(side_effect=TimeoutError())):
            with self.assertRaises(peer_client.ResponseTimeout):asyncio.run(peer_client.run(args))
        nc.close.assert_awaited_once()
    def test_response_timeout_is_not_reported_as_tls_connection_failure(self):
        async def fail(_):raise peer_client.ResponseTimeout('No response for get_status')
        with patch('sys.argv',['client','--status','--target','windows-peer']),patch.object(peer_client,'run',fail):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):peer_client.main()
            r=json.loads(output.getvalue());self.assertIn('已建立 TLS',r['data']['hint']);self.assertNotIn('没能连上',r['data']['hint'])
    def test_status_is_read_only_without_stdin(self):
        async def capture(args):return {'status':args.status,'target':args.target}
        with patch('sys.argv',['client','--status','--target','windows-peer']),patch.object(peer_client,'run',capture),patch('sys.stdin') as stdin:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):peer_client.main()
            self.assertEqual(json.loads(output.getvalue()),{'status':True,'target':'windows-peer'});stdin.read.assert_not_called()
    def test_project_progress_is_read_only_without_stdin(self):
        async def capture(args):
            return {'project':args.project_progress,'target':args.target}
        with patch('sys.argv',['client','--project-progress','demo-project','--target','windows-demo']),patch.object(peer_client,'run',capture):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):peer_client.main()
            self.assertEqual(json.loads(output.getvalue()),{'project':'demo-project','target':'windows-demo'})

    def test_plain_and_windows_bom_input_preserve_question(self):
        async def capture(args):
            return {'question': args.question}
        for data in ['中文问题\n', '\ufeff中文问题\n']:
            with self.subTest(data=data), patch('sys.argv', ['client', '--question-stdin']), \
                 patch('sys.stdin', io.StringIO(data)), patch.object(peer_client, 'run', capture):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):peer_client.main()
                self.assertEqual(json.loads(output.getvalue())['question'], '中文问题\n')
