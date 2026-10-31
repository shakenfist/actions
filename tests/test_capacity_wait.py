#!/usr/bin/env python3

"""Tests that the playbooks which wait for capacity count it correctly.

Each playbook gives its run one budget, capacity_wait_budget, and every
sf_instance create is handed what is left of it as capacity_wait. What
each create spent comes back as capacity_waited.seconds and is added to
the sf_capacity_waited fact by the task straight after it. The pattern
is copied by hand once per create, and its two easy mistakes are both
silent. A create without capacity_wait just does not wait. A count task
copied from its neighbour and still reading the neighbour's register
adds nothing, because of its default(0), so the run quietly waits past
its budget. Neither shows until the cluster is short of room, and
nothing can exercise these playbooks before merge (see .github/workflows/ci.yml),
so a structural check is the only net there is -- as it is for the
cloud-init gate in test_ansible_readiness.py.
"""

import os
import re
import unittest

import jinja2
import yaml

from tests.helpers import REPO_ROOT


ANSIBLE_DIR = os.path.join(REPO_ROOT, 'ansible')
DOCS = os.path.join(REPO_ROOT, 'docs', 'ansible.md')
MODULE = 'shakenfist.shakenfist.sf_instance'
INCLUDE = 'kerbside-create-instance.yml'
NODE_INCLUDE = 'tasks/create-topology-node.yml'

# The playbooks that set a budget, and the task files that create on
# their behalf. Every other playbook is left alone on purpose: the
# -released and -upgrade topologies are run by no workflow, and the
# image-build playbooks belong to the conductor's ci-images builder.
BUDGETED = (
    'ci-topology-localhost.yml',
    'ci-topology-slim-tier.yml',
    'ci-topology-slim-primary.yml',
    'kerbside-single-node.yml',
    'kerbside-multi-node.yml',
    'kerbside-multi-node-2.yml',
)
CREATING = (
    'ci-topology-localhost.yml',
    INCLUDE,
    NODE_INCLUDE,
)
# Budgeted playbooks which create only through one of those task files.
CREATES_THROUGH = {
    'ci-topology-slim-primary.yml': NODE_INCLUDE,
    'ci-topology-slim-tier.yml': NODE_INCLUDE,
    'kerbside-single-node.yml': INCLUDE,
    'kerbside-multi-node.yml': INCLUDE,
    'kerbside-multi-node-2.yml': INCLUDE,
}


def load(name):
    with open(os.path.join(ANSIBLE_DIR, name)) as f:
        return yaml.safe_load(f)


def task_lists(container):
    """Yield every list of tasks inside a playbook or task file, in order.

    Order matters here, because the count must be the task straight after
    its create, so this yields whole lists rather than walking tasks.
    """
    if isinstance(container, list):
        if all(isinstance(t, dict) for t in container):
            yield container
        for item in container:
            yield from task_lists(item)
    elif isinstance(container, dict):
        for key in ('tasks', 'pre_tasks', 'post_tasks', 'handlers',
                    'block', 'rescue', 'always'):
            if key in container:
                yield from task_lists(container[key])


def creates(name):
    """(task, the task after it or None) for each present sf_instance."""
    found = []
    for tasks in task_lists(load(name)):
        for index, task in enumerate(tasks):
            args = task.get(MODULE)
            if not isinstance(args, dict):
                continue
            if args.get('state', 'present') != 'present':
                continue
            following = tasks[index + 1] if index + 1 < len(tasks) else None
            found.append((task, following))
    return found


def builtin(task, module):
    """A task's arguments to an ansible.builtin module, under its short or its full name."""
    return task.get(module) or task.get('ansible.builtin.' + module)


def documented_budgets():
    """The playbook -> budget table under "Waiting for capacity"."""
    with open(DOCS) as f:
        text = f.read()
    section = text.split('## Waiting for capacity', 1)[1].split('\n## ', 1)[0]
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r'^\| `([\w.-]+\.yml)` \| (\d+) \|$', section, re.MULTILINE)}


