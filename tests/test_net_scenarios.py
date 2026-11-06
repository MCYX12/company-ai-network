import unittest

from scripts import net_scenarios

PORTS = """Hardware Port: Ethernet
Device: en0
Ethernet Address: d0:11:e5:ae:52:5a

Hardware Port: Wi-Fi
Device: en1
Ethernet Address: d0:11:e5:c1:c5:70
"""

IFCONFIG = """en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 192.168.110.75 netmask 0xffffff00 broadcast 192.168.110.255
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 192.168.110.40 netmask 0xffffff00 broadcast 192.168.110.255
utun7: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1500
	inet 2.0.0.1 --> 2.0.0.1 netmask 0xffffff00
"""


class NetScenarioTests(unittest.TestCase):
    def test_classify_ports(self):
        mapping = net_scenarios.classify_ports(PORTS)
        self.assertEqual(mapping['en0'], 'wired')
        self.assertEqual(mapping['en1'], 'wifi')

    def test_parse_ifconfig(self):
        parsed = net_scenarios.parse_ifconfig(IFCONFIG)
        self.assertEqual(parsed['en0']['addresses'], ['192.168.110.75'])
        self.assertEqual(parsed['en1']['addresses'], ['192.168.110.40'])
        self.assertEqual(parsed['en0']['status'], 'up')

    def test_classify_device_marks_vpn(self):
        self.assertEqual(net_scenarios.classify_device('utun7', {}), 'vpn')
        self.assertEqual(net_scenarios.classify_device('en0', {'en0': 'wired'}), 'wired')

    def test_verdict_direct_vs_vpn(self):
        rows = [{'device': 'en0', 'kind': 'wired'}, {'device': 'utun4', 'kind': 'vpn'}]
        self.assertEqual(net_scenarios.verdict([], rows, 'en0')['status'], 'unreachable')
        self.assertEqual(net_scenarios.verdict(['en0'], rows, 'en0')['status'], 'direct_intranet')
        self.assertEqual(net_scenarios.verdict(['utun4'], rows, 'utun4')['status'], 'via_vpn')
        both = net_scenarios.verdict(['en0', 'utun4'], rows, 'en0')
        self.assertEqual(both['status'], 'both')
        self.assertEqual(both['via_lan'], ['en0'])
        self.assertEqual(both['via_vpn'], ['utun4'])


if __name__ == '__main__':
    unittest.main()
