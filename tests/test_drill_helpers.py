"""Guard the drill-side parsing that other tests cannot see.

The router names its dispatch-lease holder ``<hostname>-<pid>-<token>``. A drill that
parsed the wrong segment silently killed nothing and then reported the *takeover* as
missing, which reads like a product failure. These cases pin the parser to the naming
that app/router.py actually produces, and demand an explicit None (a loud failure) for
anything else.
"""
import unittest
import os
from unittest.mock import patch

from tests.router_failover_drill import holder_pid
from tests.real_model_soak import offline_in, presence_map, process_sample


class HolderPidTests(unittest.TestCase):
    def test_current_naming(self):
        self.assertEqual(holder_pid('demo-892499-ace99d8d'), 892499)

    def test_hostname_with_dashes(self):
        self.assertEqual(holder_pid('mac-mini-4711-deadbeef'), 4711)

    def test_matches_router_naming(self):
        # Bind the parser to the producer instead of to a copied literal.
        import os
        import socket
        from app.router import lease_holder_name
        holder = lease_holder_name(pid=os.getpid(), token='deadbeef')
        self.assertEqual(holder, f'{socket.gethostname()}-{os.getpid()}-deadbeef')
        self.assertEqual(holder_pid(holder), os.getpid())

    def test_malformed_names_are_not_guessed(self):
        for bad in ('demo-892499', 'router', 'demo--abcdef12', 'demo-abc-def', '', None, 12):
            self.assertIsNone(holder_pid(bad), bad)

    def test_legacy_two_segment_name_is_rejected(self):
        # The pre-fencing '<host>-<pid>' shape has no token; guessing it as a PID would
        # hide a naming regression, so it must stay unparsed.
        self.assertIsNone(holder_pid('demo-892499'))


class SoakHeartbeatTests(unittest.TestCase):
    """The soak heartbeat parser: a failed listing is not a device going offline."""

    def test_presence_map_reads_the_listing(self):
        objs = [{'code': 'OK', 'data': {'agents': [{'agent_id': 'a', 'presence': 'online'},
                                                   {'agent_id': 'b', 'presence': 'offline'}]}}]
        self.assertEqual(presence_map(objs), {'a': 'online', 'b': 'offline'})

    def test_presence_map_rejects_an_unusable_listing(self):
        for objs in ([], [{'code': 'QUERY_FAILED'}], [{'code': 'OK', 'data': {}}], ['nope']):
            self.assertIsNone(presence_map(objs), objs)

    def test_offline_and_unknown_are_distinct(self):
        self.assertEqual(offline_in({'a': 'online', 'b': 'offline'}, ['a', 'b']), (['b'], []))
        self.assertEqual(offline_in({'a': 'online'}, ['a', 'b']), ([], ['b']))
        self.assertEqual(offline_in(None, ['a']), ([], ['a']))   # listing failed, not an outage

    def test_explicit_router_pid_does_not_sample_another_router(self):
        with patch('tests.real_model_soak.subprocess.run') as search:
            rss,cpu=process_sample('app.router',os.getpid())
            search.assert_not_called()
            self.assertIsNotNone(rss);self.assertIsNotNone(cpu)


if __name__ == '__main__':
    unittest.main()
