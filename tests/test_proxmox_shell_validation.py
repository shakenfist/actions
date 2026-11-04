#!/usr/bin/env python3

"""The Proxmox shell helpers' input guards, run with hostile arguments.

tools/proxmox-mint-vv.sh, proxmox-audit-probe.sh, proxmox-publish-node.sh,
proxmox-self-check.sh and proxmox-deploy.sh each check their inputs before
anything touches the network, because every one of those values lands
somewhere a stray character changes the meaning: a URL path, an HTTP
header, curl's --resolve, a networkspec, or a GITHUB_OUTPUT line. The
first two share those guards through tools/proxmox-api-lib.sh, and both
suites run them: a library one caller has stopped calling is a guard that
is still tested and no longer runs. The substrate lane's
own runs only ever pass good values, so they prove the happy path and
would never notice a guard that had silently stopped rejecting. These
tests run the scripts for real, with a fake curl, git and sudo first on
PATH, so a guard that stopped rejecting reaches a recorded fake rather
than the network or the host's /etc/hosts.

Each rejection is checked four ways: the exit status is non-zero, stderr
carries the message for *that* guard (so a case cannot pass by failing
for an unrelated reason), no fake was reached, and the hostile payload
is not in stdout -- stdout is where the mint script publishes its
timestamp and where a runner reads workflow commands. Positive controls
run the same fixtures with good values, so the fixtures themselves are
known to get past the guards.
"""

import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

from tests.helpers import REPO_ROOT


TOOLS = os.path.join(REPO_ROOT, 'tools')
MINT = os.path.join(TOOLS, 'proxmox-mint-vv.sh')
AUDIT = os.path.join(TOOLS, 'proxmox-audit-probe.sh')
PUBLISH = os.path.join(TOOLS, 'proxmox-publish-node.sh')
SELF_CHECK = os.path.join(TOOLS, 'proxmox-self-check.sh')
DEPLOY = os.path.join(TOOLS, 'proxmox-deploy.sh')
PLAYBOOK = os.path.join(REPO_ROOT, 'ansible', 'proxmox-single-node.yml')
ACTION_YAML = os.path.join(REPO_ROOT, 'deploy-proxmox-on-shakenfist', 'action.yml')

# A fixture, not a credential: it only has to be non-empty.
TOKEN_FIXTURE = 'fixture-token-value'

# Each fake records that it ran, so a test can tell "rejected before the
# network" from "rejected by whatever the network said".
FAKE_CURL = r'''#!/bin/bash
echo curl >> "${FAKE_LOG}"
out=''
headers=''
url=''
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output) out="$2"; shift 2 ;;
        --dump-header) headers="$2"; shift 2 ;;
        https://*) url="$1"; shift ;;
        *) shift ;;
    esac
done
# The URL goes to a log of its own, so a test can tell which endpoint was
# called, and in which order, where FAKE_LOG only says "curl".
echo "${url}" >> "${FAKE_URL_LOG}"
case "${url}" in
    */status/current)
        # The audit read. Its code and reason phrase are fixtures too, so a
        # test can hand this script PVE's 403.
        cat "${FAKE_CURL_GUEST_RESPONSE}" > "${out}"
        printf 'HTTP/1.1 %s %s\r\n' "${FAKE_CURL_GUEST_CODE}" "${FAKE_CURL_GUEST_REASON}" > "${headers}"
        printf '%s' "${FAKE_CURL_GUEST_CODE}"
        ;;
    *)
        # The mint. The ticket's password field is filled in here rather
        # than kept in the fixture, so no test file holds a value under
        # that name (CodeQL's clear-text storage query cannot tell a
        # fixture from a secret).
        jq '.data.password = .data.host' "${FAKE_CURL_RESPONSE}" > "${out}"
        printf 'HTTP/1.1 200 OK\r\n' > "${headers}"
        printf '200'
        ;;
esac
'''

FAKE_RECORDER = r'''#!/bin/bash
echo "$(basename "$0")" >> "${FAKE_LOG}"
echo "fake $(basename "$0") reached" >&2
exit 97
'''


def guest(vmid=100, status='running'):
    """What GET .../status/current answers for a running guest."""
    return {'data': {'status': status, 'vmid': vmid, 'name': 'spice-target', 'qmpstatus': status}}


