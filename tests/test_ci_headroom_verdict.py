#!/usr/bin/env python3

"""Tests for tools/ci_headroom_verdict.sh.

This is the only part of the headroom instrument which can fail a CI
job, and the whole safety argument for it is a set of claims about when
it declines to: when the report is unhappy for some reason other than
the band, when the report does not implement the contract at all, and
when an operator has switched the gate off. Those are exactly the
claims that are cheap to assert in a comment and expensive to get
wrong, since the failure mode is a red job on an innocent pull request.

The script was split out of ci_headroom_collect.sh so they could be
asserted here instead. Everything it needs is a path to something it
can run as the report, so none of this touches ssh, scp or Loki.
"""

import os
import re
import subprocess
import unittest

from tests.helpers import REPO_ROOT


VERDICT = os.path.join(REPO_ROOT, 'tools', 'ci_headroom_verdict.sh')
COLLECT = os.path.join(REPO_ROOT, 'tools', 'ci_headroom_collect.sh')
WORKFLOW = os.path.join(REPO_ROOT, '.github', 'workflows', 'smoke-cluster.yml')

# What the report's source has to contain before this script will believe
# its exit status means a band violation. Named here as well as in the
# script so that a rename on either side fails a test rather than
# silently stopping the gate.
SENTINEL = 'BAND_VIOLATION_EXIT'

BAND_VIOLATION = 3


class VerdictTestCase(unittest.TestCase):
    def write_report(self, exit_code, sentinel=True, message='summary line'):
        """Write a stand-in for ci_headroom_report.py and return its path."""
        path = os.path.join(self.workdir, 'report.py')
        lines = ['import sys']
        if sentinel:
            lines.append('%s = %d' % (SENTINEL, BAND_VIOLATION))
        lines.append('print(%r)' % message)
        lines.append('sys.exit(%d)' % exit_code)
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return path

    def run_verdict(self, *argv, **env):
        environment = dict(os.environ)
        environment.pop('CI_HEADROOM_GATE', None)
        environment.update(env)
        return subprocess.run(
            ['bash', VERDICT] + list(argv), cwd=REPO_ROOT, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=environment)

    def setUp(self):
        super().setUp()
        import tempfile
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workdir = self.tempdir.name

    def test_a_happy_report_says_nothing_and_exits_zero(self):
        result = self.run_verdict(self.write_report(0))
        self.assertEqual(result.returncode, 0)
        self.assertIn('summary line', result.stdout)
        self.assertNotIn('has failed this job', result.stdout)
        self.assertNotIn('::error', result.stdout)

    def test_the_band_violation_status_fails_the_job(self):
        result = self.run_verdict(self.write_report(BAND_VIOLATION))
        self.assertEqual(result.returncode, BAND_VIOLATION)
        self.assertIn('has failed this job', result.stdout)
        self.assertIn('This is not a test', result.stdout)

    def test_the_band_violation_is_also_a_run_annotation(self):
        # The step is named for collection, so without this a reader of the
        # run summary sees only a collection step that failed.
        result = self.run_verdict(self.write_report(BAND_VIOLATION))
        self.assertIn(
            '::error title=Cluster headroom outside the CI sizing band::',
            result.stdout)

    def test_the_report_being_unhappy_does_not_fail_the_job(self):
        # An uncaught exception in the report, and an argparse usage error
        # for a flag the report does not have. Both mean the measurement
        # cannot be trusted, which is not evidence about the cloud.
        for status in (1, 2, 4, 70, 127):
            with self.subTest(status=status):
                result = self.run_verdict(self.write_report(status))
                self.assertEqual(result.returncode, 0)
                self.assertIn('exited %d' % status, result.stdout)
                self.assertIn('rather than as a statement', result.stdout)

    def test_a_report_without_the_sentinel_is_never_gated_on(self):
        # Version skew: every ci_headroom_report.py written before the
        # contract existed, for which 3 means nothing in particular.
        result = self.run_verdict(
            self.write_report(BAND_VIOLATION, sentinel=False))
        self.assertEqual(result.returncode, 0)
        self.assertIn(SENTINEL, result.stdout)
        self.assertIn('does not implement', result.stdout)

    def test_the_off_switch_suppresses_the_gate(self):
        for value in ('false', 'False', '0', 'no', 'off', 'OFF'):
            with self.subTest(value=value):
                result = self.run_verdict(
                    self.write_report(BAND_VIOLATION),
                    CI_HEADROOM_GATE=value)
                self.assertEqual(result.returncode, 0)
                self.assertIn('gate is', result.stdout)
                self.assertIn('switched off', result.stdout)

    def test_the_off_switch_says_the_verdict_still_stands(self):
        # Suppressing the gate must not read as suppressing the finding.
        result = self.run_verdict(
            self.write_report(BAND_VIOLATION), CI_HEADROOM_GATE='false')
        self.assertIn('verdict stands', result.stdout)

    def test_anything_but_an_off_value_leaves_the_gate_on(self):
        # The default is on, and so is any value that is not recognisably
        # a negative -- a typo must not silently disarm the gate.
        for value in ('true', 'True', '1', 'yes', '', 'maybe'):
            with self.subTest(value=value):
                result = self.run_verdict(
                    self.write_report(BAND_VIOLATION),
                    CI_HEADROOM_GATE=value)
                self.assertEqual(result.returncode, BAND_VIOLATION)

    def test_report_arguments_are_passed_through(self):
        path = os.path.join(self.workdir, 'echo_args.py')
        with open(path, 'w') as f:
            f.write('import sys\nprint(" ".join(sys.argv[1:]))\n')
        result = self.run_verdict(path, '--series', 'a b', '--label', 'x')
        self.assertEqual(result.returncode, 0)
        self.assertIn('--series a b --label x', result.stdout)

    def test_no_report_is_a_usage_error(self):
        result = self.run_verdict()
        self.assertEqual(result.returncode, 64)
        self.assertIn('usage:', result.stderr)


