#!/usr/bin/env python3

"""Tests that no playbook prints a whole hostvars entry into its log.

hostvars include every extra var, so a task that prints a host's whole
hostvars, such as a debug of {{hostvars['imagerebuild']}}, prints every
secret passed in as an extra var too. ci-dependencies.yml did this with
the GitLab token on every nightly build, and the conductor files a
failed build's log tail as a public issue. Naming fields is fine:
{{hostvars['imagerebuild']['sf_instance_uuid']}} prints one value.
Binding a whole entry to a variable that is only read field by field,
as tasks/wait-for-cloud-init.yml does, is fine too; what this holds is
that none reaches an output.
"""

import glob
import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


# {{hostvars['x']}}, {{ hostvars[inventory_hostname] }}, {{hostvars}} and
# {{vars}}, but not {{hostvars['x']['field']}}.
WHOLE = re.compile(r'\{\{\s*(hostvars\s*(\[[^\]]*\]\s*)?|vars\s*)\}\}')
# debug's var: takes a bare expression rather than a template.
WHOLE_VAR = re.compile(r'^\s*(hostvars\s*(\[[^\]]*\]\s*)?|vars\s*)$')
SINKS = ('debug', 'fail', 'assert')
NESTED = ('block', 'rescue', 'always')
PLAY_LISTS = ('tasks', 'pre_tasks', 'post_tasks', 'handlers')


def sink_args(task):
    """Return the arguments of a debug, fail or assert task, else None."""
    for key, value in task.items():
        if key.split('.')[-1] in SINKS:
            return value
    return None


def walk_tasks(items):
    """Yield every task in a list of tasks, descending into blocks."""
    for task in items or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in NESTED:
            yield from walk_tasks(task.get(key))


def file_tasks(document):
    """Yield every task in a playbook or a task file."""
    if not isinstance(document, list):
        return
    for item in document:
        if isinstance(item, dict) and ('hosts' in item or 'import_playbook' in item):
            for key in PLAY_LISTS:
                yield from walk_tasks(item.get(key))
        else:
            yield from walk_tasks([item])


def strings(value):
    """Yield every string in a nested structure of task arguments."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def offences(path):
    with open(path) as f:
        document = yaml.safe_load(f)
    found = []
    for task in file_tasks(document):
        args = sink_args(task)
        if args is None:
            continue
        hits = [s for s in strings(args) if WHOLE.search(s)]
        if isinstance(args, dict) and isinstance(args.get('var'), str) and WHOLE_VAR.match(args['var']):
            hits.append(args['var'])
        if hits:
            found.append('%s: %r prints %s' % (os.path.relpath(path, REPO_ROOT), task.get('name'), hits))
    return found


class LogHygieneTests(unittest.TestCase):
    def test_no_task_prints_a_whole_hostvars_entry(self):
        paths = sorted(glob.glob(os.path.join(REPO_ROOT, 'ansible', '**', '*.yml'), recursive=True))
        self.assertGreater(len(paths), 0, 'no playbooks found; the scan is vacuous')
        found = []
        for path in paths:
            found.extend(offences(path))
        self.assertEqual([], found)

    def test_the_patterns_catch_the_forms_they_mean_to(self):
        for text in ("{{hostvars['imagerebuild']}}", '{{ hostvars[inventory_hostname] }}',
                     '{{hostvars}}', '{{ vars }}', "before {{hostvars['x']}} after"):
            self.assertIsNotNone(WHOLE.search(text), text)
        for text in ("{{hostvars['imagerebuild']['sf_instance_uuid']}}", "{{ hostvars['x'].ansible_host }}",
                     '{{ vars_path }}'):
            self.assertIsNone(WHOLE.search(text), text)
        self.assertIsNotNone(WHOLE_VAR.match("hostvars['imagerebuild']"))
        self.assertIsNone(WHOLE_VAR.match("hostvars['imagerebuild']['sf_instance_uuid']"))

    def test_sinks_are_found_inside_blocks_and_fqcn(self):
        document = [{'hosts': 'all', 'tasks': [
            {'block': [{'name': 'inner', 'ansible.builtin.debug': {'msg': "{{hostvars['h']}}"}}]},
        ]}]
        tasks = [t for t in file_tasks(document) if sink_args(t) is not None]
        self.assertEqual(['inner'], [t['name'] for t in tasks])


if __name__ == '__main__':
    unittest.main()
