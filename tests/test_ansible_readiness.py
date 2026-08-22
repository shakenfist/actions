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
import unittest

import yaml

from tests.helpers import REPO_ROOT


ANSIBLE_DIR = os.path.join(REPO_ROOT, 'ansible')
GATE = 'tasks/wait-for-cloud-init.yml'


def documents():
    """Yield (name, parsed) for every YAML file directly in ansible/."""
    for name in sorted(os.listdir(ANSIBLE_DIR)):
        if not name.endswith('.yml'):
            continue
        with open(os.path.join(ANSIBLE_DIR, name)) as f:
            yield name, yaml.safe_load(f)


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

    The same module deletes them, with a uuid and state: absent, so the
    state is what separates the two.
    """
    for key, value in task.items():
        if key.split('.')[-1] != 'sf_instance':
            continue
        if isinstance(value, dict) and value.get('state') == 'present':
            return True
    return False


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

    def play_creates(self, play):
        for task in tasks(play):
            if creates_instance(task):
                return True
            included = included_file(task)
            if included and os.path.basename(included) in self.creating_includes:
                return True
        return False

    def play_gates(self, play):
        for task in tasks(play):
            if included_file(task) == GATE:
                return True
        return False

    def test_gate_file_exists(self):
        self.assertTrue(
            os.path.exists(os.path.join(ANSIBLE_DIR, GATE)),
            '%s is missing; every readiness play imports it' % GATE)

    def test_every_creating_play_is_followed_by_the_gate(self):
        # Ordering matters as much as presence: a gate play before the
        # instances exist waits on the previous run's hosts, or on an
        # empty group, and passes for the wrong reason.
        checked = 0
        for name, doc in sorted(self.docs.items()):
            plays = self.plays(doc)
            pending = []
            for index, play in enumerate(plays):
                if self.play_creates(play):
                    pending.append(index)
                elif self.play_gates(play) and pending:
                    pending.pop(0)
                    checked += 1

            with self.subTest(playbook=name):
                self.assertEqual(
                    [], pending,
                    '%s creates instances in play(s) %s with no readiness '
                    'play importing %s after them. See the comment at the '
                    'top of ansible/%s.'
                    % (name, ', '.join(str(i) for i in pending), GATE, GATE))

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
