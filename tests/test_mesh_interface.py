#!/usr/bin/env python3

"""Tests that the mesh interface is configured by detection, not by release.

The multi-node CI topologies give each node a second NIC on the mesh
network and configure it by hand, because Shaken Fist creates that
interface with address=none -- cloud-init would otherwise pick it as the
default route. Nothing else in the system gives eth1 an address, and
build-smoke-cluster reads the primary's mesh IP to point mariadb_host
and loki_url at it, so a node whose mesh interface is unconfigured takes
the whole cluster down with it.

Which tool does that configuring used to be decided from
ansible_distribution_version: "Debian 12 or newer, therefore netplan".
That is an inference about an image from the release it is built on, and
it was wrong the first time the CI under-cloud moved to Debian 13 -- the
trixie image ships systemd-networkd and no netplan, on purpose, so the
netplan template failed with "Destination directory /etc/netplan does
not exist". Every multi-node lane failed with it, on every merge-group
run, for four days.

The failure was invisible to pre-merge CI because the only caller that
takes the under-cloud default is a job which runs on merge_group and
workflow_dispatch alone. There is no functional test here that would
have caught it either: the fabric is not available on a dev host. A
structural check is the only pre-merge net there is, which is the same
reasoning test_ansible_readiness.py is written from.
"""

import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


ANSIBLE_DIR = os.path.join(REPO_ROOT, 'ansible')

# The tasks that write or apply the mesh interface configuration.
#
# Three ways in, rather than a name match alone. A name match is what
# this file started with, and it makes the coverage depend on what a task
# is called: an arm renamed to "Bring up the mesh NIC" would quietly stop
# being checked by every test below. So a task also counts if it is gated
# on the style fact, or if it writes into one of the directories these
# renderers own. Coverage then follows what a task does.
MESH_TASK_RE = re.compile(
    r'^(Configure the mesh interface|Enable eth1|Enable networking)')

NET_CONFIG_DIRS = ('/etc/netplan',
                   '/etc/systemd/network',
                   '/etc/network/interfaces.d')

WRITE_MODULES = ('template', 'copy',
                 'ansible.builtin.template', 'ansible.builtin.copy')

STYLE_FACT = 'mesh_style'
NETPLAN_PROBE = '/usr/sbin/netplan'

# cloud-init renders 10-cloud-init-<nic>.network on an image it manages
# with the networkd backend, and systemd-networkd applies only the first
# matching file in lexical order. Anything we write has to sort ahead of
# that or it is never selected.
CLOUD_INIT_PREFIX = '10-'

MTU = '8950'


def documents():
    """Yield (relative path, parsed) for every playbook under ansible/."""
    for root, _, names in os.walk(ANSIBLE_DIR):
        for name in sorted(names):
            if not name.endswith(('.yml', '.yaml')):
                continue
            path = os.path.join(root, name)
            with open(path) as f:
                yield os.path.relpath(path, ANSIBLE_DIR), yaml.safe_load(f)


def tasks(container):
    """Walk every task dict inside a play, task list, or block."""
    if isinstance(container, list):
        for item in container:
            yield from tasks(item)
        return
    if not isinstance(container, dict):
        return
    if 'name' in container or 'when' in container:
        yield container
    for key in ('tasks', 'pre_tasks', 'post_tasks', 'handlers', 'block',
                'rescue', 'always'):
        if key in container:
            yield from tasks(container[key])


def writes_network_config(task):
    """True if the task templates or copies into a renderer's directory."""
    for module in WRITE_MODULES:
        spec = task.get(module)
        if isinstance(spec, dict):
            if str(spec.get('dest', '')).startswith(NET_CONFIG_DIRS):
                return True
    return False


def is_mesh_task(task):
    name = task.get('name')
    if isinstance(name, str) and MESH_TASK_RE.match(name):
        return True
    if STYLE_FACT in str(task.get('when', '')):
        return True
    return writes_network_config(task)


def style_fact_expression(parsed):
    """Return the mesh_style expression from a playbook, or None."""
    for task in tasks(parsed):
        for module in ('set_fact', 'ansible.builtin.set_fact'):
            spec = task.get(module)
            if isinstance(spec, dict) and STYLE_FACT in spec:
                return str(spec[STYLE_FACT])
    return None


def declared_styles(expression):
    """The style literals the expression can produce.

    A style is a *result* of the ternary, so drop the literals that are
    part of a test before collecting: the right-hand side of an equality
    comparison, and the contents of a membership list. Without that,
    'Debian', '10' and '11' come back as styles and the check is
    meaningless.
    """
    expression = re.sub(r'\[[^\]]*\]', '', expression)
    expression = re.sub(r"==\s*'[^']*'", '', expression)
    return set(re.findall(r"'([^']+)'", expression))


