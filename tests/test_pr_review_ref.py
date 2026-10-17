#!/usr/bin/env python3

"""Run pr-review-ref's script for real.

tools/pr-review-ref.sh decides which commit a requested re-review sees,
and every way it can be wrong is silent: a stale merge ref accepted is a
careful review of superseded code, and a confirmation that compares
nothing lets a push between resolving and checking out be reviewed with
no warning. Nothing about either run looks wrong, so the script is run
here against a stub `gh` that answers the way the API does, and against
scratch git repositories laid out the way actions/checkout leaves them.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

from tests.helpers import REPO_ROOT


SCRIPT = os.path.join(REPO_ROOT, 'tools', 'pr-review-ref.sh')
ACTION = os.path.join(REPO_ROOT, 'pr-review-ref', 'action.yml')

REPO = 'shakenfist/actions'
PULL = 'repos/%s/pulls/42' % REPO
MERGE_REF = 'repos/%s/git/ref/pull/42/merge' % REPO

HEAD = 'a' * 40
OLD_HEAD = 'b' * 40
BASE = 'c' * 40
MERGE = '1' * 40
OLD_MERGE = '2' * 40


def commit(sha):
    return 'repos/%s/commits/%s' % (REPO, sha)


# Answers `gh api PATH --jq EXPR` from FAKE_GH_RESPONSES, a JSON map of
# path to a list of {"status": ..., "body": ...}. Successive calls for a
# path take successive entries, and the last one repeats, which is how a
# merge ref that GitHub republishes between attempts is modelled. As gh
# does, a 200 has the jq applied, and an HTTP error writes its body to
# *stdout* and exits 1. Only the jq the script uses is understood -- a
# path of keys and [n] indexes -- and anything else, or a path nobody
# provided, exits 99, so a script that changes what it asks fails its
# test rather than reading an answer meant for another call. Every call
# is appended to FAKE_GH_LOG.
FAKE_GH = """\
import json, os, re, sys

args = sys.argv[1:]
with open(os.environ['FAKE_GH_LOG'], 'a') as f:
    f.write(' '.join(args) + '\\n')
with open(os.environ['FAKE_GH_RESPONSES']) as f:
    responses = json.load(f)
if args[:1] != ['api'] or len(args) < 2 or args[1] not in responses:
    sys.stderr.write('fake gh: nothing to answer %r with\\n' % args)
    sys.exit(99)
with open(os.environ['FAKE_GH_LOG']) as f:
    seen = sum(1 for line in f if line.split()[1:2] == [args[1]])
answers = responses[args[1]]
response = answers[min(seen, len(answers)) - 1]
if response['status'] != 200:
    sys.stdout.write(json.dumps(response['body']))
    sys.stderr.write('gh: %s (HTTP %d)\\n' % (response['body']['message'], response['status']))
    sys.exit(1)
value = response['body']
expression = args[args.index('--jq') + 1] if '--jq' in args else '.'
if not re.fullmatch(r'(\\.[A-Za-z_]\\w*(\\[\\d+\\])?)+', expression):
    sys.stderr.write('fake gh: cannot evaluate --jq %r\\n' % expression)
    sys.exit(99)
for key, index in re.findall(r'\\.([A-Za-z_]\\w*)(?:\\[(\\d+)\\])?', expression):
    value = value.get(key) if isinstance(value, dict) else None
    if index:
        value = value[int(index)] if isinstance(value, list) and int(index) < len(value) else None
