import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.lan_reverse_relay import command, private_file, private_ipv4


class LanRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / 'key'
        self.hosts = Path(self.temp.name) / 'hosts'
        self.key.write_text('test key'); self.key.chmod(0o600)
        self.hosts.write_text('pinned test host'); self.hosts.chmod(0o600)
        self.config = dict(schema_version=1, windows_host='10.0.0.20',
                           broker_host='192.168.1.10', broker_port=14222,
                           remote_port=24222, key=str(self.key),
                           known_hosts=str(self.hosts), host_key_alias='192.168.1.8')
        self.route = [{'dst': '10.0.0.20', 'dev': 'wlan0', 'prefsrc': '192.168.1.10'}]

    def build(self, config=None, route=None, physical=True):
        with patch('scripts.lan_reverse_relay.Path.exists', return_value=physical):
            return command(config or self.config, route or self.route)

    def test_pinned_key_loopback_and_no_password_or_agent_fallback(self):
        cmd = self.build()
        for value in ('BatchMode=yes', 'PasswordAuthentication=no', 'StrictHostKeyChecking=yes',
                      'IdentityAgent=none', 'ExitOnForwardFailure=yes', 'ServerAliveCountMax=3',
                      '127.0.0.1:24222:192.168.1.10:14222', 'companyagentrelay@10.0.0.20'):
            self.assertIn(value, cmd)
        self.assertEqual(cmd[cmd.index('-B') + 1], 'wlan0')
        self.assertEqual(cmd[cmd.index('-b') + 1], '192.168.1.10')

    def test_vpn_public_loopback_and_cgnat_addresses_rejected(self):
        for value in ('100.64.0.1', '8.8.8.8', '127.0.0.1', '::1', '-oProxyCommand=id'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                private_ipv4(value)
        with self.assertRaises(ValueError): self.build(physical=False)

    def test_route_mismatch_multiple_routes_and_bad_source_rejected(self):
        for route in ([dict(self.route[0], dst='10.0.0.1')], self.route * 2,
                      [dict(self.route[0], prefsrc='10.0.0.60')]):
            with self.subTest(route=route), self.assertRaises(ValueError): self.build(route=route)

    def test_other_listener_and_unknown_config_rejected(self):
        for change in ({'remote_port': 24223}, {'remote_port': True}, {'broker_port': 22},
                       {'password': 'forbidden'}, {'schema_version': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.build(config=dict(self.config, **change))

    def test_exposed_key_symlink_and_writable_hosts_rejected(self):
        self.key.chmod(0o640)
        with self.assertRaises(ValueError): self.build()
        self.key.chmod(0o600)
        link = Path(self.temp.name) / 'link'; link.symlink_to(self.key)
        with self.assertRaises(ValueError): private_file(str(link), secret=True)
        self.hosts.chmod(0o622)
        with self.assertRaises(ValueError): self.build()

    def test_hard_link_and_linked_ancestor_rejected(self):
        hard = Path(self.temp.name) / 'hard'
        os.link(self.key, hard)
        with self.assertRaises(ValueError): private_file(str(hard), secret=True)
        hard.unlink()
        link = Path(self.temp.name) / 'linked-directory'
        link.symlink_to(self.key.parent, target_is_directory=True)
        with self.assertRaises(ValueError): private_file(str(link / 'key'), secret=True)


if __name__ == '__main__':
    unittest.main()
