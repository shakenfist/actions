#!/usr/bin/env python3

"""Tests that the dependencies disk's downloads stay verified.

ci-dependencies.yml fills the cache disk every runner mounts, and a
download cut off part way through is the failure worth guarding: the
short file is published and then fails, much later, as a confusing boot
error in whichever lane uses that image (issue #82). What catches it is
get_url itself, which from ansible-core 2.19.0 fails a transfer that
delivers fewer bytes than its Content-Length. Nothing in the playbook
names that check, so it is easy to lose without noticing, in two ways:
by moving the downloads to the uri module, which takes a dest too but
saves a truncated body and reports success, or by running the playbook
under an older ansible-core. These tests hold both in place.
"""

import os
import unittest

import yaml

from tests.helpers import REPO_ROOT


PLAYBOOK = os.path.join(REPO_ROOT, 'ansible', 'ci-dependencies.yml')
CACHE_TASK = 'Cache all minimal images we currently build to reduce network traffic'
VERSION_TASK = 'Require an ansible-core whose get_url checks Content-Length'
MANIFEST_TASK = 'Write the action archive manifest'
ARCHIVE_TASK = 'Cache action archives, without failing the build'
SUMMARY_TASK = 'Parse the action archive summary'
WARN_TASK = 'Warn about action archives that could not be cached'
FLOOR_TASK = 'Look for cache entries that arrived empty or truncated'
MINIMUM = "ansible_version.full is version('2.19.0', '>=')"


def load_plays():
    with open(PLAYBOOK) as f:
        return yaml.safe_load(f)


def find_task(plays, name):
    """Return (play index, task index, task) for the task with this name."""
    for play_index, play in enumerate(plays):
        for task_index, task in enumerate(play.get('tasks', [])):
            if task.get('name') == name:
                return play_index, task_index, task
    raise AssertionError('%s has no task named %r' % (PLAYBOOK, name))


class CacheDownloadTests(unittest.TestCase):
    def setUp(self):
        self.plays = load_plays()

    def test_downloads_use_get_url(self):
        _, _, task = find_task(self.plays, CACHE_TASK)
        modules = {'get_url', 'ansible.builtin.get_url'}
        self.assertTrue(
            modules & set(task),
            'The cache downloads must stay get_url: it is what fails a download '
            'shorter than its Content-Length, and uri does not.')

    def test_version_is_asserted_first(self):
        # First, so that an old controller fails before it has created an
        # instance, and not after spending the downloads it cannot verify.
        play_index, task_index, task = find_task(self.plays, VERSION_TASK)
        self.assertEqual((0, 0), (play_index, task_index))
        self.assertIn(MINIMUM, task['assert']['that'])


class ActionArchiveTests(unittest.TestCase):
    def setUp(self):
        self.plays = load_plays()

    def test_script_task_cannot_fail_the_build(self):
        _, _, task = find_task(self.plays, ARCHIVE_TASK)
        script = task.get('script')
        self.assertIsNotNone(script, 'The archives must be fetched by the script module.')
        self.assertIn('cache-action-archives.py', script)
        self.assertIs(False, task.get('failed_when'),
                      'A missed archive costs a job one codeload request, but a failed nightly '
                      'rebuild costs every image its freshness, so this task must not fail the build.')

    def test_script_task_follows_the_image_cache_in_the_same_play(self):
        image_play, image_index, _ = find_task(self.plays, CACHE_TASK)
        script_play, script_index, _ = find_task(self.plays, ARCHIVE_TASK)
        manifest_play, manifest_index, _ = find_task(self.plays, MANIFEST_TASK)
        self.assertEqual(image_play, script_play)
        self.assertEqual(image_play, manifest_play)
        self.assertGreater((script_play, script_index), (image_play, image_index))
        self.assertGreater((script_play, script_index), (manifest_play, manifest_index))

    def test_manifest_defaults_to_empty(self):
        _, _, task = find_task(self.plays, MANIFEST_TASK)
        self.assertIn('default([])', task['copy']['content'],
                      'A conductor that predates action_archives sends nothing; that must be an empty list.')

    def test_wiring_agrees_with_itself(self):
        _, _, manifest = find_task(self.plays, MANIFEST_TASK)
        _, _, task = find_task(self.plays, ARCHIVE_TASK)
        self.assertEqual('python3', task['args']['executable'])
        script = task['script']
        self.assertIn('/tmp/action-archives.json', script)
        self.assertIn('/srv/ci/cached/action-archive-cache', script)
        self.assertIn(manifest['copy']['dest'], script.split())
        self.assertIn('--budget 600', script, 'The bound on the whole run should be visible here.')

    def test_summary_is_parsed_from_the_last_line_only(self):
        # ssh runs the command under a tty, which merges stderr into stdout, so the diagnostics come
        # before the JSON summary and parsing all of stdout fails exactly when there are failures.
        play_index, archive_index, _ = find_task(self.plays, ARCHIVE_TASK)
        for task in self.plays[play_index]['tasks'][archive_index:]:
            text = yaml.safe_dump(task)
            self.assertNotRegex(text, r'stdout\s*\|\s*from_json',
                                '%r parses all of stdout, which includes stderr under ssh.' % task.get('name'))
        _, _, task = find_task(self.plays, SUMMARY_TASK)
        value = task['set_fact']['action_archives_summary']
        self.assertIn('stdout_lines', value)
        self.assertIn('from_json', value)
        self.assertIs(True, task.get('ignore_errors'), 'Parsing the summary must not fail the build.')

    def test_warning_reads_the_parsed_summary(self):
        _, _, task = find_task(self.plays, WARN_TASK)
        text = yaml.safe_dump(task)
        self.assertIn('action_archives_summary', text)
        self.assertNotIn('action_archives_result.stdout', text)

    def test_failed_archive_warning_cannot_fail_the_build(self):
        _, _, task = find_task(self.plays, WARN_TASK)
        self.assertIn('debug', task)
        self.assertNotIn('failed_when', task)
        self.assertIs(True, task.get('ignore_errors'),
                      'A warning must not be able to fail the build it is warning about.')
        self.assertNotIn('fail', task)
        self.assertNotIn('assert', task)

    def test_size_floor_does_not_recurse(self):
        _, _, task = find_task(self.plays, FLOOR_TASK)
        self.assertFalse(
            task['find'].get('recurse', False),
            "The size floor must stay non-recursive: actions/checkout's archive is about 465 KiB, "
            'under its 1 MB floor, and only the action-archive-cache subdirectory keeps it from failing the build.')


if __name__ == '__main__':
    unittest.main()
