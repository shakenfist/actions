#!/usr/bin/env python3

"""Tests for tools/ci_headroom_collect.sh.

Nothing in this script may fail a CI job, so what is asserted here is
what it says when the instrument fails: which paths carry a warning
annotation, which deliberately do not, and that a refused census query
leaves no census behind and its reason in the log.

The script has two halves, and neither touches a real ssh, scp or Loki
here. CollectTestCase runs the runner half with ssh and scp replaced by
stubs on PATH. RemoteTestCase runs the remote half -- the heredoc the
runner sends to the primary -- directly under bash, with its traces
directory moved into a temporary one and curl replaced by a stub that
answers the way a real curl does for a given HTTP status.
"""

import os
import subprocess
import tempfile
import unittest

from tests.helpers import REPO_ROOT
from tests.test_ci_headroom_verdict import BAND_VIOLATION
from tests.test_ci_headroom_verdict import SENTINEL
from tests.test_ci_headroom_verdict import WITHHELD


COLLECT = os.path.join(REPO_ROOT, 'tools', 'ci_headroom_collect.sh')

CENSUS_MISSING = '::warning title=Refusal census not collected::'


def write_executable(path, body):
    with open(path, 'w') as f:
        f.write('#!/bin/bash\n' + body)
    os.chmod(path, 0o755)


def workflow_commands(stdout):
    """Return every stdout line GitHub would read as a workflow command.

    The runner trims leading whitespace before it looks for the '::', so
    an indented line counts.
    """
    return [line for line in stdout.splitlines()
            if line.lstrip().startswith('::')]


class CollectTestCase(unittest.TestCase):
    """The runner side of ci_headroom_collect.sh, with ssh and scp stubbed.

    The remote half is not run here, but everything after the copies back
    to the runner can be: scp is replaced by a stub that copies a remote
    path's basename out of a fixture directory, or fails when there is no
    such file, which is what a real scp does when the primary never wrote
    it.
    """

    def setUp(self):
        super().setUp()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = self.tempdir.name

        self.bin = os.path.join(self.root, 'bin')
        self.remote = os.path.join(self.root, 'remote')
        self.workspace = os.path.join(self.root, 'workspace')
        self.tools = os.path.join(self.root, 'tools')
        self.sf_tools = os.path.join(self.workspace, 'shakenfist', 'tools')
        for d in (self.bin, self.remote, self.tools, self.sf_tools,
                  os.path.join(self.root, 'tmp')):
            os.makedirs(d)

        write_executable(os.path.join(self.bin, 'ssh'),
                         'cat > /dev/null\nexit 0\n')
        write_executable(os.path.join(self.bin, 'scp'),
                         'src="${@: -2:1}"\n'
                         'dst="${@: -1}"\n'
                         'f="%s/${src##*/}"\n'
                         '[ -f "${f}" ] || exit 1\n'
                         'cp "${f}" "${dst}"\n' % self.remote)

        for name in ('ci_headroom_collect.sh', 'ci_headroom_verdict.sh'):
            with open(os.path.join(REPO_ROOT, 'tools', name)) as src:
                with open(os.path.join(self.tools, name), 'w') as dst:
                    dst.write(src.read())

    def write_probe(self):
        with open(os.path.join(self.sf_tools, 'ci_headroom_probe.py'),
                  'w') as f:
            f.write('\n')

    def write_report(self, exit_code=0):
        # The probe and the report arrived in shakenfist together, so a
        # checkout with a report always has the probe beside it.
        self.write_probe()
        with open(os.path.join(self.sf_tools, 'ci_headroom_report.py'),
                  'w') as f:
            f.write('import sys\n%s = %d\nprint("summary line")\n'
                    'sys.exit(%d)\n' % (SENTINEL, BAND_VIOLATION, exit_code))

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

    def test_a_missing_report_beside_the_probe_is_withheld_once(self):
        # The instrument broken rather than absent. The series is missing
        # too, as it would be, and the run still gets one annotation rather
        # than one per thing that went missing.
        self.write_probe()
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(WITHHELD + 'ci_headroom_report.py', result.stdout)
        self.assertEqual(result.stdout.count(WITHHELD), 1, result.stdout)

    def test_a_ref_predating_the_probe_is_not_annotated(self):
        # Neither probe nor report: ci_headroom_launch.sh skipped the probe
        # as expected, so there is no series either, and nothing anyone
        # could act on. An annotation here would be on every run of that
        # ref, and would teach readers to ignore the one that matters.
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('::warning', result.stdout)
        self.assertIn('predating the headroom probe', result.stdout)

    def test_no_census_is_annotated_but_the_verdict_still_runs(self):
        self.write_report()
        self.write_remote('headroom.jsonl')
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(CENSUS_MISSING, result.stdout)
        self.assertNotIn(WITHHELD, result.stdout)
        self.assertIn('summary line', result.stdout)

    def test_no_census_and_a_failed_report_carry_both_annotations(self):
        # Two different things missing, and neither stands in for the other.
        self.write_report(exit_code=1)
        self.write_remote('headroom.jsonl')
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(CENSUS_MISSING, result.stdout)
        self.assertIn(WITHHELD, result.stdout)

    def test_a_missing_verdict_script_is_annotated_as_withheld(self):
        os.unlink(os.path.join(self.tools, 'ci_headroom_verdict.sh'))
        self.write_report()
        self.write_remote('headroom.jsonl')
        self.write_remote('headroom-census.json')
        result = self.run_collect()
        self.assertEqual(result.returncode, 0)
        self.assertIn(WITHHELD + 'ci_headroom_verdict.sh', result.stdout)
        self.assertIn('summary line', result.stdout)


