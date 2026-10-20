#!/usr/bin/env python3

"""Tests for tools/ansible-syntax-check.sh.

The script is the only gate ansible/ has, and is run by hand rather than
from CI, so the failure worth pinning is a verdict that is not
ansible-playbook's: a failing check reported as anything but its own
status, or a passing check failed because the warning filter removed
every line of its output. ansible-playbook is stubbed on PATH, so these
run without ansible or its collections installed.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from tests.helpers import REPO_ROOT


SCRIPT = os.path.join(REPO_ROOT, 'tools', 'ansible-syntax-check.sh')
HOST_WARNING = '[WARNING]: Could not match supplied host pattern, ignoring: primary'


class AnsibleSyntaxCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

        self.playbook = os.path.join(self.tmp, 'play.yml')
        with open(self.playbook, 'w') as f:
            f.write('- hosts: primary\n  tasks: []\n')

        self.stubs = os.path.join(self.tmp, 'bin')
        os.makedirs(self.stubs)
        self.environment = dict(os.environ, PATH='%s:%s' % (self.stubs, os.environ['PATH']))

    def stub_ansible_playbook(self, output, status):
        path = os.path.join(self.stubs, 'ansible-playbook')
        with open(path, 'w') as f:
            f.write('#!/bin/bash\nprintf \'%%s\\n\' %s\nexit %d\n'
                    % (' '.join("'%s'" % line for line in output), status))
        os.chmod(path, 0o755)

    def run_check(self):
        return subprocess.run(['bash', SCRIPT, self.playbook], capture_output=True, text=True,
                              env=self.environment)

    def test_passing_check_passes_and_drops_the_host_warning(self):
        self.stub_ansible_playbook([HOST_WARNING, 'playbook: play.yml'], 0)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('playbook: play.yml', result.stdout)
        self.assertNotIn('Could not match supplied host pattern', result.stdout)

    def test_passing_check_whose_output_is_all_filtered_still_passes(self):
        self.stub_ansible_playbook([HOST_WARNING], 0)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failing_check_reports_its_own_status_and_output(self):
        self.stub_ansible_playbook([HOST_WARNING, 'ERROR! conflicting action statements'], 4)
        result = self.run_check()
        self.assertEqual(result.returncode, 4)
        self.assertIn('ERROR! conflicting action statements', result.stdout)
        self.assertNotIn('Could not match supplied host pattern', result.stdout)

    def test_no_playbooks_is_not_a_failure(self):
        tasks = os.path.join(self.tmp, 'tasks.yml')
        with open(tasks, 'w') as f:
            f.write('- name: a task file, not a play\n  debug: {}\n')
        result = subprocess.run(['bash', SCRIPT, tasks], capture_output=True, text=True,
                                env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No playbooks to check.', result.stdout)


if __name__ == '__main__':
    unittest.main()
