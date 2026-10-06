#!/usr/bin/env python3

"""Tests for tools/ci_node_checks.sh's Kerbside coverage.

The failure worth pinning is a check that passes without having looked: a
kerbside-api.service with Restart=always that crash-loops never shows as a
failed unit, so only NRestarts reveals it. A host with no Kerbside units must
still pass, and a Kerbside journal error must fail the run.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from tests.helpers import REPO_ROOT


SCRIPT = os.path.join(REPO_ROOT, 'tools', 'ci_node_checks.sh')

SYSTEMCTL = """#!/bin/bash
# Stub systemctl driven by STUB_* environment variables.
case "$1" in
    list-units)
        if [ "$2" = "--failed" ]; then
            printf '%s' "${STUB_FAILED:-}"
            exit 0
        fi
        printf '%s' "${STUB_KERBSIDE_UNITS:-}"
        ;;
    show)
        echo "${STUB_NRESTARTS:-0}"
        ;;
esac
exit 0
"""

JOURNALCTL = """#!/bin/bash
# Stub journalctl: unit-scoped calls return STUB_UNIT_JOURNAL, others the boot journal.
for arg in "$@"; do
    if [ "${arg}" = "-u" ]; then
        printf '%s\\n' "${STUB_UNIT_JOURNAL:-}"
        exit 0
    fi
done
printf '%s\\n' "${STUB_BOOT_JOURNAL:-}"
"""

KERBSIDE_UNITS = ('kerbside-api.service loaded active running Kerbside API\\n'
                  'kerbside-old.service not-found inactive dead kerbside-old.service\\n')


class NodeChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        for name, body in (('systemctl', SYSTEMCTL), ('journalctl', JOURNALCTL)):
            path = os.path.join(self.tmp, name)
            with open(path, 'w') as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.environment = dict(os.environ, PATH='%s:%s' % (self.tmp, os.environ['PATH']))

    def run_script(self, job='', **stub):
        environment = dict(self.environment)
        for key, value in stub.items():
            environment['STUB_' + key.upper()] = value.encode().decode('unicode_escape')
        return subprocess.run(['bash', SCRIPT, '', job], capture_output=True, text=True, env=environment)

    def test_host_without_kerbside_passes(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_kerbside_with_no_restarts_passes(self):
        result = self.run_script(kerbside_units=KERBSIDE_UNITS, nrestarts='0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_kerbside_restarts_fail_naming_unit_and_count(self):
        result = self.run_script(kerbside_units=KERBSIDE_UNITS, nrestarts='2')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('kerbside-api.service has been restarted 2 times', result.stdout)
        self.assertNotIn('kerbside-old.service has been', result.stdout)

    def test_failed_kerbside_unit_fails(self):
        result = self.run_script(failed='kerbside-api.service loaded failed failed Kerbside API\\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('kerbside-api.service', result.stdout)

    def test_kerbside_journal_exit_fails(self):
        result = self.run_script(unit_journal="kerbside-api.service: Failed with result 'exit-code'.")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_lifecycle_job_skips_restart_check(self):
        result = self.run_script(job='node-lifecycle', kerbside_units=KERBSIDE_UNITS, nrestarts='2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
