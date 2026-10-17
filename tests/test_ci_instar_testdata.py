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

import glob
import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT


PLAYBOOK = os.path.join(REPO_ROOT, 'ansible', 'ci-instar-testdata.yml')
DEPENDENCIES = os.path.join(REPO_ROOT, 'ansible', 'ci-dependencies.yml')
TOKEN = 'gitlab_testdata_token'
CREDENTIAL_FORM = 'oauth2'
# {{hostvars['x']}} or {{ hostvars }}, but not {{hostvars['x']['field']}}.
WHOLE_HOSTVARS = re.compile(r"\{\{\s*hostvars\s*(\[[^\]]*\]\s*)?\}\}")
TOKEN_ASSERT = 'Require a GitLab token for the instar-testdata clone'
CREATE_TASK = 'Create an instance'
UNMOUNT_TASK = 'Unmount the instar-testdata disk'
SCRUB_TASKS = [
    'Remove the credential from the instar-testdata remote',
    'Remove the credential from the instar-testdata lfs config',
    'Confirm no credential is left on the instar-testdata disk',
]
VERSION_TASK = 'Require an ansible-core whose get_url checks Content-Length'
MINIMUM = "ansible_version.full is version('2.19.0', '>=')"
LFS_PULL_TASK = 'Fetch the git-lfs content for instar-testdata'
FETCH_TASK = "Fetch instar's resparsify script"
RESPARSIFY_TASK = 'Resparsify instar-testdata images'
REFRESH_TASK = 'Refresh the instar-testdata index after resparsifying'
INDEX_CHECK_TASK = 'Confirm the instar-testdata index matches the working tree'
# develop by name: instar has no main, and raw.githubusercontent.com
# serves the default branch for a ref that does not exist.
RESPARSIFY_URL = 'https://raw.githubusercontent.com/shakenfist/instar/develop/tools/ci/resparsify-testdata.sh'
RESPARSIFY_SCRIPT = '/usr/local/bin/resparsify-testdata.sh'
TREE = '/srv/ci/cached/instar-testdata'


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
        # whatever runs after it, and the task reports success for a
        # step that did not happen.
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

    def test_resparsify_script_is_fetched_from_develop_with_get_url(self):
        # get_url, not uri: only get_url fails a download shorter than its
        # Content-Length, and a truncated script could exit zero having
        # rewritten nothing.
        task = all_tasks(self.plays)[find_task(self.plays, FETCH_TASK)]
        self.assertIn('get_url', task)
        self.assertEqual(RESPARSIFY_URL, task['get_url']['url'])
        self.assertEqual(RESPARSIFY_SCRIPT, task['get_url']['dest'])

    def test_version_is_asserted_before_the_instance_is_created(self):
        index = find_task(self.plays, VERSION_TASK)
        self.assertLess(index, find_task(self.plays, CREATE_TASK))
        self.assertIn(MINIMUM, all_tasks(self.plays)[index]['assert']['that'])

    def test_resparsify_is_called_with_only_the_tree(self):
        # The script's header fixes this form as the whole tree,
        # permanently; any further argument narrows what it rewrites.
        task = all_tasks(self.plays)[find_task(self.plays, RESPARSIFY_TASK)]
        self.assertEqual('%s %s' % (RESPARSIFY_SCRIPT, TREE), task['command'])
        self.assertEqual('debian', task.get('become_user'))

    def test_resparsify_runs_after_the_scrub_and_before_the_checks(self):
        # The script is fetched from another repository's branch, so it
        # must not run while the token is still on the disk: after both
        # credential removals. It must still run after the LFS pull, which
        # writes the images fully allocated, and before the checks, so
        # that the credential search and the free-space check see the
        # disk as it will be published.
        fetch = find_task(self.plays, FETCH_TASK)
        resparsify = find_task(self.plays, RESPARSIFY_TASK)
        self.assertLess(find_task(self.plays, LFS_PULL_TASK), fetch)
        self.assertLess(find_task(self.plays, SCRUB_TASKS[0]), fetch)
        self.assertLess(find_task(self.plays, SCRUB_TASKS[1]), fetch)
        self.assertLess(fetch, resparsify)
        self.assertLess(resparsify, find_task(self.plays, SCRUB_TASKS[2]))

    def test_index_is_refreshed_after_the_last_rewrite(self):
        # The resparsify gives every image a new inode and timestamps, so
        # unless the index is refreshed after it, every runner's first git
        # reset rewrites every image. Nothing may touch the tree between
        # the refresh and the check that the index is clean, and the check
        # must come before the disk is unmounted for publishing.
        resparsify = find_task(self.plays, RESPARSIFY_TASK)
        refresh = find_task(self.plays, REFRESH_TASK)
        check = find_task(self.plays, INDEX_CHECK_TASK)
        self.assertEqual(resparsify + 1, refresh)
        self.assertEqual(refresh + 1, check)
        self.assertLess(check, find_task(self.plays, UNMOUNT_TASK))
        task = all_tasks(self.plays)[refresh]
        self.assertIn('git update-index --refresh', task['shell'])
        self.assertEqual('debian', task.get('become_user'))
        task = all_tasks(self.plays)[check]
        self.assertEqual('git diff-files --quiet', task['command'])
        self.assertEqual('debian', task.get('become_user'))
        # A check changes nothing, like the other checks in the playbook.
        self.assertIs(False, task.get('changed_when'))

    def test_token_is_checked_before_the_instance_is_created(self):
        self.assertLess(
            find_task(self.plays, TOKEN_ASSERT), find_task(self.plays, CREATE_TASK))

    def test_token_assert_does_not_print_the_token(self):
        task = all_tasks(self.plays)[find_task(self.plays, TOKEN_ASSERT)]
        for key in ('fail_msg', 'success_msg'):
            self.assertNotIn(TOKEN, task['assert'].get(key, ''))

    def test_every_task_handling_the_credential_has_no_log(self):
        # A task carries the token if it names the variable, or if it
        # works on the oauth2: URL form the clone writes to disk: the lfs
        # scrub reads section names that hold the token without ever
        # naming the variable, and a failing git config prints them.
        checked = 0
        for task in all_tasks(self.plays):
            if task.get('name') == TOKEN_ASSERT:
                continue
            body = yaml.safe_dump(task)
            if TOKEN in body or CREDENTIAL_FORM in body:
                checked += 1
                self.assertIs(
                    True, task.get('no_log'),
                    '%r handles the credential but does not set no_log: true' % task.get('name'))
        self.assertGreaterEqual(
            checked, 3, 'fewer credential tasks than the clone, scrub and search; the detection is broken')

    def test_hostvars_are_never_dumped_whole(self):
        # hostvars include every extra var, the token among them, so
        # printing a host's whole hostvars prints the token into the log.
        for task in all_tasks(self.plays):
            body = yaml.safe_dump(task, width=1000)
            self.assertIsNone(
                WHOLE_HOSTVARS.search(body),
                '%r renders a whole hostvars entry, which includes the token' % task.get('name'))

    def test_credential_is_scrubbed_before_unmount(self):
        unmount = find_task(self.plays, UNMOUNT_TASK)
        for name in SCRUB_TASKS:
            self.assertLess(find_task(self.plays, name), unmount, name)

    def test_the_check_comes_after_the_scrub(self):
        # The grep only proves anything if it runs after the scrubbing.
        self.assertLess(
            find_task(self.plays, SCRUB_TASKS[1]), find_task(self.plays, SCRUB_TASKS[2]))

    def test_no_other_playbook_uses_the_token(self):
        # This is the only playbook that scrubs the credential before
        # snapshotting. ci-dependencies.yml once cloned testdata with
        # the token and kept it in .git/config on a disk every runner
        # attaches; a new user of the token needs this one's scrub.
        users = []
        for path in sorted(glob.glob(os.path.join(REPO_ROOT, 'ansible', '**', '*.yml'), recursive=True)):
            if path == PLAYBOOK:
                continue
            # Parsed, so comments explaining the token do not count.
            if TOKEN in yaml.safe_dump(load_plays(path), width=1000):
                users.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual([], users)


if __name__ == '__main__':
    unittest.main()
