"""Tests for tools/ansible-galaxy-retry.sh.

The wrapper exists because galaxy.ansible.com answers HTTP 500 often
enough to fail smoke cluster deploys (shakenfist/shakenfist#4632). What
matters is the line it draws: a transient failure is retried and a real
one is not, and the exit status that reaches the caller is
ansible-galaxy's own. A fake ansible-galaxy on PATH plays each part.
"""

import os
import stat
import subprocess
import tempfile
import unittest

from tests.helpers import REPO_ROOT

WRAPPER = os.path.join(REPO_ROOT, 'tools', 'ansible-galaxy-retry.sh')

# Fails the first FAIL_TIMES calls with FAIL_MESSAGE and FAIL_RC, then
# succeeds; every call's arguments are appended to CALLS.
FAKE = """#!/bin/bash
echo "$*" >> "${CALLS}"
n=$(wc -l < "${CALLS}")
if [ "${n}" -le "${FAIL_TIMES}" ]; then
    echo "${FAIL_MESSAGE}" >&2
    exit "${FAIL_RC}"
fi
echo "installed successfully"
"""

GALAXY_500 = ("[ERROR]: Failed to download collection tar from 'default': "
              'HTTP Error 500: Internal Server Error')


class AnsibleGalaxyRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = os.path.join(self.tmp.name, 'ansible-galaxy')
        with open(fake, 'w') as f:
            f.write(FAKE)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        self.calls = os.path.join(self.tmp.name, 'calls')
        open(self.calls, 'w').close()

    def run_wrapper(self, fail_times, message=GALAXY_500, rc=1, attempts='4'):
        env = dict(os.environ,
                   PATH=self.tmp.name + os.pathsep + os.environ['PATH'],
                   CALLS=self.calls, FAIL_TIMES=str(fail_times),
                   FAIL_MESSAGE=message, FAIL_RC=str(rc),
                   GALAXY_RETRY_ATTEMPTS=attempts, GALAXY_RETRY_DELAY='0')
        result = subprocess.run(
            [WRAPPER, 'collection', 'install', 'x.tar.gz', '--force'],
            env=env, capture_output=True, text=True, timeout=60)
        with open(self.calls) as f:
            calls = f.read().splitlines()
        return result, calls

    def test_a_galaxy_500_is_retried_until_it_succeeds(self):
        result, calls = self.run_wrapper(fail_times=2)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(3, len(calls))
        # The arguments reach ansible-galaxy unchanged, every time.
        self.assertEqual(['collection install x.tar.gz --force'] * 3, calls)

    def test_retrying_stops_at_the_attempt_limit(self):
        result, calls = self.run_wrapper(fail_times=10, rc=7, attempts='3')
        self.assertEqual(3, len(calls))
        # ansible-galaxy's own status, not one the wrapper made up.
        self.assertEqual(7, result.returncode)
        self.assertIn('giving up', result.stderr)

    def test_a_real_failure_is_not_retried(self):
        result, calls = self.run_wrapper(
            fail_times=10, rc=1,
            message="[ERROR]: Failed to resolve the requested dependencies map. "
                    "Could not satisfy the following requirements")
        self.assertEqual(1, len(calls))
        self.assertEqual(1, result.returncode)

    def test_a_dropped_connection_is_retried(self):
        result, calls = self.run_wrapper(
            fail_times=1,
            message='ERROR! Unknown error when attempting to call Galaxy: '
                    '<urlopen error [Errno 104] Connection reset by peer>')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, len(calls))

    def test_the_output_still_reaches_the_log(self):
        result, _ = self.run_wrapper(fail_times=1)
        self.assertIn('HTTP Error 500', result.stdout + result.stderr)
        self.assertIn('installed successfully', result.stdout)


if __name__ == '__main__':
    unittest.main()
