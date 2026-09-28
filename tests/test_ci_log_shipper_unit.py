#!/usr/bin/env python3

"""Tests that the CI runner log shipper cannot hold up boot.

The Alloy unit waits for an sfcbr-* hostname before it runs Alloy, and a
host which is not a runner never gets one. The runner images are also the
under-cloud images for the multi-node smoke topologies, so most hosts
that boot them are exactly that case. While the poll sat in ExecStartPre
the unit's start job lasted twenty minutes, multi-user.target waited for
it, cloud-final.service on debian waited for multi-user.target, and every
readiness gate hit its ten minute cap -- issue #113. Nothing failed, so
nothing noticed. The unit only runs on a real image boot, which no
pre-merge check does, so check its shape instead.
"""

import configparser
import os
import unittest

import yaml

from tests.helpers import REPO_ROOT


TASKS = os.path.join(REPO_ROOT, 'ansible', 'tasks', 'install-ci-log-shipper.yml')
UNIT = '/etc/systemd/system/alloy.service'


def unit():
    """Parse the alloy.service content out of the install task file."""
    with open(TASKS) as f:
        tasks = yaml.safe_load(f)
    contents = [
        t['ansible.builtin.copy']['content'] for t in tasks
        if t.get('ansible.builtin.copy', {}).get('dest') == UNIT]
    if len(contents) != 1:
        raise AssertionError(f'expected one task installing {UNIT}, found {len(contents)}')

    # strict=False because systemd allows a key to repeat; interpolation
    # off because the shell in ExecStart is full of $ and %.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(contents[0])
    return parser


class TestLogShipperUnit(unittest.TestCase):
    def test_nothing_runs_before_the_start_job_ends(self):
        service = unit()['Service']
        self.assertNotIn(
            'ExecStartPre', service,
            'ExecStartPre is part of the start job, which multi-user.target '
            'and so cloud-final wait for; put slow work in ExecStart instead')
        self.assertIn(service.get('Type', 'simple'), ('simple', 'exec'))

    def test_start_timeout_is_not_raised(self):
        # A raised TimeoutStartSec is only needed by a slow start job, so
        # its return is the likeliest sign of the poll moving back.
        self.assertNotIn('TimeoutStartSec', unit()['Service'])

    def test_alloy_is_execed_after_the_hostname_poll(self):
        start = unit()['Service']['ExecStart']
        self.assertIn('sfcbr-*', start)
        self.assertIn('exec /usr/local/bin/alloy run', start)
        self.assertLess(start.index('sfcbr-*'), start.index('exec /usr/local/bin/alloy'))


if __name__ == '__main__':
    unittest.main()
