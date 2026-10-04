#!/usr/bin/env python3

"""Tests for the input validation in smoke-cluster.yml.

workflow_call does not validate string inputs, and every step in the
smoke job that depends on test_kind is gated on equality with a known
value. A value that matches none of those gates skips every test and
probe step after a full deploy and leaves a green job that tested
nothing. The "Validate inputs" step refuses such a value before the
deploy; these tests run that step's shell for real, and hold its list
of accepted kinds equal to the kinds the gates name, so that teaching
the workflow a third kind in one place and not the other fails here.
"""

import os
import re
import subprocess
import unittest

import yaml

from tests.helpers import REPO_ROOT


WORKFLOW = os.path.join(REPO_ROOT, '.github', 'workflows', 'smoke-cluster.yml')


class SmokeClusterInputsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW) as f:
            cls.text = f.read()
        cls.workflow = yaml.safe_load(cls.text)
        cls.steps = cls.workflow['jobs']['smoke_cluster']['steps']
        cls.names = [step.get('name') for step in cls.steps]
        cls.validate = cls.steps[cls.names.index('Validate inputs')]

    def run_validate(self, test_kind):
        environment = dict(os.environ)
        environment['TEST_KIND'] = test_kind
        return subprocess.run(
            ['bash', '-e', '-c', self.validate['run']], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=environment)

    def accepted_kinds(self):
        pattern = re.search(r'^\s*([\w|-]+)\) ;;$', self.validate['run'],
                            re.MULTILINE)
        return set(pattern.group(1).split('|'))

    def test_validation_runs_before_the_deploy(self):
        self.assertLess(self.names.index('Validate inputs'),
                        self.names.index('Build the smoke cluster'))

    def test_test_kind_reaches_the_shell_through_env(self):
        self.assertEqual(self.validate['env']['TEST_KIND'],
                         '${{ inputs.test_kind }}')
        self.assertNotIn('${{', self.validate['run'])

    def test_known_kinds_pass(self):
        for kind in ('functional', 'ansible-modules'):
            with self.subTest(kind=kind):
                result = self.run_validate(kind)
                self.assertEqual(result.returncode, 0, result.stdout)

    def test_unknown_kinds_fail_with_an_annotation(self):
        for kind in ('ansible_modules', 'functionals', '', 'functional '):
            with self.subTest(kind=kind):
                result = self.run_validate(kind)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('::error title=Unknown test_kind::',
                              result.stdout)

    def test_the_default_is_accepted(self):
        default = self.workflow[True]['workflow_call']['inputs'][
            'test_kind']['default']
        self.assertIn(default, self.accepted_kinds())

    def test_accepted_kinds_match_the_gates(self):
        # Every kind a gate compares against must be accepted, or that
        # gate's steps can never run; every accepted kind must be named by
        # some gate, or it is accepted only to skip every test.
        gated = set(re.findall(r"inputs\.test_kind == '([^']+)'", self.text))
        self.assertEqual(self.accepted_kinds(), gated)


if __name__ == '__main__':
    unittest.main()