class WiringTestCase(unittest.TestCase):
    """The verdict script is only reachable if the wiring around it holds."""

    def setUp(self):
        super().setUp()
        with open(COLLECT) as f:
            self.collect = f.read()
        with open(WORKFLOW) as f:
            self.workflow = f.read()

    def test_the_collect_script_hands_off_to_the_verdict_script(self):
        self.assertIn('ci_headroom_verdict.sh', self.collect)
        # exec, so nothing sits between the verdict's status and the step's.
        self.assertIn('exec bash "${verdict}"', self.collect)

    def test_the_collect_step_does_not_swallow_the_status(self):
        step = self.workflow[self.workflow.index(
            '- name: Collect the cluster headroom series'):]
        step = step[:step.index('- name: List failing tests')]
        self.assertNotIn('continue-on-error', step)
        self.assertIn('CI_HEADROOM_GATE: ${{ inputs.headroom_gate }}', step)

    def test_the_probe_start_step_still_swallows_everything(self):
        step = self.workflow[self.workflow.index(
            '- name: Start the cluster headroom probe'):]
        step = step[:step.index('- name: Run functional tests')]
        self.assertIn('continue-on-error: true', step)

    def test_the_failing_test_listing_follows_the_suite_not_the_job(self):
        # Otherwise a band violation, which is the job's first failure,
        # prints "Failed tests:" on a run whose suite passed.
        step = self.workflow[self.workflow.index(
            '- name: List failing tests'):]
        step = step[:step.index('- name: Check for exceptions on disk')]
        self.assertIn("steps.functional.outcome == 'failure'", step)
        self.assertNotIn('if: failure()', step)

    def test_the_gate_input_exists_and_defaults_to_on(self):
        # The block runs from the input's own line to the next line at the
        # same indentation, which is the sibling input after it.
        lines = self.workflow.splitlines()
        start = lines.index('      headroom_gate:')
        block = [lines[start]]
        for line in lines[start + 1:]:
            if re.match(r'^      \S', line):
                break
            block.append(line)
        block = '\n'.join(block)
        self.assertIn('type: boolean', block)
        self.assertIn('default: true', block)


if __name__ == '__main__':
    unittest.main()
