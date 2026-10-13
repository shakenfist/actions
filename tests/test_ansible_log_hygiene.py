#!/usr/bin/env python3

"""Tests that no playbook prints a whole hostvars entry into its log.

hostvars include every extra var, so a task that prints a host's whole
hostvars, such as a debug of {{hostvars['imagerebuild']}}, prints every
secret passed in as an extra var too. ci-dependencies.yml had exactly
that debug while the conductor was meant to pass it the GitLab testdata
token as an extra var; ci-instar-testdata.yml receives that token now,
and a failed build's log tail is filed as a public issue. Naming fields
is fine:
{{hostvars['imagerebuild']['sf_instance_uuid']}} prints one value.
Binding a whole entry to a variable that is only read field by field,
as tasks/wait-for-cloud-init.yml does, is fine too; what this holds is
that none reaches an output.

The outputs checked are the tasks whose arguments Ansible writes to the
log: debug, fail and assert print them, shell, command and raw show the
rendered command when they fail, and set_stats is printed at the end of
the run. A copy or template that writes a file is not checked.
"""

import glob
import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


SINKS = ('debug', 'fail', 'assert', 'shell', 'command', 'raw', 'set_stats')
NESTED = ('block', 'rescue', 'always')
PLAY_LISTS = ('tasks', 'pre_tasks', 'post_tasks', 'handlers')

# The bodies of {{ ... }} and {% ... %}; text outside them is literal.
TEMPLATE = re.compile(r'\{\{(.*?)\}\}|\{%(.*?)%\}', re.S)
# hostvars or vars as a name in their own right, not my_vars, x.vars or lookup('vars', 'x').
REFERENCE = re.compile(r'(?<![\w.\'"])(hostvars|vars)\b')
ATTRIBUTE = re.compile(r'\s*\.\s*(\w+)')
# How many subscripts or attributes select one field: hostvars['host']['field'] or vars['field'].
FIELD_DEPTH = {'hostvars': 2, 'vars': 1}


def closing_bracket(expression, start):
    """Return the index of the ] matching the [ at start, else None."""
    depth = 0
    quote = None
    for i in range(start, len(expression)):
        c = expression[i]
        if quote:
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return i
    return None


def accessors(expression, pos):
    """Count the subscripts and attribute reads that follow pos.

    A method call such as .items() ends the chain rather than counting,
    since it hands back the whole entry.
    """
    count = 0
    while True:
        rest = expression[pos:]
        pos += len(rest) - len(rest.lstrip())
        if expression.startswith('[', pos):
            end = closing_bracket(expression, pos)
            if end is None:
                return count
            pos = end + 1
        else:
            match = ATTRIBUTE.match(expression, pos)
            if not match or expression[match.end():].lstrip().startswith('('):
                return count
            pos = match.end()
        count += 1


def whole_references(expression):
    """Return each hostvars or vars reference in a Jinja expression that is not narrowed to one field."""
    found = []
    for match in REFERENCE.finditer(expression):
        if accessors(expression, match.end()) < FIELD_DEPTH[match.group(1)]:
            found.append(expression[match.start():].strip())
    return found


def prints_whole(text):
    """Return whether a templated string prints a whole hostvars entry or vars."""
    return any(whole_references(body) for pair in TEMPLATE.findall(text) for body in pair)


def sink_args(task):
    """Return the arguments of a task that writes them to the log, else None."""
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


def document_offences(document):
    """Return (task name, offending strings) for each task that prints a whole entry."""
    found = []
    for task in file_tasks(document):
        args = sink_args(task)
        if args is None:
            continue
        hits = [s for s in strings(args) if prints_whole(s)]
        # debug's var: takes a bare expression rather than a template.
        if isinstance(args, dict) and isinstance(args.get('var'), str) and whole_references(args['var']):
            hits.append(args['var'])
        if hits:
            found.append((task.get('name'), hits))
    return found


def offences(path):
    with open(path) as f:
        document = yaml.safe_load(f)
    return ['%s: %r prints %s' % (os.path.relpath(path, REPO_ROOT), name, hits)
            for name, hits in document_offences(document)]


class LogHygieneTests(unittest.TestCase):
    def test_no_task_prints_a_whole_hostvars_entry(self):
        paths = sorted(glob.glob(os.path.join(REPO_ROOT, 'ansible', '**', '*.yml'), recursive=True))
        self.assertGreater(len(paths), 0, 'no playbooks found; the scan is vacuous')
        found = []
        for path in paths:
            found.extend(offences(path))
        self.assertEqual([], found)

    def test_whole_entries_are_caught_in_every_form(self):
        for text in ("{{hostvars['imagerebuild']}}", '{{ hostvars[inventory_hostname] }}',
                     '{{hostvars}}', '{{ vars }}', "before {{hostvars['x']}} after",
                     "{{ hostvars['x'] | to_nice_json }}", '{{ vars | to_json }}',
                     '{{ hostvars.imagerebuild }}', "{{ hostvars[groups['a'][0]] }}",
                     "{{ hostvars['x'].items() | list }}", "{% for k in hostvars['x'] %}{{ k }}{% endfor %}",
                     "{{ hostvars['x' }}"):
            self.assertTrue(prints_whole(text), text)
        for text in ("hostvars['imagerebuild']", 'hostvars.x | to_json', 'vars'):
            self.assertTrue(whole_references(text), text)

    def test_named_fields_are_allowed(self):
        for text in ("{{hostvars['imagerebuild']['sf_instance_uuid']}}", "{{ hostvars['x'].ansible_host }}",
                     "{{ hostvars[groups['a'][0]]['ansible_host'] | default('') }}",
                     "{{ hostvars['x'] ['a]b'] }}", "{{ hostvars['['].ansible_host }}",
                     "{{ vars['x'] }}", '{{ vars.x }}', '{{ vars_path }}', '{{ my_vars }}', '{{ x.vars }}',
                     "{{ lookup('vars', 'x') }}", 'check hostvars by hand'):
            self.assertFalse(prints_whole(text), text)
        self.assertEqual([], whole_references("hostvars['imagerebuild']['sf_instance_uuid']"))

    def test_sinks_are_found_inside_blocks_and_fqcn(self):
        document = [{'hosts': 'all', 'tasks': [
            {'block': [{'name': 'msg', 'ansible.builtin.debug': {'msg': "{{hostvars['h']}}"}}],
             'rescue': [{'name': 'var', 'debug': {'var': "hostvars['h']"}}],
             'always': [{'name': 'cmd', 'ansible.builtin.shell': "echo {{ hostvars['h'] | to_json }}"},
                        {'name': 'quiet', 'set_fact': {'h': "{{ hostvars['h'] }}"}}]},
        ]}]
        self.assertEqual(['msg', 'var', 'cmd'], [name for name, _ in document_offences(document)])


if __name__ == '__main__':
    unittest.main()
