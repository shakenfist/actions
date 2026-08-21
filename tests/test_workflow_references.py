#!/usr/bin/env python3

"""Tests that workflows only name things which exist.

A workflow that dispatches a missing file, or reads a tool from a
directory its checkout never fetched, fails at the moment somebody is
relying on it and not before. That is not hypothetical here: the shared
template these bot workflows came from dispatches
`functional-tests.yml`, which this repository does not have, and the
whole reason they were deployed at all is that a missing
`pr-re-review.yml` let two pull requests merge with their review fixes
unreviewed. Nothing else in CI looks at these files beyond their YAML
syntax.
"""

import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


WORKFLOW_DIR = os.path.join(REPO_ROOT, '.github', 'workflows')


def workflows():
    for name in sorted(os.listdir(WORKFLOW_DIR)):
        if name.endswith('.yml') or name.endswith('.yaml'):
            path = os.path.join(WORKFLOW_DIR, name)
            with open(path) as f:
                yield name, f.read()


class DispatchTargetTest(unittest.TestCase):
    def test_every_dispatched_workflow_exists(self):
        # `gh workflow run X` on a name GitHub does not know fails at
        # run time with "could not find any workflows named X", which
        # the caller reports as a dispatch failure rather than a bug.
        found = False
        for name, text in workflows():
            for target in re.findall(r'gh workflow run\s+(\S+)', text):
                found = True
                with self.subTest(workflow=name, target=target):
                    self.assertTrue(
                        os.path.exists(os.path.join(WORKFLOW_DIR, target)),
                        '%s dispatches %s, which does not exist in '
                        '.github/workflows/' % (name, target))
        self.assertTrue(found, 'no gh workflow run calls found to check')


class LocalReusableWorkflowTest(unittest.TestCase):
    def test_every_relative_uses_target_exists(self):
        # Relative `uses:` is how ci.yml reviews its own reviewer and
        # how canary.yml calls the smoke lane. A typo here is a workflow
        # that never starts.
        found = False
        for name, text in workflows():
            for target in re.findall(r'uses:\s+(\./\S+)', text):
                found = True
                with self.subTest(workflow=name, target=target):
                    self.assertTrue(
                        os.path.exists(os.path.join(REPO_ROOT, target)),
                        '%s uses %s, which does not exist' % (name, target))
        self.assertTrue(found, 'no relative uses: targets found to check')


class AddressCommentsCheckoutTest(unittest.TestCase):
    """The trusted-tools checkout must fetch what the step then reads.

    pr-address-comments.yml deliberately runs the base branch's copy of
    the tooling rather than the pull request's, so the sparse checkout
    list and the directories TOOLS_DIR and SCRIPT_DIR point into have to
    agree. They are written far apart in the file and neither actionlint
    nor a YAML parse would notice them drifting.
    """

    WORKFLOW = os.path.join(WORKFLOW_DIR, 'pr-address-comments.yml')

    def setUp(self):
        with open(self.WORKFLOW) as f:
            self.text = f.read()
        self.parsed = yaml.safe_load(self.text)

    def sparse_paths(self):
        for job in self.parsed['jobs'].values():
            for step in job.get('steps', []):
                sparse = (step.get('with') or {}).get('sparse-checkout')
                if sparse:
                    return sparse.split()
        self.fail('no sparse-checkout found in %s' % self.WORKFLOW)

    def test_the_directories_read_from_the_trusted_checkout_are_fetched(self):
        sparse = self.sparse_paths()
        # Each of these is "${{ github.workspace }}/trusted-tools/<dir>".
        used = re.findall(
            r'(?:TOOLS_DIR|SCRIPT_DIR):.*?/trusted-tools/(\S+)', self.text)
        self.assertTrue(used, 'no trusted-tools directories referenced')
        for directory in used:
            with self.subTest(directory=directory):
                self.assertIn(
                    directory, sparse,
                    '%s is read from the trusted checkout but is not in its '
                    'sparse-checkout list, so the directory will be empty'
                    % directory)

    def test_the_sparse_checkout_directories_exist_in_the_repository(self):
        for directory in self.sparse_paths():
            with self.subTest(directory=directory):
                self.assertTrue(
                    os.path.isdir(os.path.join(REPO_ROOT, directory)),
                    'sparse-checkout names %s, which is not a directory in '
                    'this repository' % directory)

    def test_render_review_sits_beside_its_schema_in_the_tools_dir(self):
        # render-review.py resolves SCHEMA_PATH as
        # Path(__file__).parent / 'review-schema.json'. Point TOOLS_DIR at
        # a directory holding the script but not the schema and
        # load_schema() returns None, validate_review() silently drops to
        # structural checks, and every review validates -- including ones
        # the schema would reject. That is the failure this repository
        # avoids by not copying the script into tools/.
        tools_dir = re.search(
            r'TOOLS_DIR:.*?/trusted-tools/(\S+)', self.text).group(1)
        for filename in ('render-review.py', 'review-schema.json'):
            with self.subTest(filename=filename):
                self.assertTrue(
                    os.path.exists(
                        os.path.join(REPO_ROOT, tools_dir, filename)),
                    'TOOLS_DIR points at %s, which has no %s'
                    % (tools_dir, filename))


if __name__ == '__main__':
    unittest.main()