print('null' if value is None else value if isinstance(value, str) else json.dumps(value))
"""


def ok(body):
    return {'status': 200, 'body': body}


NOT_FOUND = {'status': 404, 'body': {'message': 'Not Found'}}


def pull(head_sha=HEAD):
    return ok({'head': {'sha': head_sha}} if head_sha else {'head': {}})


def merge_ref(sha):
    return ok({'object': {'sha': sha}})


def merge_commit(head_sha):
    return ok({'parents': [{'sha': BASE}, {'sha': head_sha}]})


class Run:
    """What one run of the script did."""

    def __init__(self, returncode, stdout, outputs, gh_calls, sleeps):
        self.returncode = returncode
        self.stdout = stdout
        self.outputs = outputs
        self.gh_calls = gh_calls
        self.sleeps = sleeps

    def written(self):
        return dict(line.split('=', 1) for line in self.outputs.splitlines())


def run_script(command, env=None, responses=None, cwd=None, script=SCRIPT):
    """Run the script with a stub gh and a stub sleep on PATH."""
    with tempfile.TemporaryDirectory() as tmp:
        gh = os.path.join(tmp, 'gh')
        with open(gh, 'w') as f:
            f.write('#!%s\n%s' % (sys.executable, FAKE_GH))
        os.chmod(gh, 0o755)
        # The retries wait five seconds each, and the waits are part of
        # what is under test, so they are counted rather than slept.
        sleep = os.path.join(tmp, 'sleep')
        with open(sleep, 'w') as f:
            f.write('#!/bin/sh\necho "$@" >> "%s"\n' % os.path.join(tmp, 'sleeps'))
        os.chmod(sleep, 0o755)
        answers = os.path.join(tmp, 'responses.json')
        with open(answers, 'w') as f:
            json.dump(responses or {}, f)
        for name in ('gh-output', 'gh-log', 'sleeps'):
            open(os.path.join(tmp, name), 'w').close()
        run_env = {k: v for k, v in os.environ.items() if not k.startswith('GITHUB_')}
        run_env.update(
            GITHUB_OUTPUT=os.path.join(tmp, 'gh-output'), FAKE_GH_RESPONSES=answers,
            FAKE_GH_LOG=os.path.join(tmp, 'gh-log'), PATH='%s:%s' % (tmp, os.environ['PATH']))
        run_env.update(env or {})
        result = subprocess.run(
            ['bash', script] + command, env=run_env, cwd=cwd, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def read(name):
            with open(os.path.join(tmp, name)) as f:
                return f.read()

        return Run(result.returncode, result.stdout, read('gh-output'),
                   read('gh-log').splitlines(), read('sleeps').splitlines())


class ResolveTest(unittest.TestCase):
    def resolve(self, responses, pr_number='42'):
        return run_script(['resolve'], env={'GH_TOKEN': 'x', 'REPO': REPO, 'PR_NUMBER': pr_number},
                          responses=responses)

    def test_a_current_merge_ref_is_reviewed_at_once(self):
        run = self.resolve({PULL: [pull()], MERGE_REF: [merge_ref(MERGE)], commit(MERGE): [merge_commit(HEAD)]})
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(run.written(), {'head_sha': HEAD, 'ref': 'refs/pull/42/merge', 'merged': 'true'})
        self.assertEqual(run.sleeps, [])

    def test_a_stale_merge_ref_is_retried_until_it_catches_up(self):
        # GitHub recomputes the merge commit asynchronously, so the first
        # answer can merge the previous head and a later one the current.
        run = self.resolve({
            PULL: [pull()],
            MERGE_REF: [merge_ref(OLD_MERGE), merge_ref(OLD_MERGE), merge_ref(MERGE)],
            commit(OLD_MERGE): [merge_commit(OLD_HEAD)],
            commit(MERGE): [merge_commit(HEAD)],
        })
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(run.written(), {'head_sha': HEAD, 'ref': 'refs/pull/42/merge', 'merged': 'true'})
        self.assertEqual(run.sleeps, ['5', '5'])
        self.assertIn('Attempt 1: merge ref merges %s, not the head' % OLD_HEAD, run.stdout)

    def test_a_merge_ref_that_never_catches_up_falls_back_to_the_head(self):
        run = self.resolve({PULL: [pull()], MERGE_REF: [merge_ref(OLD_MERGE)],
                            commit(OLD_MERGE): [merge_commit(OLD_HEAD)]})
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(run.written(), {'head_sha': HEAD, 'ref': 'refs/pull/42/head', 'merged': 'false',
                                         'reason': 'stale'})
        # Six attempts, with a wait between each and none after the last:
        # the waits hold a scarce runner.
        self.assertEqual(sum(1 for call in run.gh_calls if MERGE_REF in call), 6)
        self.assertEqual(run.sleeps, ['5'] * 5)

    def test_the_fallback_names_the_last_way_the_merge_ref_failed(self):
        cases = {
            # gh writes a 404's body to stdout, so only the exit status
            # can say the ref is absent rather than stale.
            'absent': {PULL: [pull()], MERGE_REF: [NOT_FOUND]},
            'unreadable': {PULL: [pull()], MERGE_REF: [merge_ref(MERGE)], commit(MERGE): [NOT_FOUND]},
            # A stale answer followed by none: what was last seen wins.
            'absent after stale': {PULL: [pull()], MERGE_REF: [merge_ref(OLD_MERGE), NOT_FOUND],
                                   commit(OLD_MERGE): [merge_commit(OLD_HEAD)]},
        }
        for name, responses in cases.items():
            with self.subTest(case=name):
                run = self.resolve(responses)
                self.assertEqual(run.returncode, 0, run.stdout)
                self.assertEqual(run.written(), {'head_sha': HEAD, 'ref': 'refs/pull/42/head', 'merged': 'false',
                                                 'reason': name.split()[0]})

    def test_a_single_parent_commit_does_not_match_an_absent_head(self):
        # --jq prints "null" for an absent field at both ends of the
        # comparison, and two absent values agreeing is not a match.
        run = self.resolve({PULL: [pull(head_sha=None)], MERGE_REF: [merge_ref(MERGE)],
                            commit(MERGE): [ok({'parents': [{'sha': BASE}]})]})
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.outputs, '')

    def test_a_merge_commit_with_one_parent_is_not_the_head(self):
        run = self.resolve({PULL: [pull()], MERGE_REF: [merge_ref(MERGE)],
                            commit(MERGE): [ok({'parents': [{'sha': HEAD}]})]})
        self.assertEqual(run.returncode, 0, run.stdout)
        # Not stale, which would tell the requester it merges an earlier
        # head.
        self.assertEqual(run.written(), {'head_sha': HEAD, 'ref': 'refs/pull/42/head', 'merged': 'false',
                                         'reason': 'unreadable'})
        self.assertIn('which is not a merge', run.stdout)

    def test_a_merge_ref_naming_no_commit_is_not_read(self):
        # --jq prints "null" for an absent .object.sha and exits 0. No
        # commit is asked for, so none appears in the log as missing.
        cases = {'no object': {}, 'no sha': {'object': {}}, 'short sha': {'object': {'sha': MERGE[:7]}}}
        for name, body in cases.items():
            with self.subTest(case=name):
                run = self.resolve({PULL: [pull()], MERGE_REF: [ok(body)]})
                self.assertEqual(run.returncode, 0, run.stdout)
                self.assertEqual(run.written(), {'head_sha': HEAD, 'ref': 'refs/pull/42/head', 'merged': 'false',
                                                 'reason': 'unreadable'})
                self.assertFalse([call for call in run.gh_calls if '/commits/' in call], run.gh_calls)
                self.assertIn('not a commit', run.stdout)

    def test_an_unreadable_pull_request_fails_rather_than_guessing(self):
        run = self.resolve({PULL: [NOT_FOUND]})
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.outputs, '')

    def test_a_head_that_is_not_a_sha_fails(self):
        # A short sha is hex but can never equal a merge commit's
        # parent, so it would end as a race that never happened.
        for head_sha in ('HEAD', 'A' * 40, '%s; rm -rf /' % HEAD, HEAD[:7], HEAD + 'a'):
            with self.subTest(head_sha=head_sha):
                run = self.resolve({PULL: [pull(head_sha=head_sha)]})
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(run.outputs, '')

    def test_a_pull_request_number_that_is_not_a_number_fails_before_any_call(self):
        for number in ('', '42/../1', '4 2'):
            with self.subTest(number=number):
                run = self.resolve({PULL: [pull()]}, pr_number=number)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(run.gh_calls, [])
                self.assertEqual(run.outputs, '')


def git(cwd, *args):
    return subprocess.run(
        ['git', '-c', 'user.name=test', '-c', 'user.email=test@example.com', '-c', 'commit.gpgsign=false']
        + list(args), cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True).stdout.strip()


class ConfirmTest(unittest.TestCase):
    """Confirm against a repository holding a base, two heads and their merges.

    head is the commit resolve validated; newer is a push that landed
    between resolve and the checkout.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = cls.tmp.name
        git(cls.repo, 'init', '-q', '-b', 'main')
        cls.commit_file('base')
        cls.base = git(cls.repo, 'rev-parse', 'HEAD')
        git(cls.repo, 'checkout', '-q', '-b', 'feature')
        cls.commit_file('head')
        cls.head = git(cls.repo, 'rev-parse', 'HEAD')
        cls.commit_file('newer')
        cls.newer = git(cls.repo, 'rev-parse', 'HEAD')
        git(cls.repo, 'checkout', '-q', 'main')
        cls.commit_file('main moved on')
        cls.merge = cls.merge_of(cls.head)
        cls.newer_merge = cls.merge_of(cls.newer)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def commit_file(cls, content):
        with open(os.path.join(cls.repo, 'file'), 'w') as f:
            f.write(content)
        git(cls.repo, 'add', 'file')
        git(cls.repo, 'commit', '-q', '-m', content)

    @classmethod
    def merge_of(cls, sha):
        """A merge of sha into main, as GitHub publishes it: parents are base then head."""
        tree = git(cls.repo, 'rev-parse', '%s^{tree}' % sha)
        return git(cls.repo, 'commit-tree', tree, '-p', 'main', '-p', sha, '-m', 'merge')

    def confirm(self, checked_out, head_sha, merged, command=('confirm',)):
        git(self.repo, 'checkout', '-q', '--detach', checked_out)
        return run_script(list(command), env={'HEAD_SHA': head_sha, 'MERGED': merged}, cwd=self.repo)

    def test_the_validated_merge_is_confirmed(self):
        run = self.confirm(self.merge, self.head, 'true')
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(run.outputs, '')

    def test_the_validated_head_is_confirmed(self):
        run = self.confirm(self.head, self.head, 'false')
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(run.outputs, '')

    def test_a_head_that_moved_fails_and_says_so(self):
        cases = {
            # The merge ref was republished for the newer head.
            'merge path': (self.newer_merge, 'true'),
            # The head ref moved on.
            'head path': (self.newer, 'false'),
        }
        for name, (checked_out, merged) in cases.items():
            with self.subTest(path=name):
                run = self.confirm(checked_out, self.head, merged)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(run.outputs, 'cause=moved\n')
                self.assertIn('Comment again', run.stdout)

    def test_a_defect_in_the_workflow_is_not_blamed_on_a_moved_head(self):
        # These fail too, but asking again would hit the same failure, so
        # the notice must not say to.
        cases = {
            'merge path on a commit that is not a merge': (self.head, self.head, 'true'),
            'merged neither true nor false': (self.merge, self.head, ''),
            'merged in the wrong case': (self.merge, self.head, 'TRUE'),
            'no validated head': (self.head, '', 'false'),
            'a validated head that is not a full sha': (self.head, self.head[:7], 'false'),
        }
        for name, (checked_out, head_sha, merged) in cases.items():
            with self.subTest(case=name):
                run = self.confirm(checked_out, head_sha, merged)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(run.outputs, '')

    def test_the_merge_path_compares_the_merged_head_not_the_merge(self):
        # HEAD on the merge path is the merge commit itself, which is
        # never the validated head, so comparing it would fail every
        # merge review; comparing HEAD^1 would confirm the base.
        run = self.confirm(self.merge, self.merge, 'true')
        self.assertNotEqual(run.returncode, 0)
        run = self.confirm(self.merge, self.base, 'true')
        self.assertNotEqual(run.returncode, 0)

    def test_an_unknown_mode_fails(self):
        for command in (['confim'], [], ['']):
            with self.subTest(command=command):
                run = self.confirm(self.head, self.head, 'false', command=command)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(run.outputs, '')


