import asyncio,unittest,json
from unittest.mock import AsyncMock
from types import SimpleNamespace
import nats
from tests.helpers.client import load_client_worker
class Tests(unittest.IsolatedAsyncioTestCase):
 async def test_publish_error_does_not_kill_heartbeat_and_cancellation_stops_it(self):
  w=load_client_worker();seen=asyncio.Event();attempts=[]
  async def publish(subject,raw):
   attempts.append(json.loads(raw))
   if len(attempts)==1:raise nats.errors.ConnectionClosedError()
   seen.set()
  nc=SimpleNamespace(is_connected=True,publish=publish);task=asyncio.create_task(w.publish_heartbeat(nc,'heartbeat',lambda:{'enabled':False},interval=.001))
  await asyncio.wait_for(seen.wait(),1);self.assertFalse(task.done());task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertGreaterEqual(len(attempts),2)
 async def test_disconnected_client_is_not_published_as_online(self):
  w=load_client_worker();nc=SimpleNamespace(is_connected=False,publish=AsyncMock());task=asyncio.create_task(w.publish_heartbeat(nc,'heartbeat',lambda:{},interval=.001));await asyncio.sleep(.005);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  nc.publish.assert_not_called()
