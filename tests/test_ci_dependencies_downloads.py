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


if __name__ == '__main__':
    unittest.main()