class CapacityWaitTest(unittest.TestCase):

    def test_every_create_is_given_what_is_left_of_the_budget(self):
        for name in CREATING:
            found = creates(name)
            self.assertTrue(found, '%s has no sf_instance create; the list here is stale' % name)
            for task, _ in found:
                with self.subTest(playbook=name, task=task.get('name')):
                    self.assertIn('capacity_wait', task[MODULE])
                    self.assertIn('capacity_wait_budget', str(task[MODULE]['capacity_wait']))
                    self.assertIn('sf_capacity_waited', str(task[MODULE]['capacity_wait']))
                    self.assertIn('register', task)

    def test_every_create_is_counted_by_the_task_after_it(self):
        for name in CREATING:
            for task, following in creates(name):
                with self.subTest(playbook=name, task=task.get('name')):
                    self.assertIsNotNone(following, 'nothing counts what this create waited')
                    facts = following.get('set_fact') or following.get('ansible.builtin.set_fact')
                    self.assertIsInstance(facts, dict, 'the task after the create is not a set_fact')
                    self.assertIn('sf_capacity_waited', facts)
                    counted = re.findall(r'(\w+)\.capacity_waited\.seconds', str(facts['sf_capacity_waited']))
                    self.assertEqual([task['register']], counted,
                                     'the count reads %s, not this create\'s register' % counted)

    def test_every_topology_node_create_awaits_placement(self):
        """Each create passes await_placement when the collection has it, and omits it otherwise.

        An unawaited create cannot replay a preflight capacity abort, so a slim build refused at
        preflight fails at the SSH banner wait instead (#207). But the collection is built from the
        job's shakenfist checkout, and one without the option rejects it even when it is false, so
        a literal true would fail every slim build on such a branch. The include asks ansible-doc
        first and passes the option only when it is there.
        """
        found = creates(NODE_INCLUDE)
        self.assertTrue(found, '%s has no sf_instance create; the list here is stale' % NODE_INCLUDE)

        tasks = load(NODE_INCLUDE)
        asks = [t for t in tasks
                if 'ansible-doc' in str(builtin(t, 'command')) and MODULE in str(builtin(t, 'command'))]
        self.assertEqual(1, len(asks), 'the include does not ask ansible-doc whether %s has await_placement, '
                                       'so it cannot tell whether passing the option is safe' % MODULE)
        records = [builtin(t, 'set_fact') for t in tasks
                   if 'sf_instance_has_await_placement' in (builtin(t, 'set_fact') or {})]
        self.assertEqual(1, len(records), 'nothing sets sf_instance_has_await_placement from the ansible-doc answer')
        recorded = str(records[0]['sf_instance_has_await_placement'])
        self.assertIn(asks[0]['register'], recorded, 'the fact is not computed from the ansible-doc task\'s output')
        self.assertIn("'await_placement'", recorded, 'the fact does not look for await_placement in the options')

        # Rendered with plain Jinja2 and stand-ins for Ansible's omit
        # sentinel and its bool filter, as below.
        environment = jinja2.Environment()
        environment.filters['bool'] = lambda v: str(v).lower() in ('true', 'yes', 'on', '1')
        omit = '__omit_place_holder__'
        for task, _ in found:
            with self.subTest(task=task.get('name')):
                self.assertLess(tasks.index(asks[0]), tasks.index(task), 'the create runs before the question')
                value = task[MODULE].get('await_placement')
                self.assertIsInstance(value, str,
                                      'await_placement must be passed when the collection has it, or an unawaited '
                                      'create cannot replay a preflight capacity abort (#207), and omitted when it '
                                      'has not, or every slim build against such a collection fails; a literal '
                                      'cannot do both')
                self.assertIn('sf_instance_has_await_placement', value)
                self.assertIn('omit', value)
                template = environment.from_string(value.strip())
                self.assertEqual('True', template.render(omit=omit, sf_instance_has_await_placement=True).strip())
                self.assertEqual(omit, template.render(omit=omit, sf_instance_has_await_placement=False).strip())

    def test_each_budget_matches_the_documented_table(self):
        documented = documented_budgets()
        self.assertEqual(sorted(BUDGETED), sorted(documented))
        for name in BUDGETED:
            with self.subTest(playbook=name):
                budgets = [play['vars']['capacity_wait_budget'] for play in load(name)
                           if 'capacity_wait_budget' in (play.get('vars') or {})]
                self.assertEqual([documented[name]], budgets)

    def test_playbooks_create_through_their_counting_include(self):
        self.assertTrue(set(CREATES_THROUGH) <= set(BUDGETED))
        for name, include in CREATES_THROUGH.items():
            with self.subTest(playbook=name):
                self.assertFalse(creates(name), 'creates directly rather than through %s' % include)
                with open(os.path.join(ANSIBLE_DIR, name)) as f:
                    self.assertIn(include, f.read())

    def test_no_other_playbook_passes_the_option(self):
        passing = set()
        for root, _, names in os.walk(ANSIBLE_DIR):
            for n in names:
                if n.endswith(('.yml', '.yaml')):
                    with open(os.path.join(root, n)) as f:
                        if re.search(r'^\s*capacity_wait:', f.read(), re.MULTILINE):
                            passing.add(os.path.relpath(os.path.join(root, n), ANSIBLE_DIR))
        self.assertEqual(set(CREATING), passing)

    def test_the_include_omits_the_option_without_a_positive_budget(self):
        # Rendered with plain Jinja2 and a stand-in for Ansible's omit
        # sentinel: int and max are Jinja2 builtins, so the expression
        # means the same thing here as it does under Ansible.
        (task, _), = creates(INCLUDE)
        template = jinja2.Environment().from_string(str(task[MODULE]['capacity_wait']).strip())
        omit = '__omit_place_holder__'
        cases = (
            ({}, omit),
            ({'capacity_wait_budget': 0}, omit),
            ({'capacity_wait_budget': '0'}, omit),
            ({'capacity_wait_budget': 1800}, '1800'),
            ({'capacity_wait_budget': 1800, 'sf_capacity_waited': 300}, '1500'),
            ({'capacity_wait_budget': 600, 'sf_capacity_waited': 900}, '0'),
        )
        for variables, expected in cases:
            with self.subTest(**{k: str(v) for k, v in variables.items()}):
                self.assertEqual(expected, template.render(omit=omit, **variables).strip())


if __name__ == '__main__':
    unittest.main()
