#!/usr/bin/env python3

"""Tests for tools/run_remote.

Only the local branches are exercised. The ssh path needs a reachable
node and the CI key, so it cannot run here -- but the local branches are
the ones that quietly stopped working when the exec arguments were
quoted, because ${primary} is an egress IP in every CI caller and
neither branch matches. Nothing else in the tree would have caught it.
"""

import os
import socket
import subprocess
import unittest

from tests.helpers import REPO_ROOT


RUN_REMOTE = os.path.join(REPO_ROOT, 'tools', 'run_remote')


class LocalExecTest(unittest.TestCase):
    def run_remote(self, *argv):
        result = subprocess.run(
            [RUN_REMOTE] + list(argv), cwd=REPO_ROOT, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(
            result.returncode, 0,
            'run_remote %s exited %d: %s' % (
                argv, result.returncode, result.stderr.strip()))
        return result.stdout

    def test_a_command_passed_as_separate_arguments_runs(self):
        self.assertEqual(self.run_remote('localhost', 'echo', 'hi'), 'hi\n')

    def test_a_command_passed_as_one_string_runs(self):
        # This is how nearly every caller invokes it -- smoke-cluster.yml
        # passes "sudo bash tools/ci_drain_check.sh" as a single argument
        # and relies on word splitting here to turn it into an argv.
        # Quoting the exec arguments makes argv[0] the whole string and
        # the exec fails with "No such file or directory".
        self.assertEqual(self.run_remote('localhost', 'echo hi'), 'hi\n')

    def test_the_hostname_branch_behaves_like_the_localhost_branch(self):
        # The two branches are separate code paths, so a fix applied to
        # one and not the other would pass a localhost-only test.
        self.assertEqual(
            self.run_remote(socket.gethostname(), 'echo hi'), 'hi\n')

    def test_arguments_are_passed_through_to_the_command(self):
        self.assertEqual(
            self.run_remote('localhost', 'echo one two three'),
            'one two three\n')
