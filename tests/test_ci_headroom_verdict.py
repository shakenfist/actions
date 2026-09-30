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

import yaml

from tests.helpers import REPO_ROOT


VERDICT = os.path.join(REPO_ROOT, 'tools', 'ci_headroom_verdict.sh')
COLLECT = os.path.join(REPO_ROOT, 'tools', 'ci_headroom_collect.sh')
WORKFLOW = os.path.join(REPO_ROOT, '.github', 'workflows', 'smoke-cluster.yml')
CANARY = os.path.join(REPO_ROOT, '.github', 'workflows', 'canary.yml')

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
        # Armed unless a test says otherwise: a test that expects exit 0 for
        # some other reason -- version skew, a report that is merely unhappy
        # -- would otherwise pass because the gate was off, not because its
        # own guard held. Pass CI_HEADROOM_GATE=None to leave it unset.
        environment = dict(os.environ)
        environment['CI_HEADROOM_GATE'] = 'true'
        environment.update(env)
        environment = {k: v for k, v in environment.items() if v is not None}
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
        for value in ('false', 'False', '0', 'no', 'off', 'OFF', ''):
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

    def test_an_unset_switch_leaves_the_gate_off(self):
        # Off unless opted in, the same default smoke-cluster.yml gives the
        # input, so a direct invocation matches a caller which never armed.
        result = self.run_verdict(
            self.write_report(BAND_VIOLATION), CI_HEADROOM_GATE=None)
        self.assertEqual(result.returncode, 0)
        self.assertIn('switched off', result.stdout)

    def test_anything_but_an_off_value_arms_the_gate(self):
        # Any set value that is not recognisably a negative arms it -- a
        # typo in an armed caller must not silently disarm the gate.
        for value in ('true', 'True', '1', 'yes', 'maybe'):
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

    def collect_step(self):
        step = self.workflow[self.workflow.index(
            '- name: Collect the cluster headroom series'):]
        return step[:step.index('- name: List failing tests')]

    def test_the_collect_script_hands_off_to_the_verdict_script(self):
        self.assertIn('ci_headroom_verdict.sh', self.collect)
        # exec, so nothing sits between the verdict's status and the step's.
        self.assertIn('exec bash "${verdict}"', self.collect)

    def test_the_collect_step_does_not_swallow_the_status(self):
        step = self.collect_step()
        self.assertNotIn('continue-on-error', step)
        self.assertIn('CI_HEADROOM_GATE:', step)
        self.assertIn('inputs.headroom_gate', step)

    def test_the_gate_cannot_be_armed_for_an_unmeasured_test_kind(self):
        # The band is fitted against the shapes shakenfist's warn window
        # measured, and it has only ever seen functional runs. The
        # ansible-modules suite is probed but must not be gated on a band
        # nobody fitted to it, however its caller sets headroom_gate --
        # this workflow is consumed @main, so an armed caller would go red
        # fleet-wide with nothing to revert. Asserted on the resolved
        # expression rather than the literal so that adding a third test
        # kind has to come past this test.
        step = self.collect_step()
        gate = re.search(r'CI_HEADROOM_GATE: (.*)', step).group(1).strip()
        self.assertEqual(
            gate,
            "${{ inputs.test_kind == 'functional' && inputs.headroom_gate }}")

    def test_the_headroom_label_never_names_an_unrun_suite(self):
        # stestr_config is ignored for test_kind ansible-modules and keeps
        # its default, so passing it verbatim would label the run with a
        # suite it never executed. The harvest takes the topology from the
        # label's first token, so only the second may vary by test kind.
        step = self.collect_step()
        label = re.search(r'ci_headroom_collect\.sh.*\n\s*"(.*)"',
                          step).group(1)
        self.assertTrue(label.startswith('${{ inputs.topology }} '), label)
        self.assertIn("inputs.test_kind == 'ansible-modules'", label)
        self.assertIn('inputs.stestr_config', label)

    def test_the_probe_cap_tracks_the_test_step_that_will_run(self):
        # The cap is test-step timeout plus five minutes. 'Run ansible
        # module tests' hardcodes 60, above test_timeout_minutes' default of
        # 45, so a cap derived from the input alone would stop the probe
        # before the step it is measuring can time out -- losing exactly the
        # contended tail the instrument exists to record.
        step = self.workflow[self.workflow.index(
            '- name: Start the cluster headroom probe'):]
        step = step[:step.index('- name: Run functional tests')]
        self.assertIn("inputs.test_kind == 'ansible-modules' && 60", step)
        self.assertIn('inputs.test_timeout_minutes', step)

        ansible = self.workflow[self.workflow.index(
            '- name: Run ansible module tests'):]
        ansible = ansible[:ansible.index('run: |')]
        cap = int(re.search(r"&& (\d+) \|\|", step).group(1))
        timeout = int(re.search(r'timeout-minutes: (\d+)', ansible).group(1))
        self.assertGreaterEqual(
            cap, timeout,
            'the probe cap (%d minutes) is below the ansible-modules step '
            'timeout (%d minutes), so the probe stops sampling before the '
            'step it measures can time out' % (cap, timeout))

    def test_every_probed_test_kind_can_write_its_traces(self):
        # The trap this exists to hold shut: /srv/ci is a mount point whose
        # root base_image_user cannot write, so "Make the traces directory"
        # is the only thing that creates a writable traces/. The fallback
        # mkdir in ci_headroom_launch.sh runs unprivileged, discards its
        # error, and the script exits 0 -- so narrowing this step to a
        # subset of the probed kinds produces a green run that measured
        # nothing, with no failure anywhere to notice. Asserted as an
        # equality between conditions rather than a literal, so a third
        # probed test kind cannot be added to one and forgotten in the
        # other.
        def condition(name, end):
            step = self.workflow[self.workflow.index('- name: %s' % name):]
            step = step[:step.index('- name: %s' % end)]
            return re.search(r'if: (.*)', step).group(1).strip()

        traces = condition('Make the traces directory',
                           'Authorise the primary to reach other nodes')
        probe = condition('Start the cluster headroom probe',
                          'Run functional tests')
        self.assertEqual(
            traces, probe,
            'the traces directory step runs for "%s" but the probe runs for '
            '"%s"; any kind in the second and not the first starts a probe '
            'that cannot write, and still passes' % (traces, probe))

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

    def test_the_canary_is_deliberately_not_gated(self):
        # The canary runs a single-node smoke cloud, a shape no warn window
        # measured. Pinned so a change to the input's default cannot arm or
        # disarm it without this file saying which was meant.
        with open(CANARY) as f:
            canary = yaml.safe_load(f)
        inputs = canary['jobs']['smoke']['with']
        self.assertIn('headroom_gate', inputs)
        self.assertIs(inputs['headroom_gate'], False)

    def test_the_gate_input_exists_and_defaults_to_off(self):
        # Off by default so a caller no warn window has measured is never
        # gated by omission; shakenfist opts its measured shapes in.
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
        self.assertIn('default: false', block)


if __name__ == '__main__':
    unittest.main()
