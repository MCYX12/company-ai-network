"""One pinned SSH reverse relay over a physical private LAN, supervised by systemd.

The Windows listener is loopback-only; the existing NATS mTLS exchange passes
through unchanged. No passwords, proxy configuration, or VPN fallback are used.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess


def private_ipv4(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in ipaddress.IPv4Network(net) for net in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('RFC1918 IPv4 address required')
    return str(address)


def private_file(value, secret=False):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('Independent absolute file required')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError('Independent owned regular file required')
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Linked ancestor rejected')
    if secret and info.st_mode & 0o077:
        raise ValueError('Secret must not be accessible to group or others')
    if info.st_mode & 0o022:
        raise ValueError('File must not be writable by group or others')
    return str(path)


def command(config, route):
    required = {'schema_version', 'windows_host', 'broker_host', 'broker_port',
                'remote_port', 'key', 'known_hosts', 'host_key_alias'}
    if set(config) != required or type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ValueError('Invalid relay configuration')
    windows = private_ipv4(config['windows_host'])
    broker = private_ipv4(config['broker_host'])
    for name in ('broker_port', 'remote_port'):
        if type(config[name]) is not int or not 1024 <= config[name] <= 65535:
            raise ValueError('Unprivileged port required')
    # This dedicated server account is provisioned for this single listener.
    if config['remote_port'] != 24222:
        raise ValueError('Managed listener must be 24222')
    alias = private_ipv4(config['host_key_alias'])
    if len(route) != 1 or route[0].get('dst') != windows:
        raise ValueError('One explicit route required')
    device = route[0].get('dev', '')
    source = private_ipv4(route[0].get('prefsrc', ''))
    # Reject virtual interfaces and verify the kernel identifies a physical NIC.
    if not device or not (Path('/sys/class/net') / device / 'device').exists():
        raise ValueError('Physical network interface required; VPN fallback refused')
    key = private_file(config['key'], secret=True)
    hosts = private_file(config['known_hosts'])
    return ['/usr/bin/ssh', '-F', '/dev/null', '-N', '-T', '-B', device, '-b', source,
            '-i', key, '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes',
            '-o', 'BatchMode=yes', '-o', 'PasswordAuthentication=no',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=' + alias,
            '-o', 'UserKnownHostsFile=' + hosts, '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
            '-R', '127.0.0.1:%d:%s:%d' % (config['remote_port'], broker, config['broker_port']),
            'companyagentrelay@' + windows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(private_file(args.config, secret=True)).read_text())
    host = private_ipv4(config['windows_host'])
    route = json.loads(subprocess.check_output(['/usr/sbin/ip', '-j', '-4', 'route', 'get', host], timeout=5))
    cmd = command(config, route)
    if args.check:
        print(json.dumps({'valid': True, 'transport': 'physical_lan_ssh',
                          'windows_host': host, 'interface': route[0]['dev'],
                          'listener': '127.0.0.1:24222', 'password_saved': False}))
        return
    os.execv(cmd[0], cmd)


if __name__ == '__main__':
    main()
