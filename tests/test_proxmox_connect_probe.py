#!/usr/bin/env python3

"""Tests for tools/proxmox-connect-probe.py's parsing.

Only read_vv, parse_proxy and connect_target are covered: they are the
pure, offline parts of the probe, and per AGENTS.md the socket-handling
rest of it cannot be exercised without a real PVE node, which is why
proxmox-substrate.yml's own runs are what settle that half. These three
are also precisely the parts that can reject a *valid* .vv and report it
as a substrate failure rather than as what it actually is: a parsing
bug here.

The .vv content below follows the real shape a mint leaves, per the
master plan's *Three protocol details* and PLAN-proxmox-source.md's
worked example: a bare "host" pseudo-hostname (not a DNS name), and a
"ca" field with its newlines escaped as literal backslash-n rather than
real ones, because it has to survive as one INI value.
"""

import os
import tempfile
import unittest

from tests.helpers import load_script


probe = load_script('tools/proxmox-connect-probe.py', 'proxmox_connect_probe')


REAL_HOST = 'pvespiceproxy:6aaf3e30:100:pve1:61000::0ce019e3c7ab'


def write_vv(tmp_path, **fields):
    lines = ['[virt-viewer]']
    for key, value in fields.items():
        lines.append('%s=%s' % (key, value))
    with open(tmp_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return tmp_path


def real_vv_fields(**overrides):
    # No "password" field: read_vv never looks at one, and a fixture
    # writing a hardcoded value under that key -- even an obviously fake
    # one -- is indistinguishable from a real secret to a static scanner
    # (this tripped CodeQL's clear-text-storage-of-sensitive-information
    # query on this file's first version).
    fields = {
        'type': 'spice',
        'proxy': 'http://pve1.example:3128',
        'host': REAL_HOST,
        'tls-port': '61000',
        'host-subject': ('OU=PVE Cluster Node,O=Proxmox Virtual '
                         'Environment,CN=pve1.example'),
        # Escaped, not a real newline: see the module docstring above.
        'ca': '-----BEGIN CERTIFICATE-----\\nMIIB...\\n-----END CERTIFICATE-----\\n',
    }
    fields.update(overrides)
    return fields


class ReadVvTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmpdir))
        self.path = os.path.join(self.tmpdir, 'test.vv')

    def test_a_realistic_vv_parses_cleanly(self):
        write_vv(self.path, **real_vv_fields())
        proxy, host, tls_port = probe.read_vv(self.path)
        self.assertEqual(proxy, 'http://pve1.example:3128')
        self.assertEqual(host, REAL_HOST)
        self.assertEqual(tls_port, '61000')

    def test_a_vv_with_a_section_but_no_virt_viewer_is_rejected(self):
        with open(self.path, 'w') as f:
            f.write('[other]\nproxy=http://pve1.example:3128\n')
        with self.assertRaises(probe.ProbeError) as cm:
            probe.read_vv(self.path)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('has no [virt-viewer] section', str(cm.exception))

    def test_a_missing_key_is_rejected(self):
        fields = real_vv_fields()
        del fields['tls-port']
        write_vv(self.path, **fields)
        with self.assertRaises(probe.ProbeError) as cm:
            probe.read_vv(self.path)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('tls-port', str(cm.exception))

    def test_an_unparseable_file_is_rejected_without_quoting_it(self):
        # configparser's own errors sometimes quote the offending line,
        # and a line of a .vv may be a credential -- read_vv is
        # documented to show only the exception's class, never its text.
        # A line with neither a section header, a comment nor a "="
        # trips configparser.ParsingError.
        with open(self.path, 'w') as f:
            f.write('[virt-viewer]\nproxy=http://pve1.example:3128\n'
                    'this line has no delimiter\n')
        with self.assertRaises(probe.ProbeError) as cm:
            probe.read_vv(self.path)
        self.assertEqual(cm.exception.code, 2)
        self.assertNotIn('this line has no delimiter', str(cm.exception))
        self.assertIn('ParsingError', str(cm.exception))


class ParseProxyTest(unittest.TestCase):
    def test_the_documented_proxmox_shape_parses(self):
        # "http://<hostname -f>:3128", exactly what proxmox-mint-vv.sh's
        # docstring says PVE advertises.
        host, port = probe.parse_proxy('http://pve1.example:3128')
        self.assertEqual((host, port), ('pve1.example', 3128))

    def test_a_bare_host_gets_the_default_scheme_and_port(self):
        # The scheme is optional in the .vv format, and 3128 is the
        # default remote-viewer also assumes.
        host, port = probe.parse_proxy('pve1.example')
        self.assertEqual((host, port), ('pve1.example', probe.DEFAULT_PROXY_PORT))

    def test_a_non_http_scheme_is_rejected(self):
        with self.assertRaises(probe.ProbeError) as cm:
            probe.parse_proxy('https://pve1.example:3128')
        self.assertEqual(cm.exception.code, 2)

    def test_a_bad_port_is_rejected(self):
        with self.assertRaises(probe.ProbeError) as cm:
            probe.parse_proxy('http://pve1.example:not-a-port')
        self.assertEqual(cm.exception.code, 2)

    def test_a_proxy_with_no_host_is_rejected(self):
        with self.assertRaises(probe.ProbeError) as cm:
            probe.parse_proxy('http://:3128')
        self.assertEqual(cm.exception.code, 2)


class ConnectTargetTest(unittest.TestCase):
    def test_the_real_pseudo_hostname_is_accepted(self):
        # See the comment beside connect_target's regex: this exact
        # shape (pvespiceproxy:<hex>:<vmid>:<node>:<port>:...:<hex>) is
        # what a real mint produces, and it must keep parsing.
        self.assertEqual(
            probe.connect_target(REAL_HOST, '61000'),
            '%s:61000' % REAL_HOST)

    def test_a_host_that_could_end_the_request_line_is_rejected(self):
        for bad_host in ('evil\r\nX-Injected: 1', 'evil host', 'evil\nhost'):
            with self.subTest(bad_host=bad_host):
                with self.assertRaises(probe.ProbeError) as cm:
                    probe.connect_target(bad_host, '3128')
                self.assertEqual(cm.exception.code, 2)
                # The ticket is a credential: the message names the
                # problem, never the value that triggered it.
                self.assertNotIn(bad_host, str(cm.exception))

    def test_a_non_numeric_port_is_rejected(self):
        with self.assertRaises(probe.ProbeError) as cm:
            probe.connect_target(REAL_HOST, 'sixty-one-thousand')
        self.assertEqual(cm.exception.code, 2)
        self.assertNotIn('sixty-one-thousand', str(cm.exception))

    def test_an_empty_port_is_rejected(self):
        with self.assertRaises(probe.ProbeError):
            probe.connect_target(REAL_HOST, '')


class NoCredentialLeaksTest(unittest.TestCase):
    """Nothing in read_vv's error paths may print the pseudo-hostname.

    "host" carries a live proxy ticket (see the module docstring), so an
    error naming it would print a credential to a public CI log.
    """

    def test_read_vv_errors_never_mention_the_host(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir))
        path = os.path.join(tmpdir, 'test.vv')
        fields = real_vv_fields()
        del fields['proxy']
        write_vv(path, **fields)
        with self.assertRaises(probe.ProbeError) as cm:
            probe.read_vv(path)
        self.assertNotIn(REAL_HOST, str(cm.exception))


if __name__ == '__main__':
    unittest.main()