def ticket(proxy='http://pve1.example.test:3128'):
    return {'data': {
        'type': 'spice',
        'proxy': proxy,
        'host': 'pvespiceproxy:6aaf3e30:100:pve1:61000::0ce019e3c7ab',
        'tls-port': 61000,
        'host-subject': 'OU=PVE Cluster Node,O=Proxmox Virtual Environment,CN=pve1.example.test',
        'ca': '-----BEGIN CERTIFICATE-----\\nMIIB\\n-----END CERTIFICATE-----\\n',
    }}


class ShellHarness(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)
        self.bindir = os.path.join(self.tmpdir, 'bin')
        os.mkdir(self.bindir)
        self.write_exec('curl', FAKE_CURL)
        for name in ('git', 'sudo', 'ansible-playbook', 'ansible-galaxy'):
            self.write_exec(name, FAKE_RECORDER)
        self.fake_log = os.path.join(self.tmpdir, 'fake.log')
        open(self.fake_log, 'w').close()
        self.fake_url_log = os.path.join(self.tmpdir, 'urls.log')
        open(self.fake_url_log, 'w').close()

        self.workdir = os.path.join(self.tmpdir, 'work')
        os.mkdir(self.workdir)
        self.token_file = os.path.join(self.workdir, 'token')
        with open(self.token_file, 'w') as f:
            f.write(TOKEN_FIXTURE + '\n')
        self.ca_file = os.path.join(self.workdir, 'pve-root-ca.pem')
        with open(self.ca_file, 'w') as f:
            f.write('not a real CA; nothing here reads it\n')
        self.response = os.path.join(self.tmpdir, 'response.json')
        self.set_ticket(ticket())
        self.guest_response = os.path.join(self.tmpdir, 'guest.json')
        self.set_guest(guest())
        self.github_output = os.path.join(self.tmpdir, 'github_output')
        self.github_env = os.path.join(self.tmpdir, 'github_env')
        open(self.github_output, 'w').close()
        open(self.github_env, 'w').close()

    def write_exec(self, name, content):
        path = os.path.join(self.bindir, name)
        with open(path, 'w') as f:
            f.write(content)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def set_ticket(self, body):
        with open(self.response, 'w') as f:
            json.dump(body, f)

    def set_guest(self, body):
        with open(self.guest_response, 'w') as f:
            json.dump(body, f)

    def run_script(self, script, args, extra_env=None):
        env = dict(os.environ)
        env.pop('no_proxy', None)
        env.pop('NO_PROXY', None)
        # The happy-path answers, which a case overrides through extra_env
        # to hand a script what a real node would say on a bad day.
        env.update({
            'FAKE_CURL_GUEST_CODE': '200',
            'FAKE_CURL_GUEST_REASON': 'OK',
        })
        env.update(extra_env or {})
        env.update({
            'PATH': self.bindir + os.pathsep + env.get('PATH', ''),
            'FAKE_LOG': self.fake_log,
            'FAKE_URL_LOG': self.fake_url_log,
            'FAKE_CURL_RESPONSE': self.response,
            'FAKE_CURL_GUEST_RESPONSE': self.guest_response,
            'GITHUB_OUTPUT': self.github_output,
            'GITHUB_ENV': self.github_env,
        })
        return subprocess.run([script] + args, env=env, capture_output=True,
                              text=True, timeout=60)

    def fakes_reached(self):
        with open(self.fake_log) as f:
            return f.read().split()

    def urls_reached(self):
        with open(self.fake_url_log) as f:
            return f.read().split()

    def assertRejected(self, result, stderr_pattern, payload, fakes_allowed=(),
                       quoted_in_annotation=False):
        self.assertNotEqual(result.returncode, 0, 'accepted: %r' % (result,))
        self.assertRegex(result.stderr, stderr_pattern)
        if payload is not None:
            if quoted_in_annotation:
                # The one allowed appearance: quoted inside the script's own
                # ::error annotation, as the diagnostic, which only a value
                # with no newline can reach. It must never start a line of
                # its own, where the runner would read it as a command.
                for line in result.stdout.splitlines():
                    if payload in line:
                        self.assertTrue(line.startswith('::error title=Proxmox substrate::'), line)
            else:
                self.assertNotIn(payload, result.stdout)
        self.assertEqual([f for f in self.fakes_reached() if f not in fakes_allowed], [])