def mesh_playbooks():
    """Yield (path, parsed, [mesh tasks]) for each playbook with a mesh."""
    for path, parsed in documents():
        found = [t for t in tasks(parsed) if is_mesh_task(t)]
        if found:
            yield path, parsed, found


class TestMeshInterfaceDetection(unittest.TestCase):
    def test_some_playbook_configures_a_mesh(self):
        """Guard against every assertion below passing vacuously."""
        self.assertTrue(list(mesh_playbooks()),
                        'no playbook configures a mesh interface, so the '
                        'checks in this file are testing nothing')

    def test_no_mesh_task_branches_on_the_release(self):
        """The regression itself: choosing a live tool by version number."""
        for path, _, found in mesh_playbooks():
            for task in found:
                when = str(task.get('when', ''))
                self.assertNotIn(
                    'ansible_distribution_version', when,
                    '%s: task %r selects its network configuration tool '
                    'from the distribution release. That inference is what '
                    'broke every multi-node lane when the under-cloud moved '
                    'to Debian 13; branch on %s instead, which is derived '
                    'from what the guest actually has.'
                    % (path, task.get('name'), STYLE_FACT))

    def test_mesh_tasks_branch_on_the_detected_style(self):
        """Each arm is selected by the fact, so a new image picks an arm."""
        for path, _, found in mesh_playbooks():
            for task in found:
                self.assertIn(
                    STYLE_FACT, str(task.get('when', '')),
                    '%s: task %r is not gated on %s, so it runs on images '
                    'it was never meant for.'
                    % (path, task.get('name'), STYLE_FACT))

    def test_the_style_is_derived_from_probing_the_guest(self):
        """The fact has to come from a probe, not from another guess."""
        for path, _, _ in mesh_playbooks():
            with open(os.path.join(ANSIBLE_DIR, path)) as f:
                text = f.read()
            self.assertIn(
                NETPLAN_PROBE, text,
                '%s: configures a mesh interface but never probes for %s, '
                'so %s cannot be derived from what the guest has.'
                % (path, NETPLAN_PROBE, STYLE_FACT))

    def test_every_style_has_an_arm(self):
        """Every value the fact can take must have a task gated on it.

        The previous version of this compared the gated styles against a
        hardcoded set of three, which said nothing about the expression --
        a fourth style could be added to the set_fact with no arm behind
        it and the test still passed. Read the styles out of the
        expression instead, so the two cannot drift apart.
        """
        for path, parsed, found in mesh_playbooks():
            expression = style_fact_expression(parsed)
            self.assertIsNotNone(
                expression,
                '%s: has mesh tasks but sets no %s' % (path, STYLE_FACT))

            declared = declared_styles(expression)
            self.assertTrue(
                declared, '%s: no style literals found in %r'
                % (path, expression))

            gated = set()
            for task in found:
                when = str(task.get('when', ''))
                for style in declared:
                    if "'%s'" % style in when:
                        gated.add(style)

            self.assertEqual(
                declared, gated,
                '%s: %s can produce %s but only %s have a task gated on '
                'them. An image resolving to an unhandled style comes up '
                'with an unconfigured mesh NIC and fails later, in the '
                'deploy.' % (path, STYLE_FACT, sorted(declared),
                             sorted(gated)))

    def test_the_address_is_asserted_after_the_arms(self):
        """One ungated task has to prove whichever arm ran worked.

        networkctl reconfigure returns when the request is queued rather
        than when the address is up, a .network file that loses the
        lexical race is applied silently, and a link that is not called
        eth1 is not noticed by anything here. All three end the same way:
        no mesh address, no error, and a failure much later in the deploy.
        """
        for path, parsed, _ in mesh_playbooks():
            checks = [t for t in tasks(parsed)
                      if 'mesh_ip' in str(t.get('until', ''))]
            self.assertTrue(
                checks,
                '%s: no task retries until the mesh address is present. '
                'Without one, an arm that silently did nothing is not an '
                'error until the deploy cannot reach MariaDB.' % path)
            for task in checks:
                self.assertNotIn(
                    STYLE_FACT, str(task.get('when', '')),
                    '%s: task %r only asserts the address for one style. '
                    'Every arm can fail this way.'
                    % (path, task.get('name')))


