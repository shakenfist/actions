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

import json
import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


ANSIBLE_DIR = os.path.join(REPO_ROOT, 'ansible')
GATE = 'tasks/wait-for-cloud-init.yml'
PREREQUISITES = 'tasks/install-kolla-prerequisites.yml'


def documents():
    """Yield (path relative to ansible/, parsed) for every YAML file under it.

    Recursive, and keyed on the relative path rather than the basename, so
    that a task file which creates instances is recognised wherever it
    lives and so include paths can be compared without flattening them.
    """
    for root, _, names in os.walk(ANSIBLE_DIR):
        for name in sorted(names):
            # Ansible is equally happy with either spelling, and a
            # provisioning playbook added as .yaml would otherwise be
            # invisible to every check in this file without tripping any
            # of the vacuity guards.
            if not name.endswith(('.yml', '.yaml')):
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


# Everything Ansible lets a task carry that is not the module it runs.
# Subtracting these from a task's keys leaves the modules, which is how
# the gate checks below tell "this task calls setup" apart from "this task
# has a when". A keyword missing from this set reads as a module and shows
# up as a failure naming it, which is the safe direction to be wrong in.
TASK_KEYWORDS = frozenset((
    'always', 'any_errors_fatal', 'args', 'async', 'become', 'become_flags',
    'become_method', 'become_user', 'block', 'changed_when', 'check_mode',
    'collections', 'connection', 'debugger', 'delay', 'delegate_facts',
    'delegate_to', 'environment', 'failed_when', 'ignore_errors',
    'ignore_unreachable', 'import_playbook', 'import_role', 'import_tasks',
    'include', 'include_role', 'include_tasks', 'listen', 'local_action',
    'loop', 'loop_control', 'module_defaults', 'name', 'no_log', 'notify',
    'poll', 'port', 'register', 'remote_user', 'rescue', 'retries',
    'run_once', 'tags', 'throttle', 'until', 'vars', 'when',
))


def modules_in(task):
    """The modules a task calls, keyed by short name.

    Short names because a task may spell a module either way, and the
    checks here care which module it is rather than how it was written.
    A block carries no module of its own and so yields nothing.
    """
    return {key.split('.')[-1]: value for key, value in task.items()
            # Every with_* lookup form rather than the with_items entry
            # this used to carry: they are all keywords, and naming one
            # of them meant a task using another would be reported as
            # running a module called "with_dict".
            if key.split('.')[-1] not in TASK_KEYWORDS
            and not key.split('.')[-1].startswith('with_')}