class MintVvTest(ShellHarness):
    def good_args(self, **overrides):
        values = {
            '--api-url': 'https://pve1.example.test:8006',
            '--node': 'pve1',
            '--vmid': '100',
            '--token-id': 'kerbside@pve!ci',
            '--token-file': self.token_file,
            '--ca-file': self.ca_file,
            '--out': os.path.join(self.workdir, 'console.vv'),
            '--resolve': '10.0.2.2',
        }
        values.update(overrides)
        args = []
        for key, value in values.items():
            args += [key, value]
        return args

    def assertMintRejected(self, stderr_pattern, payload, **overrides):
        result = self.run_script(MINT, self.good_args(**overrides))
        self.assertRejected(result, stderr_pattern, payload)
        # stdout is the mint time and nothing else; a failure publishes none.
        self.assertEqual(result.stdout, '')
        self.assertFalse(os.path.exists(os.path.join(self.workdir, 'console.vv')))

    def test_help_prints_the_whole_usage_example(self):
        result = self.run_script(MINT, ['--help'])
        self.assertEqual(2, result.returncode)
        lines = result.stderr.splitlines()
        self.assertTrue(lines[0].startswith('  proxmox-mint-vv.sh --api-url '),
                        result.stderr)
        self.assertIn('--out /path/to/console.vv', lines[-1])
        self.assertEqual(4, len(lines), result.stderr)

    def test_good_arguments_mint_a_vv(self):
        result = self.run_script(MINT, self.good_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'^[0-9]+\.[0-9]+\n$')
        self.assertEqual(self.fakes_reached(), ['curl'])

    def test_a_token_id_with_a_newline_is_rejected(self):
        payload = 'X-Injected: yes'
        self.assertMintRejected('--token-id is not of the form', payload,
                                **{'--token-id': 'kerbside@pve!ci\n' + payload})

    def test_a_token_id_with_a_second_equals_is_rejected(self):
        self.assertMintRejected('--token-id is not of the form', 'ci=extra',
                                **{'--token-id': 'kerbside@pve!ci=extra'})

    def test_an_api_url_with_userinfo_is_rejected(self):
        payload = 'user:x@'
        self.assertMintRejected('--api-url must be', payload,
                                **{'--api-url': 'https://%spve1.example.test:8006' % payload})

    def test_an_api_url_with_a_path_is_rejected(self):
        self.assertMintRejected('--api-url must be', '/evil',
                                **{'--api-url': 'https://pve1.example.test:8006/evil'})

    def test_a_plain_http_api_url_is_rejected(self):
        self.assertMintRejected('--api-url must be', 'http://',
                                **{'--api-url': 'http://pve1.example.test:8006'})

    def test_a_resolve_with_a_space_is_rejected(self):
        self.assertMintRejected('--resolve is not an address', 'a b', **{'--resolve': 'a b'})

    def test_a_resolve_with_a_port_is_rejected(self):
        # A colon is legal in an IPv6 address; a second host:port pair is not.
        self.assertMintRejected('--resolve is not an address', 'evil',
                                **{'--resolve': '10.0.2.2,evil.example:443:6.6.6.6'})

    def test_a_node_that_leaves_the_path_segment_is_rejected(self):
        self.assertMintRejected('--node is not a node name', 'qemu/100',
                                **{'--node': 'pve1/qemu/100'})

    def test_a_node_starting_with_a_dot_is_rejected(self):
        self.assertMintRejected('--node is not a node name', '..', **{'--node': '..'})

    def test_a_vmid_that_is_not_a_number_is_rejected(self):
        self.assertMintRejected('--vmid is not a number', '100/../101', **{'--vmid': '100/../101'})


