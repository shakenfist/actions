#!/usr/bin/env python3

"""Tests that every playbook which creates instances waits for cloud-init.

sshd answers on port 22 well before cloud-init has finished, and
cloud-init then regenerates the host keys and restarts sshd, so anything
connecting in that window is dropped with "connection refused". The gate
that closes it lives in ansible/tasks/wait-for-cloud-init.yml and has to
be imported by each playbook that creates instances.

That is exactly the kind of invariant which rots quietly. It was already
broken once: kerbside-single-node.yml grew the gate in bea359f and the
other eleven provisioning paths kept the banner-only wait, which is why
issue #2 stayed open. Nothing in CI can catch a missing gate before
merge either -- the fabric is not available on a dev host, so the first
sign is a flaky canary run some time later. A structural check is the
only pre-merge net there is.
"""

import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


ANSIBLE_DIR = os.path.join(REPO_ROOT, 'ansible')
GATE = 'tasks/wait-for-cloud-init.yml'


def documents():
    """Yield (path relative to ansible/, parsed) for every YAML file under it.

    Recursive, and keyed on the relative path rather than the basename, so
    that a task file which creates instances is recognised wherever it
    lives and so include paths can be compared without flattening them.
    """
    for root, _, names in os.walk(ANSIBLE_DIR):
        for name in sorted(names):
            if not name.endswith('.yml'):
                continue
            path = os.path.join(root, name)
            with open(path) as f:
                yield os.path.relpath(path, ANSIBLE_DIR), yaml.safe_load(f)


def tasks(container):
    """Walk every task dict inside a play, task list, or block.

    Plays keep tasks under several keys and blocks nest arbitrarily, so
    recurse rather than trying to enumerate the shapes.
    """
    if isinstance(container, list):
        for item in container:
            yield from tasks(item)
    elif isinstance(container, dict):
        yield container
        for key in ('tasks', 'pre_tasks', 'post_tasks', 'handlers', 'block',
                    'rescue', 'always'):
            if key in container:
                yield from tasks(container[key])


def included_file(task):
    """Return the file an include_tasks/import_tasks task pulls in."""
    for key in ('include_tasks', 'import_tasks', 'include', 'ansible.builtin.include_tasks',
                'ansible.builtin.import_tasks'):
        if key not in task:
            continue
        value = task[key]
        if isinstance(value, dict):
            value = value.get('file')
        if isinstance(value, str):
            return value.strip()
    return None


def creates_instance(task):
    """True if this task asks Shaken Fist to create an instance.

    The same module deletes them, with a uuid and state: absent, so it is
    the absence of state: absent that separates the two. The module
    defaults state to present, so a task which creates an instance need
    not mention state at all -- reading a missing state as "not creating"
    would fail open, and let a new playbook skip the gate with this whole
    file still green.
    """
    for key, value in task.items():
        if key.split('.')[-1] != 'sf_instance':
            continue
        if isinstance(value, dict) and value.get('state', 'present') == 'present':
            return True
    return False


def add_host_targets(task):
    """Inventory names an add_host task makes available to later plays.

    Both the hostname and every group it joins, because a readiness play
    may target either. Templated names are dropped: they cannot be
    compared against a play's hosts: without an inventory to render them.
    """
    names = set()
    for key, value in task.items():
        if key.split('.')[-1] != 'add_host' or not isinstance(value, dict):
            continue
        hostname = value.get('hostname', value.get('name'))
        if isinstance(hostname, str):
            names.add(hostname)
        groups = value.get('groups', value.get('group'))
        if isinstance(groups, str):
            groups = groups.split(',')
        if isinstance(groups, list):
            names.update(g for g in groups if isinstance(g, str))
    return set(n.strip() for n in names if n.strip() and '{{' not in n)


def gate_covers(hosts, targets):
    """True if a readiness play's hosts: reaches any of these names.

    A creating play with no matchable target -- everything it adds is
    templated, and it joins no group -- falls back to being covered by
    any later gate play, because there is nothing to compare against.
    That is the old positional behaviour, kept for the case it is the
    only thing available rather than as the general rule.
    """
    if not targets:
        return True
    if not isinstance(hosts, str):
        return False
    # A pattern can combine names with commas, colons and the
    # intersection and exclusion prefixes.
    tokens = set(t for t in re.split(r'[\s,:&!]+', hosts) if t)
    return 'all' in tokens or bool(tokens & targets)


