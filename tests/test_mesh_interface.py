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

# The tasks that write or apply the mesh interface configuration. Matched
# on the name prefix rather than on the module, because the three arms use
# three different modules (copy, template and shell) and it is the
# decision they share -- not the mechanism -- that this file is about.
MESH_TASK_RE = re.compile(
    r'^(Configure the mesh interface|Enable eth1|Enable networking)')

# The fact the arms are expected to branch on, and the probe it is
# expected to be derived from.
STYLE_FACT = 'mesh_style'
NETPLAN_PROBE = '/usr/sbin/netplan'

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


def mesh_playbooks():
    """Yield (path, [mesh tasks]) for every playbook that configures a mesh."""
    for path, parsed in documents():
        found = [t for t in tasks(parsed)
                 if isinstance(t.get('name'), str)
                 and MESH_TASK_RE.match(t['name'])]
        if found:
            yield path, found


class TestMeshInterfaceDetection(unittest.TestCase):
    def test_some_playbook_configures_a_mesh(self):
        """Guard against every assertion below passing vacuously."""
        self.assertTrue(list(mesh_playbooks()),
                        'no playbook configures a mesh interface, so the '
                        'checks in this file are testing nothing')

    def test_no_mesh_task_branches_on_the_release(self):
        """The regression itself: choosing a live tool by version number."""
        for path, found in mesh_playbooks():
            for task in found:
                when = str(task.get('when', ''))
                self.assertNotIn(
                    'ansible_distribution_version', when,
                    '%s: task %r selects its network configuration tool '
                    'from the distribution release. That inference is what '
                    'broke every multi-node lane when the under-cloud moved '
                    'to Debian 13; branch on %s instead, which is derived '
                    'from what the guest actually has.'
                    % (path, task['name'], STYLE_FACT))

    def test_mesh_tasks_branch_on_the_detected_style(self):
        """Each arm is selected by the fact, so a new image picks an arm."""
        for path, found in mesh_playbooks():
            for task in found:
                self.assertIn(
                    STYLE_FACT, str(task.get('when', '')),
                    '%s: task %r is not gated on %s, so it runs on images '
                    'it was never meant for.'
                    % (path, task['name'], STYLE_FACT))

    def test_the_style_is_derived_from_probing_the_guest(self):
        """The fact has to come from a probe, not from another guess."""
        for path, _ in mesh_playbooks():
            with open(os.path.join(ANSIBLE_DIR, path)) as f:
                text = f.read()
            self.assertIn(
                NETPLAN_PROBE, text,
                '%s: configures a mesh interface but never probes for %s, '
                'so %s cannot be derived from what the guest has.'
                % (path, NETPLAN_PROBE, STYLE_FACT))

    def test_both_renderers_are_available(self):
        """Every style a playbook can select needs a template to render."""
        for path, found in mesh_playbooks():
            styles = set()
            for task in found:
                for style in ('netplan', 'networkd', 'ifupdown'):
                    if "'%s'" % style in str(task.get('when', '')):
                        styles.add(style)
            self.assertEqual(
                {'netplan', 'networkd', 'ifupdown'}, styles,
                '%s: selects %s, so an image that is none of those would '
                'come up with an unconfigured mesh interface -- which fails '
                'later and much less legibly than a missing file does.'
                % (path, sorted(styles)))


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

    def test_both_templates_exist(self):
        self.assertTrue(self.netplan.strip())
        self.assertTrue(self.networkd.strip())

    def test_both_set_the_same_mtu(self):
        """The mesh MTU is set by hand because there is no DHCP to learn it."""
        for name, text in (('netplan-eth1.yaml', self.netplan),
                           ('networkd-eth1.network', self.networkd)):
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
        for name, text in (('netplan-eth1.yaml', self.netplan),
                           ('networkd-eth1.network', self.networkd)):
            body = '\n'.join(line for line in text.splitlines()
                              if not line.lstrip().startswith('#'))
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
        for name, text in (('netplan-eth1.yaml', self.netplan),
                           ('networkd-eth1.network', self.networkd)):
            body = '\n'.join(line for line in text.splitlines()
                             if not line.lstrip().startswith('#'))
            for key in ('gateway', 'Gateway', 'routes:', 'DHCP='):
                self.assertNotIn(
                    key, body,
                    '%s sets %r. The mesh interface must never carry a '
                    'default route: cloud-init picking it as one is the '
                    'bug this whole arrangement exists to avoid.'
                    % (name, key))


if __name__ == '__main__':
    unittest.main()
