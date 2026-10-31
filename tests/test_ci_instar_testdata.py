#!/usr/bin/env python3

"""Tests for the structure of the instar-testdata disk build.

ci-instar-testdata.yml clones a private repository with a GitLab token
and publishes the resulting disk to every runner whose job asks for the
`instar` label, including runners running pull request code. Three
things about it are worth holding in place structurally, because a
regression in any of them is silent until a runner is already holding
the result: the filesystem format must not drift from the dependencies
disk's, the token must never reach a build log, and the credential must
never be written to the disk at all, because a credential removed from
a file survives in the filesystem's freed blocks.
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
STAGE_TASK = 'Stage the GitLab credential in memory for the clone'
CLONE_TASK = 'Clone instar-testdata from GitLab'
REMOVE_TASK = 'Remove the staged GitLab credential'
METADATA_SEARCH_TASK = 'Search the git metadata for the credential'
METADATA_ASSERT_TASK = 'Confirm no credential is left in the instar-testdata git metadata'
RAW_TOKEN_SEARCH_TASK = 'Search the raw instar-testdata disk for the token'
RAW_TOKEN_ASSERT_TASK = 'Confirm the token is nowhere on the raw instar-testdata disk'
BACKSTOP_TASK = 'Search the raw instar-testdata disk for a credential URL'
BACKSTOP_ASSERT_TASK = 'Confirm no credential URL is on the raw instar-testdata disk'
# The ansible modules whose free-form argument becomes a command line.
COMMAND_MODULES = ('shell', 'command', 'raw')
# The modules that fetch something from outside this repository.
DOWNLOAD_MODULES = ('get_url', 'uri', 'unarchive', 'git')
# The URL form a credential takes when it is embedded in a remote.
EMBEDDED_CREDENTIAL = re.compile(r'://oauth2:')
# git -c before the subcommand applies to one run; clone -c or --config
# would write the setting into the clone's .git/config, on the disk.
HELPER_ARGS = 'git -c credential.helper= -c "credential.helper={{ testdata_credential_helper }}"'
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


def build_play(plays):
    """Return the play that builds the disk on the builder."""
    for play in plays:
        if any(task.get('name') == CLONE_TASK for task in play.get('tasks', [])):
            return play
    raise AssertionError('no play clones instar-testdata')


def first_download(plays):
    """Return the index, in playbook order, of the first task on the builder that downloads a file.

    apt's signed packages aside, anything downloaded is code or data from
    outside this repository, so this is where the tasks that may carry the
    token must already have finished, whatever the download is called.
    """
    play_tasks = build_play(plays)['tasks']
    offset = all_tasks(plays).index(play_tasks[0])
    for index, task in enumerate(play_tasks):
        if any(key.split('.')[-1] in DOWNLOAD_MODULES for key in task):
            return offset + index
    raise AssertionError('no task downloads anything')


def command_strings(task):
    """Return every command line a task runs, as written in the playbook."""
    found = []
    for module in COMMAND_MODULES:
        value = task.get(module)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            found.extend(v for v in value.values() if isinstance(v, str))
    return found


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

    def test_resparsify_runs_after_the_credential_is_gone(self):
        # The script is fetched from another repository's branch and runs
        # as a user who can sudo, so it must not run while the token is
        # anywhere on the builder: after the staged credential is removed,
        # and after both searches that need the token. It must still run
        # after the LFS pull, which writes the images fully allocated, and
        # before the checks, so that they see the disk as it will be
        # published.
        fetch = first_download(self.plays)
        self.assertEqual(find_task(self.plays, FETCH_TASK), fetch)
        resparsify = find_task(self.plays, RESPARSIFY_TASK)
        self.assertLess(find_task(self.plays, LFS_PULL_TASK), find_task(self.plays, REMOVE_TASK))
        for name in (REMOVE_TASK, METADATA_SEARCH_TASK, METADATA_ASSERT_TASK,
                     RAW_TOKEN_SEARCH_TASK, RAW_TOKEN_ASSERT_TASK):
            self.assertLess(find_task(self.plays, name), fetch, name)
        self.assertLess(fetch, resparsify)
        self.assertLess(resparsify, find_task(self.plays, UNMOUNT_TASK))

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
        # A failed task prints its arguments, stdin included, into the
        # build log. The backstop after the unmount names the oauth2: URL
        # form in a pattern but holds no token, so it is not one of these.
        checked = 0
        for task in all_tasks(self.plays):
            if task.get('name') == TOKEN_ASSERT:
                continue
            if TOKEN in yaml.safe_dump(task):
                checked += 1
                self.assertIs(
                    True, task.get('no_log'),
                    '%r handles the credential but does not set no_log: true' % task.get('name'))
        self.assertGreaterEqual(
            checked, 3, 'fewer credential tasks than the staging and the two searches; the detection is broken')

    def test_no_command_line_carries_the_credential(self):
        # A command line is visible in the builder's process list, and git
        # writes a URL given on its command line into the clone's
        # .git/config, on the disk being published. The token reaches the
        # builder only on a task's stdin.
        checked = 0
        for task in all_tasks(self.plays):
            for command in command_strings(task):
                checked += 1
                self.assertNotIn(TOKEN, command, '%r puts the token on a command line' % task.get('name'))
                self.assertIsNone(
                    EMBEDDED_CREDENTIAL.search(command),
                    '%r puts a credential URL on a command line' % task.get('name'))
        self.assertGreater(checked, 10, 'too few command lines found; the scan is vacuous')

    def test_no_task_passes_the_token_in_its_environment(self):
        # ansible sends a task's environment as a prefix on the remote
        # command line, so it is no less visible than an argument.
        for play in self.plays:
            self.assertNotIn(TOKEN, yaml.safe_dump(play.get('environment', {})))
        for task in all_tasks(self.plays):
            self.assertNotIn(
                TOKEN, yaml.safe_dump(task.get('environment', {})),
                '%r passes the token in its environment' % task.get('name'))

    def test_no_task_after_the_fetch_carries_the_token(self):
        # The resparsify script runs as debian, who can sudo, so from the
        # fetch on, anything a task carries could be read by whatever it
        # left behind: not as an argument, not in the environment, and not
        # on stdin either. The same holds for anything else downloaded, so
        # this counts from the first download, whatever it is called.
        tasks = all_tasks(self.plays)
        after = tasks[first_download(self.plays):]
        self.assertGreater(len(after), 10)
        for task in after:
            self.assertNotIn(
                TOKEN, yaml.safe_dump(task, width=1000),
                '%r runs after the resparsify fetch and carries the token' % task.get('name'))

    def test_the_credential_is_staged_off_the_disk(self):
        # The only place the token is written is a file on tmpfs, which is
        # never snapshotted, in a directory only debian can enter. Nothing
        # about it may be under the disk's mount point.
        play = build_play(self.plays)
        directory = play['vars']['testdata_credential_dir']
        self.assertTrue(directory.startswith('/dev/shm/'), directory)
        self.assertFalse(directory.startswith(TREE), directory)
        self.assertEqual('store --file={{ testdata_credential_dir }}/store',
                         play['vars']['testdata_credential_helper'])
        task = all_tasks(self.plays)[find_task(self.plays, STAGE_TASK)]
        script = task['shell']
        self.assertIn('umask 077', script)
        self.assertIn('{{ testdata_credential_dir }}', script)
        self.assertNotIn(TREE, script)
        self.assertNotIn('chdir', task.get('args', {}))
        self.assertIn(TOKEN, task['args']['stdin'])
        self.assertEqual('debian', task.get('become_user'))
        self.assertLess(find_task(self.plays, STAGE_TASK), find_task(self.plays, CLONE_TASK))

    def test_git_gets_the_credential_from_the_helper_for_one_run(self):
        # The clone (whose checkout runs the system-wide LFS filter) and
        # the LFS pull both need the credential, from the helper, named
        # before the subcommand so nothing is written into .git/config.
        for name in (CLONE_TASK, LFS_PULL_TASK):
            command = ' '.join(all_tasks(self.plays)[find_task(self.plays, name)]['command'].split())
            self.assertTrue(command.startswith(HELPER_ARGS + ' '), '%r: %r' % (name, command))
        clone = ' '.join(all_tasks(self.plays)[find_task(self.plays, CLONE_TASK)]['command'].split())
        self.assertIn(' https://gitlab.home.stillhq.com/private/instar-testdata.git %s' % TREE, clone)

    def test_the_staged_credential_is_removed(self):
        task = all_tasks(self.plays)[find_task(self.plays, REMOVE_TASK)]
        self.assertEqual({'path': '{{ testdata_credential_dir }}', 'state': 'absent'}, task['file'])

    def test_modules_are_pipelined_where_the_token_is_handled(self):
        # Unpipelined, every module's arguments, the token among them, are
        # written to a file on the builder's boot disk and deleted, which
        # leaves them in freed blocks the resparsify script could read.
        self.assertIs(True, build_play(self.plays)['vars'].get('ansible_pipelining'))

    def test_raw_token_search_reads_the_whole_device_before_the_fetch(self):
        task = all_tasks(self.plays)[find_task(self.plays, RAW_TOKEN_SEARCH_TASK)]
        script = task['shell']
        self.assertIn('sync', script.splitlines())
        self.assertIn('blockdev --flushbufs /dev/vdc', script)
        self.assertIn('grep -qazF -f - /dev/vdc', script)
        self.assertEqual('{{ gitlab_testdata_token }}', task['args']['stdin'])
        self.assertIs(True, task.get('no_log'))
        self.assertEqual(find_task(self.plays, RAW_TOKEN_SEARCH_TASK) + 1,
                         find_task(self.plays, RAW_TOKEN_ASSERT_TASK))
        assertion = all_tasks(self.plays)[find_task(self.plays, RAW_TOKEN_ASSERT_TASK)]['assert']
        self.assertEqual(["raw_token_search.stdout == 'clean'"], assertion['that'])

    def test_raw_device_backstop_runs_after_the_unmount(self):
        # The last check reads every block of the disk as it will be
        # snapshotted, after the unmount, and the build fails before the
        # snapshot play if it finds a credential URL. It holds no secret
        # and prints only its outcome, never what it matched.
        unmount = find_task(self.plays, UNMOUNT_TASK)
        backstop = find_task(self.plays, BACKSTOP_TASK)
        self.assertEqual(unmount + 1, backstop)
        self.assertEqual(backstop + 1, find_task(self.plays, BACKSTOP_ASSERT_TASK))
        self.assertIs(build_play(self.plays), next(
            play for play in self.plays if any(t.get('name') == BACKSTOP_TASK for t in play.get('tasks', []))))
        task = all_tasks(self.plays)[backstop]
        script = task['shell']
        self.assertNotIn(TOKEN, yaml.safe_dump(task, width=1000))
        self.assertIn('blockdev --flushbufs /dev/vdc', script)
        self.assertIn(r"grep -qazE 'oauth2:[^@[:space:]]*@gitlab\.home\.stillhq\.com' /dev/vdc", script)
        echoes = re.findall(r'\becho\b(.*)', script)
        self.assertEqual([' found ;;', ' clean ;;'], echoes)
        assertion = all_tasks(self.plays)[backstop + 1]['assert']
        self.assertEqual(["raw_url_search.stdout == 'clean'"], assertion['that'])

    def test_hostvars_are_never_dumped_whole(self):
        # hostvars include every extra var, the token among them, so
        # printing a host's whole hostvars prints the token into the log.
        for task in all_tasks(self.plays):
            body = yaml.safe_dump(task, width=1000)
            self.assertIsNone(
                WHOLE_HOSTVARS.search(body),
                '%r renders a whole hostvars entry, which includes the token' % task.get('name'))

    def test_the_metadata_check_comes_after_the_credential_is_removed(self):
        # The grep only proves anything once the clone and the pull are done.
        remove = find_task(self.plays, REMOVE_TASK)
        search = find_task(self.plays, METADATA_SEARCH_TASK)
        self.assertLess(remove, search)
        self.assertEqual(search + 1, find_task(self.plays, METADATA_ASSERT_TASK))
        task = all_tasks(self.plays)[search]
        self.assertIn('grep -rlF --exclude-dir=objects -f - .git', task['shell'])
        self.assertEqual('oauth2\n{{ gitlab_testdata_token }}', task['args']['stdin'])

    def test_no_other_playbook_uses_the_token(self):
        # This is the only playbook that keeps the credential off the
        # disk it snapshots. ci-dependencies.yml once cloned testdata
        # with the token and kept it in .git/config on a disk every
        # runner attaches; a new user of the token needs this one's care.
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
