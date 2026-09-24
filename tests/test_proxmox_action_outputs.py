#!/usr/bin/env python3

"""deploy-proxmox-on-shakenfist's outputs are maintained by hand, twice.

action.yml declares each output as `${{ steps.publish.outputs.X }}`, and
tools/proxmox-publish-node.sh is the "publish" step: it writes the
GITHUB_OUTPUT lines that back those references. Nothing ties the two
lists together, so a rename or an addition on one side silently breaks
the action's contract with a consumer -- ryll's proxmox-functional.yml
today, and kerbside's Proxmox source lane later -- rather than failing a
test here.
"""

import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


ACTION_YAML = os.path.join(REPO_ROOT, 'deploy-proxmox-on-shakenfist', 'action.yml')
PUBLISH_SCRIPT = os.path.join(REPO_ROOT, 'tools', 'proxmox-publish-node.sh')

# What steps.publish.outputs.X looks like in action.yml's own output
# values, so a value of a different shape (a typo, a different step id)
# is caught rather than silently read as "no output".
OUTPUT_REFERENCE = re.compile(
    r'\$\{\{\s*steps\.publish\.outputs\.(\w+)\s*\}\}')


def declared_outputs():
    with open(ACTION_YAML) as f:
        parsed = yaml.safe_load(f)
    outputs = parsed['outputs']
    declared = {}
    for name, spec in outputs.items():
        match = OUTPUT_REFERENCE.fullmatch(spec['value'].strip())
        assert match, (
            '%s output %r has value %r, not a bare '
            'steps.publish.outputs.X reference' % (ACTION_YAML, name, spec))
        declared[name] = match.group(1)
    return declared


def published_outputs():
    # The GITHUB_OUTPUT block is the literal
    #     {
    #         echo "key=value"
    #         ...
    #     } >> "${GITHUB_OUTPUT}"
    # found by its exact closing line, then walked back to the nearest
    # bare "{" -- not a brace-matching regex, because the block's own
    # values are full of ${...} bash expansions that a naive [^{}]*
    # character class would trip over.
    with open(PUBLISH_SCRIPT) as f:
        lines = f.readlines()
    end = next(i for i, line in enumerate(lines)
               if line.strip() == '} >> "${GITHUB_OUTPUT}"')
    start = max(i for i in range(end) if lines[i].strip() == '{')
    block = ''.join(lines[start + 1:end])
    return re.findall(r'^\s*echo "(\w+)=', block, re.MULTILINE)


class OutputContractTest(unittest.TestCase):
    def test_every_declared_output_names_a_key_the_script_publishes(self):
        published = set(published_outputs())
        for output_name, output_key in declared_outputs().items():
            with self.subTest(output=output_name, key=output_key):
                self.assertIn(
                    output_key, published,
                    'action.yml output %r reads '
                    'steps.publish.outputs.%s, but proxmox-publish-node.sh '
                    'never writes a %r GITHUB_OUTPUT line'
                    % (output_name, output_key, output_key))

    def test_every_published_key_is_a_declared_output(self):
        # The converse: a key the script writes but action.yml never
        # exposes is at best dead and at worst a rename that missed one
        # side.
        declared_keys = set(declared_outputs().values())
        for key in published_outputs():
            with self.subTest(key=key):
                self.assertIn(
                    key, declared_keys,
                    'proxmox-publish-node.sh writes a %r GITHUB_OUTPUT '
                    'line, but no output in action.yml reads '
                    'steps.publish.outputs.%s' % (key, key))

    def test_the_two_lists_are_not_accidentally_both_empty(self):
        self.assertTrue(declared_outputs())
        self.assertTrue(published_outputs())


if __name__ == '__main__':
    unittest.main()