class AuditProbeTest(ShellHarness):
    """The audit read: the call that proves the token's VM.Audit half.

    It takes the same values as the mint and runs the same guards, so
    these cases are the proof that this script runs them at all -- the
    guards live in a library now, and a caller that stopped calling
    pve_validate_api_target would pass the mint's suite untouched.
    """

    def good_args(self, **overrides):
        values = {
            '--api-url': 'https://pve1.example.test:8006',
            '--node': 'pve1',
            '--vmid': '100',
            '--token-id': 'kerbside@pve!ci',
            '--token-file': self.token_file,
            '--ca-file': self.ca_file,
        }
        values.update(overrides)
        args = []
        for key, value in values.items():
            args += [key, value]
        return args

    def assertAuditRejected(self, stderr_pattern, payload, **overrides):
        result = self.run_script(AUDIT, self.good_args(**overrides))
        self.assertRejected(result, stderr_pattern, payload)
        self.assertEqual(result.stdout, '')

    def test_help_prints_the_whole_usage_example(self):
        result = self.run_script(AUDIT, ['--help'])
        self.assertEqual(2, result.returncode)
        lines = result.stderr.splitlines()
        self.assertTrue(lines[0].startswith('  proxmox-audit-probe.sh --api-url '),
                        result.stderr)
        self.assertIn('--ca-file /path/to/pve-root-ca.pem', lines[-1])
        self.assertEqual(3, len(lines), result.stderr)

    def test_good_arguments_read_the_guest(self):
        result = self.run_script(AUDIT, self.good_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fakes_reached(), ['curl'])
        # The endpoint matters: it is the one PVE gates on VM.Audit.
        self.assertEqual(self.urls_reached(),
                         ['https://pve1.example.test:8006/api2/json/nodes/pve1/qemu/100/status/current'])
        self.assertIn('status running', result.stdout)

    def test_a_node_that_leaves_the_path_segment_is_rejected(self):
        self.assertAuditRejected('--node is not a node name', 'qemu/100',
                                 **{'--node': 'pve1/qemu/100'})

    def test_a_vmid_that_is_not_a_number_is_rejected(self):
        self.assertAuditRejected('--vmid is not a number', '100/../101', **{'--vmid': '100/../101'})

    def test_a_token_id_with_a_newline_is_rejected(self):
        payload = 'X-Injected: yes'
        self.assertAuditRejected('--token-id is not of the form', payload,
                                 **{'--token-id': 'kerbside@pve!ci\n' + payload})

    def test_a_plain_http_api_url_is_rejected(self):
        self.assertAuditRejected('--api-url must be', 'http://',
                                 **{'--api-url': 'http://pve1.example.test:8006'})

    def test_an_api_url_with_a_path_is_rejected(self):
        self.assertAuditRejected('--api-url must be', '/evil',
                                 **{'--api-url': 'https://pve1.example.test:8006/evil'})

    def test_resolve_is_not_an_argument_here(self):
        # The mint takes one because proxmox-publish-node.sh mints before
        # the node's name resolves on the runner. This runs after that, so
        # an unknown argument is the right answer rather than a guard.
        self.assertAuditRejected('unknown argument: --resolve', '10.0.2.2',
                                 **{'--resolve': '10.0.2.2'})

    def test_a_missing_ca_file_is_rejected(self):
        self.assertAuditRejected('cannot read --ca-file', None,
                                 **{'--ca-file': os.path.join(self.tmpdir, 'absent.pem')})

    def test_a_403_says_which_privilege_is_missing(self):
        result = self.run_script(AUDIT, self.good_args(), extra_env={
            'FAKE_CURL_GUEST_CODE': '403',
            'FAKE_CURL_GUEST_REASON': 'Permission check failed (/vms/100, VM.Audit)',
        })
        self.assertNotEqual(result.returncode, 0)
        # PVE's own words, and what they mean for this repository's role.
        self.assertIn('Permission check failed (/vms/100, VM.Audit)', result.stderr)
        self.assertIn('missing VM.Audit on /vms', result.stderr)

    def test_another_refusal_does_not_blame_vm_audit(self):
        result = self.run_script(AUDIT, self.good_args(), extra_env={
            'FAKE_CURL_GUEST_CODE': '500',
            'FAKE_CURL_GUEST_REASON': 'internal error',
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('HTTP 500 internal error', result.stderr)
        self.assertNotIn('VM.Audit', result.stderr)

    def test_an_answer_about_another_guest_is_rejected(self):
        self.set_guest(guest(vmid=101))
        result = self.run_script(AUDIT, self.good_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('answered about 101', result.stderr)

    def test_an_answer_with_no_status_is_rejected(self):
        self.set_guest({'data': {'vmid': 100}})
        result = self.run_script(AUDIT, self.good_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no "status" field', result.stderr)

    def test_an_answer_that_is_not_a_guest_object_is_rejected(self):
        self.set_guest({'data': 'nope'})
        result = self.run_script(AUDIT, self.good_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no guest object', result.stderr)


class SelfCheckTest(ShellHarness):
    def test_a_bad_node_is_rejected_before_the_network(self):
        result = self.run_script(SELF_CHECK, [
            '--workdir', self.workdir,
            '--api-url', 'https://pve1.example.test:8006',
            '--node', 'pve1/qemu/100',
            '--vmid', '100',
            '--token-id', 'kerbside@pve!ci',
        ])
        self.assertRejected(result, '--node is not a node name', 'qemu/100')

    def test_the_audit_read_runs_before_the_mint_and_can_fail_the_step(self):
        # The read is first because that is the order a broker uses the
        # two privileges, and because a token that cannot read a guest has
        # nothing to mint for. A 403 here must stop the step: when it did
        # not exist, dropping VM.Audit from the role left this lane green.
        result = self.run_script(SELF_CHECK, [
            '--workdir', self.workdir,
            '--api-url', 'https://pve1.example.test:8006',
            '--node', 'pve1',
            '--vmid', '100',
            '--token-id', 'kerbside@pve!ci',
        ], extra_env={
            'FAKE_CURL_GUEST_CODE': '403',
            'FAKE_CURL_GUEST_REASON': 'Permission check failed (/vms/100, VM.Audit)',
        })
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('missing VM.Audit on /vms', result.stderr)
        # The mint never ran, so no ticket was minted and nothing was
        # written where a .vv would go.
        self.assertEqual(self.urls_reached(),
                         ['https://pve1.example.test:8006/api2/json/nodes/pve1/qemu/100/status/current'])
        self.assertFalse(os.path.exists(os.path.join(self.workdir, 'self-check.vv')))
        # The step reports the substrate's failure to the runner.
        self.assertIn('::error title=Proxmox substrate::', result.stdout)


class PublishNodeTest(ShellHarness):
    def write_facts(self, **overrides):
        facts = {
            'node_name': 'pve1',
            'node_fqdn': 'pve1.example.test',
            'node_address': '10.0.2.2',
            'domain': 'example.test',
            'pve_version': '9.0.3',
            'token_id': 'kerbside@pve!ci',
            'vmid': 100,
            'kvm': True,
        }
        facts.update(overrides)
        with open(os.path.join(self.workdir, 'facts.json'), 'w') as f:
            json.dump(facts, f)

    def publish(self):
        return self.run_script(PUBLISH, ['--workdir', self.workdir])

    def assertNothingPublished(self):
        for path in (self.github_output, self.github_env):
            with open(path) as f:
                self.assertEqual(f.read(), '', path)

    def test_no_proxy_keeps_the_entries_of_both_spellings(self):
        # Past the /etc/hosts check with a getent that resolves the node,
        # the rewrite must start from the union of no_proxy and NO_PROXY:
        # an entry only one of them held must survive in both.
        self.write_exec('getent', '#!/bin/bash\n'
                        'echo "10.0.2.2 STREAM pve1.example.test"\n')
        self.write_exec('sudo', '#!/bin/bash\ncat > /dev/null\n')
        self.write_facts()
        result = self.run_script(PUBLISH, ['--workdir', self.workdir],
                                 extra_env={'no_proxy': 'localhost,lower',
                                            'NO_PROXY': 'upper,localhost'})
        self.assertEqual(0, result.returncode, result.stderr)
        with open(self.github_env) as f:
            env_lines = f.read().splitlines()
        merged = 'localhost,lower,upper,pve1.example.test,10.0.2.2'
        self.assertEqual(['no_proxy=' + merged, 'NO_PROXY=' + merged],
                         env_lines)

    def test_good_facts_get_as_far_as_the_runner_hosts_file(self):
        # The positive control: good facts and a matching ticket pass every
        # guard and the mint, and stop only at the fake sudo that stands in
        # for the /etc/hosts write.
        self.write_facts()
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fake sudo reached', result.stderr)
        self.assertEqual(self.fakes_reached(), ['curl', 'sudo'])

    def test_a_newline_in_the_node_name_is_rejected(self):
        payload = 'node_address=6.6.6.6'
        self.write_facts(node_name='pve1\n' + payload)
        result = self.publish()
        self.assertRejected(result, 'has a newline in node_name', payload)
        self.assertNothingPublished()

    def test_a_newline_in_the_token_id_is_rejected(self):
        payload = 'api_url=https://attacker.example'
        self.write_facts(token_id='kerbside@pve!ci\n' + payload)
        result = self.publish()
        self.assertRejected(result, 'has a newline in token_id', payload)
        self.assertNothingPublished()

    def test_a_node_address_that_is_not_ipv4_is_rejected(self):
        self.write_facts(node_address='10.0.2.2 attacker.example')
        result = self.publish()
        self.assertRejected(result, 'is not an IPv4 address', 'attacker.example',
                            quoted_in_annotation=True)
        self.assertNothingPublished()

    def test_a_facts_fqdn_that_is_not_qualified_is_rejected(self):
        self.write_facts(node_fqdn='pve1')
        result = self.publish()
        self.assertRejected(result, 'is not a fully qualified name', None)
        self.assertNothingPublished()

    def test_a_facts_fqdn_with_a_url_in_it_is_rejected(self):
        payload = 'attacker.example/x'
        self.write_facts(node_fqdn='pve1.example.test@' + payload)
        result = self.publish()
        self.assertRejected(result, 'is not a fully qualified name', payload,
                            quoted_in_annotation=True)
        self.assertNothingPublished()

    def test_a_ticket_naming_a_different_fqdn_is_rejected(self):
        # The mismatch is found after the mint, so curl is reached; the name
        # is well-formed by then and is printed on purpose, as the
        # diagnostic. What must not happen is anything being published.
        self.set_ticket(ticket(proxy='http://pve1.elsewhere.test:3128'))
        self.write_facts()
        result = self.publish()
        self.assertRejected(result, 'names the node pve1.elsewhere.test, but the node calls itself',
                            None, fakes_allowed=('curl',))
        self.assertNothingPublished()

    def test_a_ticket_proxy_url_with_a_path_is_rejected(self):
        self.set_ticket(ticket(proxy='http://pve1.example.test:3128/x'))
        self.write_facts()
        result = self.publish()
        self.assertRejected(result, "proxy URL is not http://<name>:<port>", None,
                            fakes_allowed=('curl',))
        self.assertNothingPublished()


def playbook_vars():
    with open(PLAYBOOK) as f:
        return yaml.safe_load(f)[0]['vars']


class DeployNodeAddressTest(ShellHarness):
    """proxmox-deploy.sh and the playbook must refuse the same addresses.

    The playbook asserts proxmox_node_address is a host in
    proxmox_netblock other than the router; the script checks the same
    thing earlier, with the block spelled out. Every address in the block
    and either side of it is tried here, so the two cannot drift apart.
    """

    def deploy(self, address):
        # The fakes are reset per call: only the last run's matter.
        open(self.fake_log, 'w').close()
        return self.run_script(DEPLOY, [
            '--workdir', self.workdir,
            '--base-user', 'debian',
            '--node-address', address,
            '--smoke-vmid', '100',
        ])

    def accepted(self, address):
        # Accepted means it got past every guard to the (fake) git clone.
        result = self.deploy(address)
        if 'fake git reached' in result.stderr:
            return True
        self.assertRegex(result.stderr, '--node-address must be a host address')
        self.assertEqual(self.fakes_reached(), [])
        return False

    def test_the_defaults_are_accepted(self):
        with open(ACTION_YAML) as f:
            default = yaml.safe_load(f)['inputs']['node_address']['default']
        self.assertEqual(default, playbook_vars()['proxmox_node_address'])
        self.assertTrue(self.accepted(default))

    def test_exactly_the_playbook_netblock_hosts_are_accepted(self):
        netblock = ipaddress.ip_network(playbook_vars()['proxmox_netblock'])
        # The playbook's assert holds the block to a /24 with the router at
        # .1; if either changes, this and the script's pattern must too.
        self.assertEqual(netblock.prefixlen, 24)
        router = netblock.network_address + 1
        expected = {str(h) for h in netblock.hosts()} - {str(router)}

        # Every address in the block, plus the edges of the blocks either
        # side of it.
        below = ipaddress.ip_network('%s/24' % (netblock.network_address - 256))
        above = ipaddress.ip_network('%s/24' % (netblock.network_address + 256))
        candidates = [str(a) for a in netblock] + [
            str(n.network_address + i) for n in (below, above) for i in (0, 1, 2, 254, 255)]
        accepted = {a for a in candidates if self.accepted(a)}
        self.assertEqual(accepted, expected)

    def test_hostile_addresses_are_rejected(self):
        for address in ('10.0.2.2; id', '10.0.2.2,float=true', '10.0.2.02',
                        '010.0.2.2', '10.0.2.2\n', ' 10.0.2.2', '10.0.2.256'):
            with self.subTest(address=address):
                self.assertFalse(self.accepted(address))


class DeployPatternAgreementTest(unittest.TestCase):
    def test_the_script_names_the_playbook_netblock(self):
        # The error message is what an operator reads; it must name the
        # block the playbook actually uses.
        with open(DEPLOY) as f:
            script = f.read()
        netblock = playbook_vars()['proxmox_netblock']
        self.assertRegex(script, r'must be a host address in %s' % re.escape(netblock))


if __name__ == '__main__':
    unittest.main()