def command_string(value):
    """What a command module task runs, whichever way it was spelled.

    free form, cmd: and argv: are all the same question, and a check
    which only understood one of them would go quietly vacuous the first
    time the gate changed spelling.
    """
    if isinstance(value, dict):
        value = value.get('cmd', value.get('argv'))
    if isinstance(value, list):
        value = ' '.join(str(item) for item in value)
    return value if isinstance(value, str) else ''


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
        if not isinstance(value, dict):
            continue
        state = value.get('state', 'present')
        # A templated state is neither missing nor 'present', and reading
        # it as "not creating" would drop the playbook out of every check
        # here -- the same fail-open this function exists to avoid. Only
        # a literal state: absent excludes a task.
        if not isinstance(state, str) or '{{' in state or state == 'present':
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
    # hosts: [allsf] is as valid as hosts: allsf. Rejecting the list form
    # would report a missing gate for a playbook whose gate is sitting
    # right there, which is the worst thing this check could say.
    if isinstance(hosts, list):
        hosts = ','.join(h for h in hosts if isinstance(h, str))
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

    def gate_tasks(self):
        with open(os.path.join(ANSIBLE_DIR, GATE)) as f:
            return list(tasks(yaml.safe_load(f)))

    def test_the_gate_still_waits(self):
        # Every other test here checks that playbooks import the gate by
        # path, which says nothing about what the gate does. Gutting
        # wait-for-cloud-init.yml down to the connection wait, or dropping
        # the cloud-init step, would leave all of them green while
        # removing the behaviour they exist to protect.
        probes = []
        waits = []
        for task in self.gate_tasks():
            for name, value in modules_in(task).items():
                if name == 'command':
                    probes.append(command_string(value))
                elif name == 'raw' and isinstance(value, str):
                    waits.append(value)

        # The probe is an ssh login that runs something trivial: that is
        # what "authenticated" means here, and checking for the login
        # rather than for a module name is what lets the gate change
        # mechanism again without this test having to be rewritten.
        self.assertTrue(
            any('ssh ' in p and 'BatchMode=yes' in p for p in probes),
            '%s no longer opens an authenticated ssh connection, so it no '
            'longer proves sshd has settled; commands found were %s'
            % (GATE, probes))
        # Without a retry loop the probe is a single attempt that fails
        # the play on the first refused connection -- the sshd restart
        # window this gate exists to sit through. Without the outer
        # timeout it is unbounded in wall clock, which is how the budget
        # in docs/ansible.md stops being true: a retry count bounds the
        # number of attempts, not the time they take.
        for probe in probes:
            with self.subTest(probe=probe):
                self.assertRegex(
                    probe, r'^timeout \d+ ',
                    '%s has an ssh probe with no wall-clock bound, so the '
                    'gate can outlast the budget docs/ansible.md gives it'
                    % GATE)
                self.assertIn(
                    'sleep', probe,
                    '%s has an ssh probe that does not retry, so it fails '
                    'on the first refused connection' % GATE)
                # A folded block scalar keeps the newline on any line
                # indented further than its first, and the probe is a
                # shell script, where a newline ends one command and
                # starts another. Written that way the probe runs "ssh"
                # with half its arguments, fails, and loops on that for
                # its whole budget against a guest which is up -- which
                # is a day lost to a failure that looks exactly like the
                # one this gate was rewritten to fix.
                self.assertNotIn(
                    '\n', probe,
                    '%s has an ssh probe containing a newline. Keep every '
                    'line of cmd at the same indentation, and hoist any '
                    'wrapped expression into vars.' % GATE)

        # The probes were made identical by hoisting their connection
        # details into per-task vars, which moves the drift this check
        # exists to stop rather than removing it.
        probe_vars = [task.get('vars') for task in self.gate_tasks()
                      if 'command' in modules_in(task)]
        self.assertEqual(
            1, len({json.dumps(v, sort_keys=True) for v in probe_vars}),
            '%s builds its two ssh probes from different vars, so they no '
            'longer probe the same way: %s' % (GATE, probe_vars))
        self.assertTrue(
            any('cloud-init status --wait' in w for w in waits),
            '%s no longer waits for cloud-init, which is the whole point '
            'of it; raw commands found were %s' % (GATE, waits))
        # The first attempt and the retry spell each command out
        # separately, so changing the timeout in one and not the other
        # would otherwise leave this file green.
        for label, found in (('ssh probe', probes), ('cloud-init wait', waits)):
            self.assertEqual(
                1, len(set(found)),
                '%s runs more than one distinct %s; the retry is meant to be '
                'the same wait as the first attempt, and these have drifted: '
                '%s' % (GATE, label, sorted(set(found))))

    def test_the_gate_runs_no_module_on_the_target(self):
        # The gate has to work on a guest Ansible cannot manage. A module
        # is delivered as a wrapper which needs Python 3.9 or newer on the
        # managed node, and the oVirt lane's Rocky 8 guest has 3.6, so
        # every module there dies in the wrapper. That is what took the
        # merge queue down for six runs: wait_for_connection's probe is
        # the ping module, so the gate spent its whole timeout failing on
        # a guest that was answering ssh perfectly well
        # (shakenfist/kerbside#446).
        #
        # raw needs no Python because it is just a command down the ssh
        # pipe. debug and set_fact are action plugins, evaluated on the
        # controller. command is a module, so it is only allowed where it
        # runs on the controller too, which is what delegate_to says.
        for task in self.gate_tasks():
            for name in modules_in(task):
                if name in ('raw', 'debug', 'set_fact'):
                    continue
                with self.subTest(task=task.get('name'), module=name):
                    self.assertEqual(
                        'localhost', task.get('delegate_to'),
                        '%s runs the %s module on the target in task "%s". '
                        'Only raw, debug, set_fact, and modules delegated to '
                        'localhost are safe here; see the comment at the top '
                        'of ansible/%s.'
                        % (GATE, name, task.get('name'), GATE))

    def test_the_kolla_probe_runs_no_module_on_the_target(self):
        # The gate is not the only thing that meets a guest Ansible
        # cannot manage. PREREQUISITES runs on the same group one play
        # later, so its package-manager probe has to ask the question
        # without a module for the same reason (shakenfist/kerbside#446).
        # The two install tasks it guards are deliberately modules -- but
        # they only run on hosts the probe found apt on, which are
        # Debian, so the invariant is the probe's alone.
        probes = [task for task in tasks(self.docs[PREREQUISITES])
                  if task.get('register') == 'apt_get_probe']

        self.assertEqual(
            1, len(probes),
            'ansible/%s no longer has exactly one task registering '
            'apt_get_probe, so this check no longer knows which task to '
            'hold to the invariant' % PREREQUISITES)
        self.assertEqual(
            ['raw'], sorted(modules_in(probes[0])),
            'ansible/%s asks which package manager the host has with '
            'something other than raw. A module needs Python 3.9 or newer '
            'on the managed node and this play meets the oVirt lane\'s '
            'Rocky 8 guest, where it does not exist; see '
            'shakenfist/kerbside#446 and ansible/%s.'
            % (PREREQUISITES, GATE))

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
                # Ansible resolves a relative include against the
                # directory of the file doing the including, so check
                # that first. Everything in the tree today is a playbook
                # at the ansible/ root, where the two are the same, but
                # this change creates ansible/tasks/ as a place task
                # files live next to each other.
                base = os.path.dirname(os.path.join(ANSIBLE_DIR, name))
                candidates = [os.path.join(base, included),
                              os.path.join(ANSIBLE_DIR, included)]
                with self.subTest(playbook=name, included=included):
                    self.assertTrue(
                        any(os.path.exists(c) for c in candidates),
                        '%s includes %s, which does not exist beside it or '
                        'under ansible/' % (name, included))
        self.assertGreater(found, 10, 'no includes found to check')


if __name__ == '__main__':
    unittest.main()
