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

WITHHELD = '::warning title=Headroom verdict withheld::'
CENSUS_MISSING = '::warning title=Refusal census not collected::'


class CollectTestCase(unittest.TestCase):
    """The runner side of ci_headroom_collect.sh, with ssh and scp stubbed.

    The remote half cannot run here, but everything after the copies back
    to the runner can: scp is replaced by a stub that copies a remote
    path's basename out of a fixture directory, or fails when there is no
    such file, which is what a real scp does when the primary never wrote
    it.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = self.tempdir.name

        self.bin = os.path.join(self.root, 'bin')
        self.remote = os.path.join(self.root, 'remote')
        self.workspace = os.path.join(self.root, 'workspace')
        self.tools = os.path.join(self.root, 'tools')
        for d in (self.bin, self.remote, self.tools,
                  os.path.join(self.workspace, 'shakenfist', 'tools'),
                  os.path.join(self.root, 'tmp')):
            os.makedirs(d)

        stubs = {
            'ssh': 'cat > /dev/null\nexit 0\n',
            'scp': ('src="${@: -2:1}"\n'
                    'dst="${@: -1}"\n'
                    'f="%s/${src##*/}"\n'
                    '[ -f "${f}" ] || exit 1\n'
                    'cp "${f}" "${dst}"\n' % self.remote),
        }
        for name, body in stubs.items():
            path = os.path.join(self.bin, name)
            with open(path, 'w') as f:
                f.write('#!/bin/bash\n' + body)
            os.chmod(path, 0o755)

        for name in ('ci_headroom_collect.sh', 'ci_headroom_verdict.sh'):
            with open(os.path.join(REPO_ROOT, 'tools', name)) as src:
                with open(os.path.join(self.tools, name), 'w') as dst:
                    dst.write(src.read())

    def write_report(self):
        path = os.path.join(self.workspace, 'shakenfist', 'tools',
                            'ci_headroom_report.py')
        with open(path, 'w') as f:
            f.write('%s = %d\nprint("summary line")\n'
                    % (SENTINEL, BAND_VIOLATION))

    def write_remote(self, name, content='{}\n'):
        with open(os.path.join(self.remote, name), 'w') as f:
            f.write(content)

    def run_collect(self):
        environment = dict(os.environ)
        environment['PATH'] = self.bin + os.pathsep + environment['PATH']
        environment['GITHUB_WORKSPACE'] = self.workspace
        environment['TMPDIR'] = os.path.join(self.root, 'tmp')
        environment['CI_HEADROOM_GATE'] = 'true'
        return subprocess.run(
            ['bash', os.path.join(self.tools, 'ci_headroom_collect.sh'),
             'primary.invalid', 'debian', 'slim-primary smoke-ci.conf'],
            cwd=self.root, check=False, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=environment)

    def test_a_healthy_run_is_not_annotated(self):
        self.write_report()
        self.write_remote('headroom.jsonl')
        self.write_remote('headroom-census.json')
        result = self.run_collect()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('summary line', result.stdout)
        self.assertNotIn('::warning', result.stdout)

    def test_no_series_is_annotated_as_withheld(self):
        # The probe having failed, and the case that matters most.
        self.write_report()
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(WITHHELD + 'No headroom series', result.stdout)

    def test_no_report_is_annotated_as_withheld(self):
        self.write_remote('headroom.jsonl')
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(WITHHELD + 'ci_headroom_report.py', result.stdout)

    def test_no_census_is_annotated_but_the_verdict_still_runs(self):
        self.write_report()
        self.write_remote('headroom.jsonl')
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(CENSUS_MISSING, result.stdout)
        self.assertNotIn(WITHHELD, result.stdout)
        self.assertIn('summary line', result.stdout)

    def test_a_missing_verdict_script_is_annotated_as_withheld(self):
        os.unlink(os.path.join(self.tools, 'ci_headroom_verdict.sh'))
        self.write_report()
        self.write_remote('headroom.jsonl')
        self.write_remote('headroom-census.json')
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(WITHHELD + 'ci_headroom_verdict.sh', result.stdout)
        self.assertIn('summary line', result.stdout)


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

    def test_an_unhappy_report_is_annotated_as_withheld(self):
        # Otherwise a run whose instrument failed renders exactly like a
        # healthy one anywhere outside the step log.
        result = self.run_verdict(self.write_report(1))
        self.assertIn(WITHHELD, result.stdout)
        self.assertIn('exited 1', result.stdout.splitlines()[1])

    def test_a_report_without_the_sentinel_is_annotated_as_withheld(self):
        # The version-skew guard is what a rename of the sentinel looks
        # like: the gate stops gating, so it must not do so silently.
        result = self.run_verdict(
            self.write_report(BAND_VIOLATION, sentinel=False))
        self.assertIn(WITHHELD, result.stdout)

    def test_only_a_failed_instrument_is_annotated_as_withheld(self):
        # A clean report, a gated violation and a violation with the gate
        # off are all verdicts. Annotating them as withheld would teach
        # readers to ignore the annotation.
        for status, gate in ((0, 'true'), (BAND_VIOLATION, 'true'),
                             (BAND_VIOLATION, 'false')):
            with self.subTest(status=status, gate=gate):
                result = self.run_verdict(
                    self.write_report(status), CI_HEADROOM_GATE=gate)
                self.assertNotIn('Headroom verdict withheld', result.stdout)

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
