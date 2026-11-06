import unittest

from scripts import net_guard

ROUTES = """Routing tables

Internet:
Destination        Gateway            Flags        Netif Expire
default            192.168.110.1      UGScg          en0
1                  198.18.0.1         UGSc           utun5
128.0/1            198.18.0.1         UGSc           utun5
10.0.0.1/32        utun7              UGSc           utun7
192.168.110        link#7             UCS            en0
"""


class NetGuardTests(unittest.TestCase):
    def test_parse_and_detect_proxy_capture(self):
        routes = net_guard.parse_routes(ROUTES)
        dests = {row['dest'] for row in routes}
        self.assertIn('128.0/1', dests)
        capture = {row['iface'] for row in net_guard.proxy_capture(routes)}
        self.assertEqual(capture, {'utun5'})

    def test_host_route_command_per_platform(self):
        self.assertEqual(net_guard.host_route_command('192.168.1.20', 'en0', system='darwin'),
                         ['sudo', 'route', '-n', 'add', '-host', '192.168.1.20', '-interface', 'en0'])
        self.assertEqual(net_guard.host_route_command('10.0.0.5', 'utun7', system='linux'),
                         ['sudo', 'ip', 'route', 'add', '10.0.0.5/32', 'dev', 'utun7'])


if __name__ == '__main__':
    unittest.main()