class ReadinessGateTest(unittest.TestCase):
    def setUp(self):
        self.docs = dict(documents())

        # Task files which create instances, so a play that only includes
        # one still counts as creating. kerbside-create-instance.yml is
        # the reason this indirection exists.
        self.creating_includes = set()
        for name, doc in self.docs.items():
            if any(creates_instance(t) for t in tasks(doc)):
                self.creating_includes.add(name)

    def plays(self, doc):
        """A playbook is a list of plays; a task file is not."""
        if not isinstance(doc, list):
            return []
        return [p for p in doc if isinstance(p, dict) and 'hosts' in p]

    def creating_include(self, task):
        """The creating task file this task pulls in, if it pulls one in."""
        included = included_file(task)
        if not included or '{{' in included:
            return None
        included = os.path.normpath(included)
        return included if included in self.creating_includes else None

    def play_creates(self, play):
        for task in tasks(play):
            if creates_instance(task) or self.creating_include(task):
                return True
        return False

    def play_targets(self, play):
        """Inventory names this play hands to the plays that follow it.

        Following creating includes matters: the kerbside playbooks do
        their add_host inside kerbside-create-instance.yml, so a play
        which only includes that file still names hosts.
        """
        names = set()
        for task in tasks(play):
            names |= add_host_targets(task)
            included = self.creating_include(task)
            if included:
                for inner in tasks(self.docs[included]):
                    names |= add_host_targets(inner)
        return names

    def play_gates(self, play):
        for task in tasks(play):
            if included_file(task) == GATE:
                return True
        return False

    def test_gate_file_exists(self):
        self.assertTrue(
            os.path.exists(os.path.join(ANSIBLE_DIR, GATE)),
            '%s is missing; every readiness play imports it' % GATE)

    def test_the_gate_still_waits(self):
        # Every other test here checks that playbooks import the gate by
        # path, which says nothing about what the gate does. Gutting
        # wait-for-cloud-init.yml down to the connection wait, or dropping
        # the cloud-init step, would leave all of them green while
        # removing the behaviour they exist to protect.
        with open(os.path.join(ANSIBLE_DIR, GATE)) as f:
            gate = yaml.safe_load(f)

        modules = set()
        commands = []
        for task in tasks(gate):
            for key, value in task.items():
                modules.add(key.split('.')[-1])
                if key.split('.')[-1] == 'command' and isinstance(value, str):
                    commands.append(value)

        self.assertIn(
            'wait_for_connection', modules,
            '%s no longer waits for an authenticated connection, so it no '
            'longer proves sshd has settled' % GATE)
        self.assertTrue(
            any('cloud-init status --wait' in c for c in commands),
            '%s no longer waits for cloud-init, which is the whole point '
            'of it; commands found were %s' % (GATE, commands))

    def test_every_creating_play_is_followed_by_the_gate(self):
        # Ordering matters as much as presence: a gate play before the
        # instances exist waits on the previous run's hosts, or on an
        # empty group, and passes for the wrong reason.
        #
        # So does which hosts the gate play targets. Matching creating
        # plays to gate plays by position alone would accept two gates
        # aimed at the same host -- ci-image.yml builds two independent
        # instances, so that is one copy-paste away -- and would reject a
        # single hosts: allsf gate covering two creating plays, which is
        # correct. Match on the names each play added instead.
        checked = 0
        for name, doc in sorted(self.docs.items()):
            pending = []
            for index, play in enumerate(self.plays(doc)):
                if self.play_creates(play):
                    pending.append((index, self.play_targets(play)))
                elif self.play_gates(play):
                    covered = [entry for entry in pending
                               if gate_covers(play.get('hosts'), entry[1])]
                    for entry in covered:
                        pending.remove(entry)
                    checked += len(covered)

            with self.subTest(playbook=name):
                self.assertEqual(
                    [], [index for index, _ in pending],
                    '%s creates instances in play(s) %s with no later '
                    'readiness play importing %s and targeting the hosts '
                    'they add. See the comment at the top of ansible/%s.'
                    % (name, ', '.join(str(i) for i, _ in pending), GATE,
                       GATE))

        # Guard against the whole test passing because the detection
        # stopped recognising anything -- a rename of the module or of
        # the gate file would otherwise make this silently vacuous.
        self.assertGreater(
            checked, 10,
            'only %d creating plays were matched to a readiness play; the '
            'detection has probably stopped recognising them' % checked)

    def test_creating_playbooks_are_the_expected_ones(self):
        # Names the set explicitly so that a playbook which stops
        # creating instances, or a new one that starts, is a deliberate
        # edit here rather than a silent change in what is covered.
        expected = {
            'ci-dependencies.yml',
            'ci-image-desktop.yml',
            'ci-image.yml',
            'ci-topology-localhost-released.yml',
            'ci-topology-localhost-upgrade.yml',
            'ci-topology-localhost.yml',
            'ci-topology-slim-primary-released.yml',
            'ci-topology-slim-primary.yml',
            'ci-topology-slim-tier.yml',
            'kerbside-multi-node-2.yml',
            'kerbside-multi-node.yml',
            'kerbside-single-node.yml',
        }
        found = set()
        for name, doc in self.docs.items():
            if any(self.play_creates(p) for p in self.plays(doc)):
                found.add(name)
        self.assertEqual(expected, found)

    def test_every_include_resolves(self):
        # A typo in an include path fails at run time on a real cluster,
        # which is the most expensive place to find it. Checking every
        # include rather than just the readiness one is what makes this
        # catch a misspelled gate: a misspelling stops the path being
        # the gate, so a gate-only check would skip the very task that
        # is broken.
        found = 0
        for name, doc in sorted(self.docs.items()):
            for task in tasks(doc):
                included = included_file(task)
                if not included or '{{' in included:
                    continue
                found += 1
                with self.subTest(playbook=name, included=included):
                    self.assertTrue(
                        os.path.exists(os.path.join(ANSIBLE_DIR, included)),
                        '%s includes %s, which does not exist under ansible/'
                        % (name, included))
        self.assertGreater(found, 10, 'no includes found to check')


if __name__ == '__main__':
    unittest.main()