class TestRendererFilesAreSelectable(unittest.TestCase):
    """A file systemd-networkd never selects is worse than a missing one."""

    def test_networkd_file_sorts_ahead_of_cloud_init(self):
        for path, _, found in mesh_playbooks():
            for task in found:
                if not writes_network_config(task):
                    continue
                for module in WRITE_MODULES:
                    spec = task.get(module)
                    if not isinstance(spec, dict):
                        continue
                    dest = str(spec.get('dest', ''))
                    if not dest.startswith('/etc/systemd/network'):
                        continue
                    base = os.path.basename(dest)
                    self.assertLess(
                        base, CLOUD_INIT_PREFIX,
                        '%s: %s does not sort ahead of cloud-init\'s %s* '
                        'files. systemd-networkd applies only the first '
                        'matching .network file in lexical order, so this '
                        'one would never be selected -- silently.'
                        % (path, base, CLOUD_INIT_PREFIX))


class TestThePlaybooksAgree(unittest.TestCase):
    """The block is triplicated, so drift between copies is the risk.

    Moving it into an included task file would remove the duplication
    outright and is the better answer, but it is a larger change than
    this one. Until then, a fix applied to two of the three playbooks
    should fail rather than ship.
    """

    def _copies(self):
        return list(mesh_playbooks())

    def test_more_than_one_playbook_has_a_mesh(self):
        """Otherwise the comparisons below compare nothing."""
        self.assertGreater(len(self._copies()), 1)

    def test_every_copy_decides_the_style_identically(self):
        seen = {}
        for path, parsed, _ in self._copies():
            expression = style_fact_expression(parsed)
            seen[path] = ' '.join(str(expression).split())
        self.assertEqual(
            1, len(set(seen.values())),
            'the %s expression differs between playbooks, so some of them '
            'choose a renderer differently: %s' % (STYLE_FACT, seen))

    def test_every_copy_has_the_same_arms(self):
        """Same styles, same destinations, in every playbook."""
        seen = {}
        for path, _, found in self._copies():
            arms = set()
            for task in found:
                when = ' '.join(str(task.get('when', '')).split())
                dest = ''
                for module in WRITE_MODULES:
                    spec = task.get(module)
                    if isinstance(spec, dict) and 'dest' in spec:
                        dest = str(spec['dest'])
                arms.add((when, dest))
            seen[path] = frozenset(arms)

        self.assertEqual(
            1, len(set(seen.values())),
            'the mesh arms differ between playbooks. A fix applied to '
            'some but not all of them leaves the others broken: %s'
            % {p: sorted(a) for p, a in seen.items()})


class TestRendererTemplatesAgree(unittest.TestCase):
    """The two renderers describe one interface and are synced by hand.

    There is no common source to generate them from, so the thing worth
    checking is that they have not drifted on the values that matter.
    """

    def setUp(self):
        self.netplan = self._read('files/netplan-eth1.yaml')
        self.networkd = self._read('files/networkd-eth1.network')

    def _read(self, relative):
        with open(os.path.join(ANSIBLE_DIR, relative)) as f:
            return f.read()

    def _both(self):
        return (('netplan-eth1.yaml', self.netplan),
                ('networkd-eth1.network', self.networkd))

    @staticmethod
    def _body(text):
        return '\n'.join(line for line in text.splitlines()
                         if not line.lstrip().startswith('#'))

    def test_both_templates_exist(self):
        self.assertTrue(self.netplan.strip())
        self.assertTrue(self.networkd.strip())

    def test_both_set_the_same_mtu(self):
        """The mesh MTU is set by hand because there is no DHCP to learn it."""
        for name, text in self._both():
            self.assertIn(
                MTU, text,
                '%s does not set the mesh MTU of %s. The two renderers '
                'configure the same interface and are kept in step by '
                'hand.' % (name, MTU))

    def test_both_take_the_address_and_mac(self):
        """Both are templated from the same two play variables.

        Matched as a Jinja reference against the file with its comments
        stripped, not as a bare word against the whole file: both headers
        talk about the mesh address in prose, so a template that hardcoded
        an address would still contain the string and pass.
        """
        for name, text in self._both():
            body = self._body(text)
            for var in ('address', 'macaddr'):
                self.assertRegex(
                    body, r'\{\{\s*%s\s*\}\}' % var,
                    '%s does not interpolate the %s variable, so the two '
                    'renderers no longer configure the same interface.'
                    % (name, var))

    def test_neither_template_sets_a_default_route(self):
        """The mesh must never become the default route.

        That is the whole reason this is configured by ansible after
        cloud-init rather than in the Shaken Fist network definition.
        """
        for name, text in self._both():
            body = self._body(text)
            for key in ('gateway', 'Gateway', 'routes:', 'DHCP='):
                self.assertNotIn(
                    key, body,
                    '%s sets %r. The mesh interface must never carry a '
                    'default route: cloud-init picking it as one is the '
                    'bug this whole arrangement exists to avoid.'
                    % (name, key))


if __name__ == '__main__':
    unittest.main()