class ActionTest(unittest.TestCase):
    """action.yml hands the script what it needs and exports what it writes."""

    # What the runner substitutes for the expressions in the steps.
    CONTEXT = {
        'github.token': 'x',
        'github.repository': REPO,
    }

    def setUp(self):
        with open(ACTION) as f:
            self.parsed = yaml.safe_load(f)
        self.steps = {s['id']: s for s in self.parsed['runs']['steps']}

    def run_step(self, step_id, inputs, responses=None, cwd=None):
        """Run a step the way the runner would, from the action's own directory."""
        def substitute(match):
            name = match.group(1).strip()
            if name.startswith('inputs.'):
                return inputs.get(name[len('inputs.'):], '')
            if name not in self.CONTEXT:
                raise AssertionError('the step uses ${{ %s }}, which the test does not provide' % name)
            return self.CONTEXT[name]

        step = self.steps[step_id]
        env = {name: re.sub(r'\$\{\{([^}]*)\}\}', substitute, str(value))
               for name, value in step['env'].items()}
        env['GITHUB_ACTION_PATH'] = os.path.dirname(ACTION)
        with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False) as f:
            f.write(step['run'])
        try:
            return run_script([], env=env, responses=responses, cwd=cwd, script=f.name)
        finally:
            os.unlink(f.name)

    def test_the_modes_go_to_the_right_step(self):
        # Every mode but resolve reaches the confirm step, so a mistyped
        # one fails there rather than skipping both and confirming
        # nothing.
        self.assertEqual(self.steps['resolve']['if'].strip(), "inputs.mode == 'resolve'")
        self.assertEqual(self.steps['confirm']['if'].strip(), "inputs.mode != 'resolve'")

    def test_only_resolve_holds_the_token(self):
        # Confirm runs over the pull request's tree and calls no API.
        self.assertIn('GH_TOKEN', self.steps['resolve']['env'])
        self.assertNotIn('GH_TOKEN', self.steps['confirm']['env'])

    def test_the_resolve_step_runs_the_script(self):
        run = self.run_step('resolve', {'mode': 'resolve', 'pr-number': '42'},
                            responses={PULL: [pull()], MERGE_REF: [merge_ref(MERGE)],
                                       commit(MERGE): [merge_commit(HEAD)]})
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(run.written()['ref'], 'refs/pull/42/merge')

    def test_the_confirm_step_runs_the_script(self):
        with tempfile.TemporaryDirectory() as repo:
            git(repo, 'init', '-q')
            git(repo, 'commit', '-q', '--allow-empty', '-m', 'head')
            head = git(repo, 'rev-parse', 'HEAD')
            run = self.run_step('confirm', {'mode': 'confirm', 'head-sha': head, 'merged': 'false'}, cwd=repo)
            self.assertEqual(run.returncode, 0, run.stdout)
            run = self.run_step('confirm', {'mode': 'confirm', 'head-sha': BASE, 'merged': 'false'}, cwd=repo)
            self.assertEqual(run.outputs, 'cause=moved\n')
            run = self.run_step('confirm', {'mode': 'resolv', 'head-sha': head, 'merged': 'false'}, cwd=repo)
            self.assertNotEqual(run.returncode, 0)

    def test_every_output_is_one_the_script_writes(self):
        with open(SCRIPT) as f:
            written = set(re.findall(r'output "(\w+)=', f.read()))
        for name, spec in self.parsed['outputs'].items():
            with self.subTest(output=name):
                match = re.fullmatch(r'\$\{\{\s*steps\.(\w+)\.outputs\.(\w+)\s*\}\}', spec['value'].strip())
                self.assertIsNotNone(match, '%s is not a step output' % name)
                self.assertIn(match.group(1), self.steps)
                self.assertIn(match.group(2), written)


if __name__ == '__main__':
    unittest.main()
