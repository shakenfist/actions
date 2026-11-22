#!/usr/bin/env python3

"""Tests for the stestr seeding in smoke-cluster.yml's functional step.

Before the suite runs, the step seeds stestr with the test durations
shakenfist keeps in its tree, so stestr balances its workers by time
rather than by count (shakenfist/shakenfist#5010). Three things must hold,
because this workflow is consumed @main by every branch at once: the seed
runs from the directory stestr runs in and before stestr does; a checkout
which predates shakenfist's tool skips it rather than failing; and a seed
which fails is a warning, never the end of the remote script, which runs
under set -e. These tests run the step's own fragment of shell for the
last two.
"""

import os
import re
import subprocess
import tempfile
import unittest

import yaml

from tests.helpers import REPO_ROOT


WORKFLOW = os.path.join(REPO_ROOT, '.github', 'workflows', 'smoke-cluster.yml')

# A stand-in for shakenfist's tools/ci_stestr_times.py, which records how
# it was called and exits with the status the test asks for.
FAKE_TOOL = """
import sys
with open('called', 'w') as f:
    f.write(' '.join(sys.argv[1:]))
sys.exit(int(open('status').read()))
"""


class SmokeClusterStestrSeedTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW) as f:
            workflow = yaml.safe_load(f)
        steps = workflow['jobs']['smoke_cluster']['steps']
        functional = [s for s in steps if s.get('name') == 'Run functional tests'][0]
        cls.lines = functional['run'].splitlines()

        def index_of(needle):
            matches = [i for i, line in enumerate(cls.lines) if needle in line]
            assert len(matches) == 1, (needle, matches)
            return matches[0]

        cls.cd_line = index_of('cd ~/shakenfist/shakenfist/deploy/;')
        cls.seed_line = index_of('ci_stestr_times.py')
        cls.run_line = index_of('stestr run ')
        m = re.match(r'^\s*script="\$\{script\} (.*)"$', cls.lines[cls.seed_line])
        assert m, cls.lines[cls.seed_line]
        cls.fragment = m.group(1)

    def test_the_seed_runs_where_stestr_runs_and_before_it(self):
        self.assertLess(self.cd_line, self.seed_line)
        self.assertLess(self.seed_line, self.run_line)
        # Nothing between the seed and the run may change directory, or the
        # seed would fill a repository stestr never opens.
        for line in self.lines[self.cd_line + 1:self.run_line]:
            self.assertNotRegex(line, r'\bcd\b')

    def run_fragment(self, tool_status=None):
        """Run the fragment under set -e from shakenfist/deploy in a fake tree.

        tool_status None means the checkout has no tool; otherwise the fake
        tool exits with that status. Returns the completed process and what
        the tool was called with, or None if it was not called.
        """
        with tempfile.TemporaryDirectory() as root:
            deploy = os.path.join(root, 'shakenfist', 'deploy')
            os.makedirs(deploy)
            if tool_status is not None:
                os.makedirs(os.path.join(root, 'tools'))
                with open(os.path.join(root, 'tools', 'ci_stestr_times.py'), 'w') as f:
                    f.write(FAKE_TOOL)
                with open(os.path.join(deploy, 'status'), 'w') as f:
                    f.write(str(tool_status))
            result = subprocess.run(
                ['bash', '-c', 'set -e; %s echo REACHED' % self.fragment],
                cwd=deploy, check=False, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            called = None
            if os.path.exists(os.path.join(deploy, 'called')):
                with open(os.path.join(deploy, 'called')) as f:
                    called = f.read()
            return result, called

    def test_a_checkout_without_the_tool_skips_the_seed(self):
        result, called = self.run_fragment(tool_status=None)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('REACHED', result.stdout)
        self.assertIsNone(called)
        self.assertNotIn('WARNING', result.stdout)

    def test_a_successful_seed_is_quiet_and_the_suite_runs(self):
        result, called = self.run_fragment(tool_status=0)
        self.assertEqual('seed', called)
        self.assertIn('REACHED', result.stdout)
        self.assertNotIn('WARNING', result.stdout)

    def test_a_failed_seed_warns_and_the_suite_still_runs(self):
        result, called = self.run_fragment(tool_status=1)
        self.assertEqual('seed', called)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('WARNING: stestr was not seeded', result.stdout)
        self.assertIn('REACHED', result.stdout)


if __name__ == '__main__':
    unittest.main()