class RemoteTestCase(unittest.TestCase):
    """The remote half of ci_headroom_collect.sh, run here under bash.

    The heredoc is taken from the script's source and run as the primary
    would run it, with /srv/ci/traces pointed at a temporary directory.
    curl is a stub which behaves like the real one for the HTTP status in
    its fixture: the body on stdout, and for a status of 400 or more only
    when asked to fail, exit 22 with curl's own message on stderr -- with
    the body under --fail-with-body, without it under --fail. So the
    script's curl flags are what decides whether a refusal is kept, as on
    the primary.
    """

    def setUp(self):
        super().setUp()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = self.tempdir.name

        self.bin = os.path.join(root, 'bin')
        self.traces = os.path.join(root, 'traces')
        self.fixture = os.path.join(root, 'curl')
        for d in (self.bin, self.traces, self.fixture):
            os.makedirs(d)
        self.census = os.path.join(self.traces, 'headroom-census.json')

        write_executable(os.path.join(self.bin, 'pkill'), 'exit 0\n')
        write_executable(os.path.join(self.bin, 'curl'), '''
fixture=%s
status=$(cat "${fixture}/status")
if [ "${status}" = refused ]; then
    echo "curl: (7) Failed to connect to localhost port 3100" >&2
    exit 7
fi
fail=
for arg in "$@"; do
    case "${arg}" in
        --fail-with-body) fail=body ;;
        --fail|-f) fail=${fail:-quiet} ;;
    esac
done
if [ "${status}" -ge 400 ] && [ -n "${fail}" ]; then
    [ "${fail}" = body ] && cat "${fixture}/body"
    echo "curl: (22) The requested URL returned error: ${status}" >&2
    exit 22
fi
cat "${fixture}/body"
''' % self.fixture)

        with open(COLLECT) as f:
            collect = f.read()
        opener = "<<'REMOTE_EOF' || true\n"
        remote = collect[collect.index(opener) + len(opener):]
        remote = remote[:remote.index('\nREMOTE_EOF\n') + 1]
        self.remote = remote.replace('/srv/ci/traces', self.traces)

    def loki_answers(self, status, body):
        with open(os.path.join(self.fixture, 'status'), 'w') as f:
            f.write(str(status))
        with open(os.path.join(self.fixture, 'body'), 'w') as f:
            f.write(body)

    def run_remote(self):
        environment = dict(os.environ)
        environment['PATH'] = self.bin + os.pathsep + environment['PATH']
        return subprocess.run(
            ['bash', '-s', '--', '5000', ''], input=self.remote,
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment)

    def test_the_heredoc_was_found_and_relocated(self):
        # Guards every test below against passing on an empty script.
        self.assertIn('query_range', self.remote)
        self.assertNotIn('/srv/ci/traces', self.remote)

    def test_an_answered_query_is_kept_as_the_census(self):
        self.loki_answers(200, '{"status": "success"}\n')
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.census) as f:
            self.assertEqual(f.read(), '{"status": "success"}\n')
        self.assertNotIn('census query failed', result.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(self.traces, 'headroom-census.err')))

    def test_a_refused_query_is_not_kept_and_says_why(self):
        # The 400 for exceeding max_entries_limit_per_query, the refusal
        # this was written for. Kept, its body would reach the report as an
        # unparseable census; dropped silently, nobody could say why.
        self.loki_answers(400, 'max entries limit per query exceeded\n')
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(self.census))
        self.assertIn('census query failed', result.stdout)
        self.assertIn('returned error: 400', result.stdout)
        self.assertIn('max entries limit per query exceeded', result.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(self.traces, 'headroom-census.err')))

    def test_an_unreachable_loki_is_not_kept_and_says_why(self):
        self.loki_answers('refused', '')
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(self.census))
        self.assertIn('Failed to connect', result.stdout)

    def test_forwarded_text_cannot_issue_workflow_commands(self):
        # Loki's body, curl's stderr and the probe's log all reach the
        # runner step's stdout. None of them is this script's prose, and a
        # line of any of them starting '::' would be obeyed by the runner.
        self.loki_answers(400, '::error::from loki\n  ::warning::indented\n')
        with open(os.path.join(self.traces, 'headroom-probe.log'), 'w') as f:
            f.write('::error title=Probe::from the probe log\n')
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('from loki', result.stdout)
        self.assertIn('indented', result.stdout)
        self.assertIn('from the probe log', result.stdout)
        self.assertEqual(workflow_commands(result.stdout), [])


if __name__ == '__main__':
    unittest.main()
