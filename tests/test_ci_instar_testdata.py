#!/usr/bin/env python3

"""Tests for the structure of the instar-testdata disk build.

ci-instar-testdata.yml clones a private repository with a GitLab token
and publishes the resulting disk to every runner whose job asks for the
`instar` label, including runners running pull request code. Three
things about it are worth holding in place structurally, because a
regression in any of them is silent until a runner is already holding
the result: the filesystem format must not drift from the dependencies
disk's, the token must never reach a build log, and the credential must
be scrubbed before the disk is unmounted and snapshotted.
"""

import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


PLAYBOOK = os.path.join(REPO_ROOT, 'ansible', 'ci-instar-testdata.yml')
DEPENDENCIES = os.path.join(REPO_ROOT, 'ansible', 'ci-dependencies.yml')
TOKEN = 'gitlab_testdata_token'
TOKEN_ASSERT = 'Require a GitLab token for the instar-testdata clone'
CREATE_TASK = 'Create an instance'
UNMOUNT_TASK = 'Unmount the instar-testdata disk'
SCRUB_TASKS = [
    'Remove the credential from the instar-testdata remote',
    'Remove the credential from the instar-testdata lfs config',
    'Confirm no credential is left on the instar-testdata disk',
]


def load_plays(path):
    with open(path) as f:
        return yaml.safe_load(f)


def all_tasks(plays):
    """Return a flat list of every task, in playbook order."""
    return [task for play in plays for task in play.get('tasks', [])]


def find_task(plays, name):
    """Return the index, in playbook order, of the task with this name."""
    for index, task in enumerate(all_tasks(plays)):
        if task.get('name') == name:
            return index
    raise AssertionError('no task named %r' % name)


def mkfs_features(plays):
    """Return the -O feature list of the mkfs.ext4 command in a playbook."""
    found = []
    for task in all_tasks(plays):
        command = task.get('shell') or task.get('command')
        if isinstance(command, str) and 'mkfs.ext4' in command:
            match = re.search(r'-O\s+(\S+)', command)
            if match:
                found.append(match.group(1))
    if len(found) != 1:
        raise AssertionError('expected exactly one mkfs.ext4 -O, found %r' % found)
    return found[0]


class InstarTestdataTests(unittest.TestCase):
    def setUp(self):
        self.plays = load_plays(PLAYBOOK)

    def test_feature_list_matches_the_dependencies_disk(self):
        # Both disks are mounted across the same span of kernels, and the
        # reasoning for the list lives in ci-dependencies.yml.
        self.assertEqual(
            mkfs_features(load_plays(DEPENDENCIES)), mkfs_features(self.plays))

    def test_filesystem_is_labelled(self):
        # The runner mounts by LABEL=instar-testdata, not by position.
        checked = 0
        for task in all_tasks(self.plays):
            command = task.get('shell') or task.get('command')
            if isinstance(command, str) and 'mkfs.ext4' in command:
                checked += 1
                self.assertIn('-L instar-testdata', command)
                self.assertIn('/dev/vdc', command)
        self.assertEqual(1, checked, 'expected exactly one mkfs.ext4 task')

    def test_multi_line_scripts_fail_on_error(self):
        # A multi-line script runs its commands in sequence, and without
        # errexit and pipefail a failure part way through is hidden by
        # whatever runs after it: a failed cp in the resparsify loop would
        # otherwise be moved over the image it was copying.
        checked = 0
        for task in all_tasks(self.plays):
            script = task.get('shell')
            if isinstance(script, str) and len(script.strip().splitlines()) > 1:
                checked += 1
                self.assertEqual(
                    'set -euo pipefail', script.strip().splitlines()[0],
                    '%r does not start with set -euo pipefail' % task.get('name'))
                self.assertEqual(
                    '/bin/bash', task.get('args', {}).get('executable'),
                    '%r needs bash for pipefail' % task.get('name'))
        self.assertGreater(checked, 0, 'no multi-line shell task found; the detection is vacuous')

    def test_token_is_checked_before_the_instance_is_created(self):
        self.assertLess(
            find_task(self.plays, TOKEN_ASSERT), find_task(self.plays, CREATE_TASK))

    def test_token_assert_does_not_print_the_token(self):
        task = all_tasks(self.plays)[find_task(self.plays, TOKEN_ASSERT)]
        for key in ('fail_msg', 'success_msg'):
            self.assertNotIn(TOKEN, task['assert'].get(key, ''))

    def test_every_task_naming_the_token_has_no_log(self):
        checked = 0
        for task in all_tasks(self.plays):
            if task.get('name') == TOKEN_ASSERT:
                continue
            if TOKEN in yaml.safe_dump(task):
                checked += 1
                self.assertIs(
                    True, task.get('no_log'),
                    '%r names the token but does not set no_log: true' % task.get('name'))
        self.assertGreater(checked, 0, 'no task names the token; the detection is vacuous')

    def test_credential_is_scrubbed_before_unmount(self):
        unmount = find_task(self.plays, UNMOUNT_TASK)
        for name in SCRUB_TASKS:
            self.assertLess(find_task(self.plays, name), unmount, name)

    def test_the_check_comes_after_the_scrub(self):
        # The grep only proves anything if it runs after the scrubbing.
        self.assertLess(
            find_task(self.plays, SCRUB_TASKS[1]), find_task(self.plays, SCRUB_TASKS[2]))


if __name__ == '__main__':
    unittest.main()
