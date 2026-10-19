#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Run require-ci-success's script for real.

tools/require-ci-success.py is what stands between a tag and a publish,
and the way it fails that matters is a pass it should not have given: a
scheduled run, a pull request run or a run on someone's branch read as
CI, or a walk to an ancestor across a commit that changed code. None of
those looks wrong in the log. So the script is run here against a stub
`gh` that answers the way the API does, and each test says what the
gate must refuse as well as what it must accept.
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


SCRIPT = os.path.join(REPO_ROOT, 'tools', 'require-ci-success.py')
ACTION = os.path.join(REPO_ROOT, 'require-ci-success', 'action.yml')

REPO = 'shakenfist/example'


def sha(n):
    return '%040x' % n


TAG = sha(1)
PARENT = sha(2)
GRANDPARENT = sha(3)
OTHER = sha(99)


# Answers `gh api --method GET PATH` from FAKE_GH_RESPONSES, a JSON map of
# path to {"status": ..., "body": ...}. As gh does, a 200 prints the body
# and an HTTP error writes its body to *stdout*, "gh: <message> (HTTP
# <status>)" to stderr, and exits 1. A response may give "stdout" or
# "stderr" to print in place of those. Any other argument list, or a path
# nobody provided, exits 99, so a script that changes what it asks fails
# its test rather than reading an answer meant for another call. Every
# call is appended to FAKE_GH_LOG.
FAKE_GH = """\
import json, os, sys

args = sys.argv[1:]
with open(os.environ['FAKE_GH_LOG'], 'a') as f:
    f.write(' '.join(args) + '\\n')
with open(os.environ['FAKE_GH_RESPONSES']) as f:
    responses = json.load(f)
if len(args) != 4 or args[:3] != ['api', '--method', 'GET'] or args[3] not in responses:
    sys.stderr.write('fake gh: nothing to answer %r with\\n' % args)
    sys.exit(99)
response = responses[args[3]]
sys.stdout.write(response['stdout'] if 'stdout' in response else json.dumps(response['body']))
if response['status'] != 200:
    sys.stderr.write(response['stderr'] if 'stderr' in response
                     else 'gh: %s (HTTP %d)\\n' % (response['body'].get('message'), response['status']))
    sys.exit(1)
"""


def ok(body):
    return {'status': 200, 'body': body}


def error(status, message):
    return {'status': status, 'body': {'message': message, 'status': str(status)}}


def run(head_sha=TAG, event='push', branch='develop', status='completed', conclusion='success', number=10,
        attempt=1, repository=REPO):
    """One entry of a workflow runs listing."""
    return {
        'head_sha': head_sha, 'event': event, 'head_branch': branch, 'status': status,
        'conclusion': conclusion if status == 'completed' else None, 'run_number': number, 'run_attempt': attempt,
        'html_url': 'https://github.com/%s/actions/runs/%d%d' % (REPO, number, attempt),
        'head_repository': None if repository is None else {'full_name': repository},
    }


def queued(head_sha=TAG, queue='develop', **kwargs):
    """A merge queue run on the named branch's queue."""
    return run(head_sha=head_sha, event='merge_group', branch='gh-readonly-queue/%s/pr-7-%s' % (queue, OTHER),
               **kwargs)


def runs_path(workflow, commit, page=1):
    return 'repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=100&page=%d' % (REPO, workflow, commit, page)


def commit_path(commit):
    return 'repos/%s/commits/%s' % (REPO, commit)


def branch_path(branch='develop'):
    return 'repos/%s/branches/%s' % (REPO, branch)


def history_path(tip, page=1):
    return 'repos/%s/commits?sha=%s&per_page=100&page=%d' % (REPO, tip, page)


class GitHub:
    """What the stub gh answers with: a repository, its commits and its workflow runs.

    The tagged commit is the tip of the default branch unless a test says
    otherwise.
    """

    def __init__(self, default='develop'):
        self.default = default
        self.responses = {'repos/%s' % REPO: ok({'default_branch': default})}
        self.tip(TAG)

    def tip(self, commit):
        """The commit the default branch points at."""
        self.responses[branch_path(self.default)] = ok({'name': self.default, 'commit': {'sha': commit}})
        return self

    def history(self, *entries):
        """The default branch's history, newest first, as (sha, parents) pairs; the first is its tip.

        It is listed a hundred commits to a page, as GitHub lists it, and a
        listing that fills its last page is followed by an empty one.
        """
        tip = entries[0][0]
        self.tip(tip)
        listing = [{'sha': commit, 'parents': [{'sha': p} for p in parents]} for commit, parents in entries]
        pages = [listing[n:n + 100] for n in range(0, len(listing), 100)]
        if len(listing) % 100 == 0:
            pages.append([])
        for n, page in enumerate(pages):
            self.responses[history_path(tip, n + 1)] = ok(page)
        return self

    def runs(self, workflow, commit, *entries, page=1, total=None):
        self.responses[runs_path(workflow, commit, page)] = ok(
            {'total_count': len(entries) if total is None else total, 'workflow_runs': list(entries)})
        return self

    def commit(self, commit, parents, files):
        self.responses[commit_path(commit)] = ok({
            'sha': commit, 'parents': [{'sha': p} for p in parents],
            'files': [f if isinstance(f, dict) else {'filename': f} for f in files]})
        return self

    def review_marks(self, commit, parent):
        """A commit as prune-reviews leaves it."""
        return self.commit(commit, [parent], ['.vscode/mikal.weaudit', '.vscode/mikal.weaudit-shas.json',
                                              'REVIEWS.md'])


class Result:
    """What one run of the script did."""

    def __init__(self, returncode, stdout, gh_calls, summary):
        self.returncode = returncode
        self.stdout = stdout
        self.gh_calls = gh_calls
        self.summary = summary

    def asked(self, path):
        return 'api --method GET %s' % path in self.gh_calls

    def errors(self):
        return [line[len('::error::'):] for line in self.stdout.splitlines() if line.startswith('::error::')]


def run_script(github=None, workflows='ci.yml', commit=TAG, env=None, command=None):
    """Run the script with a stub gh on PATH."""
    with tempfile.TemporaryDirectory() as tmp:
        gh = os.path.join(tmp, 'gh')
        with open(gh, 'w') as f:
            f.write('#!%s\n%s' % (sys.executable, FAKE_GH))
        os.chmod(gh, 0o755)
        answers = os.path.join(tmp, 'responses.json')
        with open(answers, 'w') as f:
            json.dump(github.responses if github else {}, f)
        for name in ('gh-log', 'summary'):
            open(os.path.join(tmp, name), 'w').close()
        run_env = {k: v for k, v in os.environ.items() if not k.startswith('GITHUB_') and k != 'GH_TOKEN'}
        run_env.update(
            WORKFLOWS=workflows, COMMIT_SHA=commit, GITHUB_REPOSITORY=REPO, GH_TOKEN='x',
            GITHUB_STEP_SUMMARY=os.path.join(tmp, 'summary'), FAKE_GH_RESPONSES=answers,
            FAKE_GH_LOG=os.path.join(tmp, 'gh-log'), PATH='%s:%s' % (tmp, os.environ['PATH']))
        run_env.update(env or {})
        # A script that loops forever fails its test rather than hanging
        # the suite.
        result = subprocess.run(
            command or [sys.executable, '-I', SCRIPT], env=run_env, check=False, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def read(name):
            with open(os.path.join(tmp, name)) as f:
                return f.read()

        return Result(result.returncode, result.stdout, read('gh-log').splitlines(), read('summary'))


class GateTestCase(unittest.TestCase):
    def assertPasses(self, result, commit=TAG):
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('CI succeeded on %s' % commit, result.stdout)
        self.assertEqual(result.errors(), [])

    def assertFails(self, result, *fragments):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn('Traceback', result.stdout)
        self.assertNotRegex(result.stdout, r'(?m)^CI succeeded on')
        errors = result.errors()
        self.assertEqual(len(errors), 1, result.stdout)
        for fragment in fragments:
            self.assertIn(fragment, errors[0])
        return errors[0]

    def assertDidNotWalk(self, result):
        self.assertFalse([call for call in result.gh_calls if '/commits/' in call], result.gh_calls)


class ExactCommitTest(GateTestCase):
    def test_success_on_the_exact_commit_passes(self):
        result = run_script(GitHub().runs('ci.yml', TAG, run()))
        self.assertPasses(result)
        self.assertIn(run()['html_url'], result.stdout)
        self.assertDidNotWalk(result)

    def test_a_merge_queue_success_passes(self):
        result = run_script(GitHub().runs('ci.yml', TAG, queued()))
        self.assertPasses(result)

    def test_the_default_branch_is_read_not_assumed(self):
        github = GitHub(default='main').runs('ci.yml', TAG, run(branch='main'))
        self.assertPasses(run_script(github))
        # The same push on develop is now a push to some other branch.
        github = GitHub(default='main').runs('ci.yml', TAG, run(branch='develop')).commit(TAG, [PARENT], ['x.py'])
        self.assertFails(run_script(github), 'No named workflow has a push or merge queue run on %s' % TAG)
        github = GitHub(default='main').runs('ci.yml', TAG, queued(queue='main'))
        self.assertPasses(run_script(github))

    def test_every_named_workflow_must_succeed(self):
        github = GitHub().runs('ci.yml', TAG, run()).runs('functional.yml', TAG, queued(number=3))
        result = run_script(github, workflows='ci.yml\nfunctional.yml\n')
        self.assertPasses(result)
        self.assertIn('functional.yml: %s' % run(number=3)['html_url'], result.stdout)

    def test_the_summary_gives_each_workflow_a_verdict(self):
        github = GitHub().runs('ci.yml', TAG, run()).runs('lint.yml', TAG, run(number=4))
        result = run_script(github, workflows='ci.yml\nlint.yml')
        self.assertPasses(result)
        lines = result.summary.splitlines()
        self.assertEqual(lines[1:], ['- `ci.yml`: succeeded on %s: %s' % (TAG, run()['html_url']),
                                     '- `lint.yml`: succeeded on %s: %s' % (TAG, run(number=4)['html_url'])])

    def test_no_summary_is_written_where_there_is_nowhere_to_write_it(self):
        result = run_script(GitHub().runs('ci.yml', TAG, run()), env={'GITHUB_STEP_SUMMARY': ''})
        self.assertPasses(result)


class IgnoredRunsTest(GateTestCase):
    """Runs that are not CI on the default branch are not evidence, so the gate looks past them and fails.

    Each tagged commit here changed code, so there is nothing to walk across.
    """

    def assertIgnored(self, *entries, default='develop'):
        github = GitHub(default=default).runs('ci.yml', TAG, *entries).commit(TAG, [PARENT], ['src/thing.py'])
        result = run_script(github)
        self.assertFails(result, 'No named workflow has a push or merge queue run on %s' % TAG,
                         'changed src/thing.py', 'Tag the last commit CI ran on')
        self.assertIn('- `ci.yml`: no run on %s' % TAG, result.summary)
        return result

    def test_a_scheduled_success_is_ignored(self):
        self.assertIgnored(run(event='schedule'))

    def test_a_pull_request_success_is_ignored(self):
        self.assertIgnored(run(event='pull_request', branch='feature'))
        # Even one whose head branch is the default.
        self.assertIgnored(run(event='pull_request'))

    def test_other_events_are_ignored(self):
        for event in ('workflow_dispatch', 'workflow_run', 'repository_dispatch', 'dynamic'):
            with self.subTest(event=event):
                self.assertIgnored(run(event=event))

    def test_a_push_to_another_branch_is_ignored(self):
        self.assertIgnored(run(branch='feature'))
        self.assertIgnored(run(branch='develop-old'))
        self.assertIgnored(run(branch='v1.0'))

    def test_a_merge_queue_run_for_another_branch_is_ignored(self):
        for branch in ('gh-readonly-queue/main/pr-7-%s' % OTHER, 'gh-readonly-queue/developer/pr-7-%s' % OTHER,
                       'gh-readonly-queue/develop/x/pr-7-%s' % OTHER,
                       'gh-readonly-queue/develop', 'develop'):
            with self.subTest(branch=branch):
                self.assertIgnored(run(event='merge_group', branch=branch))

    def test_a_run_on_another_commit_is_ignored(self):
        # A head_sha filter GitHub silently dropped would list these.
        self.assertIgnored(run(head_sha=OTHER))

    def test_a_run_from_another_repository_is_ignored(self):
        self.assertIgnored(run(repository='someone/example'))
        self.assertIgnored(queued(repository='someone/example'))
        self.assertIgnored(run(repository='shakenfist/example-fork'))
        self.assertIgnored(run(repository=None))
        entry = run()
        del entry['head_repository']
        self.assertIgnored(entry)

    def test_the_repository_is_matched_without_regard_to_case(self):
        self.assertPasses(run_script(GitHub().runs('ci.yml', TAG, run(repository='ShakenFist/Example'))))


TIP = sha(10)
FEATURE = sha(11)
FEATURE_BASE = sha(12)
ROOT = sha(13)


def merged_feature():
    """A tip that merged a feature branch with a merge commit, over TAG.

    TIP's first parent is TAG and its second FEATURE, a commit on the
    feature branch; both descend from ROOT. Listed in date order, the
    feature branch's commits sit between TIP and TAG.
    """
    return GitHub().history((TIP, [TAG, FEATURE]), (FEATURE, [FEATURE_BASE]), (FEATURE_BASE, [ROOT]),
                            (TAG, [ROOT]), (ROOT, []))


def linear(count, start=200):
    """A history of count commits, each the only parent of the one before; the last is a root."""
    chain = [sha(start + n) for n in range(count)]
    return chain, [(commit, [chain[n + 1]] if n + 1 < count else []) for n, commit in enumerate(chain)]


class FirstParentTest(GateTestCase):
    """The commit must be on the default branch's first-parent history before any run on it is judged.

    A tag push creates push runs whose head_branch is the tag's name, so a
    tag named like the default branch makes runs that look like pushes to
    it, on any commit at all. Reachability is not enough: a commit on a
    feature branch merged with a merge commit is reachable from the tip,
    though the branch never pointed at it.
    """

    def assertNotOnBranch(self, result, commit, *fragments):
        self.assertFails(result, 'The tagged commit %s is not on the first-parent history of develop' % commit,
                         'a tag named like a branch gives its runs that branch name', *fragments)
        self.assertFalse(result.asked(runs_path('ci.yml', commit)))
        self.assertDidNotWalk(result)
        self.assertIn('- `ci.yml`: not checked: %s is not on the first-parent history of develop' % commit,
                      result.summary)

    def test_the_tip_of_the_default_branch_passes_without_reading_its_history(self):
        result = run_script(GitHub().runs('ci.yml', TAG, run()))
        self.assertPasses(result)
        self.assertTrue(result.asked(branch_path()))
        self.assertIn('%s is on the first-parent history of develop, 0 commits below its tip' % TAG, result.stdout)
        self.assertFalse([call for call in result.gh_calls if 'commits?' in call], result.gh_calls)

    def test_a_commit_on_the_first_parent_chain_near_the_tip_passes(self):
        result = run_script(merged_feature().runs('ci.yml', TAG, run()))
        self.assertPasses(result)
        self.assertIn('1 commit below its tip', result.stdout)

    def test_a_second_parent_ancestor_fails(self):
        # The attack: FEATURE made CI pass trivially, a later commit put it
        # back, the branch was merged with a merge commit, and a tag named
        # develop pushed at FEATURE gave it a successful push run on
        # "develop".
        github = merged_feature().runs('ci.yml', FEATURE, run(head_sha=FEATURE))
        result = run_script(github, commit=FEATURE)
        self.assertNotOnBranch(result, FEATURE, 'ends at the root commit %s' % ROOT)
        github = merged_feature().runs('ci.yml', FEATURE_BASE, queued(head_sha=FEATURE_BASE))
        self.assertNotOnBranch(run_script(github, commit=FEATURE_BASE), FEATURE_BASE)

    def test_a_commit_off_the_branch_entirely_fails(self):
        github = merged_feature().runs('ci.yml', OTHER, run(head_sha=OTHER))
        self.assertNotOnBranch(run_script(github, commit=OTHER), OTHER, 'ends at the root commit %s' % ROOT)

    def test_a_chain_that_spans_two_pages_passes(self):
        # A merged feature branch of 120 commits, listed between the tip
        # and its first parent, pushes the first parent onto page 2.
        feature = [sha(300 + n) for n in range(120)]
        entries = [(TIP, [PARENT, feature[0]])]
        entries += [(commit, [feature[n + 1] if n + 1 < len(feature) else TAG]) for n, commit in enumerate(feature)]
        entries += [(PARENT, [TAG]), (TAG, [ROOT]), (ROOT, [])]
        result = run_script(GitHub().history(*entries).runs('ci.yml', TAG, run()))
        self.assertPasses(result)
        self.assertIn('2 commits below its tip', result.stdout)
        self.assertTrue(result.asked(history_path(TIP, 2)))
        self.assertFalse(result.asked(history_path(TIP, 3)))

    def test_a_commit_beyond_the_page_bound_fails(self):
        chain, entries = linear(600)
        # The gate reads five pages, the newest 500 commits. The last of
        # them names its first parent, so that is the furthest it reaches.
        github = GitHub().history(*entries).runs('ci.yml', chain[500], run(head_sha=chain[500]))
        result = run_script(github, commit=chain[500])
        self.assertPasses(result, chain[500])
        self.assertIn('500 commits below its tip', result.stdout)
        self.assertFalse(result.asked(history_path(chain[0], 6)))
        # One commit further, the sixth page is never asked for.
        github = GitHub().history(*entries).runs('ci.yml', chain[501], run(head_sha=chain[501]))
        result = run_script(github, commit=chain[501])
        self.assertNotOnBranch(result, chain[501], 'as far back as the gate reads it, the newest 500 commits',
                               'tagged at or near the tip')
        self.assertTrue(result.asked(history_path(chain[0], 5)))
        self.assertFalse(result.asked(history_path(chain[0], 6)))

    def test_a_chain_that_ends_at_a_root_fails(self):
        chain, entries = linear(3)
        github = GitHub().history(*entries).runs('ci.yml', TAG, run())
        self.assertNotOnBranch(run_script(github), TAG, 'ends at the root commit %s' % chain[-1])

    def test_the_history_is_listed_from_the_tip_sha_not_the_branch_name(self):
        # A tag named develop would make the name ambiguous.
        result = run_script(merged_feature().runs('ci.yml', TAG, run()))
        self.assertPasses(result)
        self.assertTrue(result.asked(history_path(TIP)))
        self.assertFalse([call for call in result.gh_calls if 'sha=develop' in call], result.gh_calls)

    def test_the_default_branch_read_is_the_one_whose_tip_is_read(self):
        result = run_script(GitHub(default='main').runs('ci.yml', TAG, run(branch='main')))
        self.assertPasses(result)
        self.assertTrue(result.asked(branch_path('main')))

    def test_a_merge_queue_run_on_a_commit_that_never_landed_fails(self):
        github = merged_feature().runs('ci.yml', OTHER, queued(head_sha=OTHER))
        self.assertNotOnBranch(run_script(github, commit=OTHER), OTHER)

    def test_the_walk_stays_on_the_first_parent_chain(self):
        # Membership is checked once, on the tagged commit. That holds only
        # because the walk crosses nothing but single-parent commits, whose
        # parent is the next commit down the same chain.
        github = (GitHub().history((TIP, [TAG]), (TAG, [PARENT]), (PARENT, [ROOT]), (ROOT, []))
                  .runs('ci.yml', TAG).review_marks(TAG, PARENT).runs('ci.yml', PARENT, run(head_sha=PARENT)))
        result = run_script(github)
        self.assertPasses(result, PARENT)
        self.assertEqual([call for call in result.gh_calls if 'commits?' in call or '/branches/' in call],
                         ['api --method GET %s' % branch_path(), 'api --method GET %s' % history_path(TIP)])
        # A merge commit on the chain is not crossed, even towards a second
        # parent that CI succeeded on: that parent is off the chain.
        github = (GitHub().runs('ci.yml', TAG).commit(TAG, [PARENT, FEATURE], ['REVIEWS.md'])
                  .runs('ci.yml', FEATURE, run(head_sha=FEATURE)))
        result = run_script(github)
        self.assertFails(result, 'because %s is a merge commit' % TAG)
        self.assertFalse(result.asked(runs_path('ci.yml', FEATURE)))

    def test_a_history_that_cannot_be_read_is_a_github_failure(self):
        listing = [{'sha': TIP, 'parents': [{'sha': TAG}]}, {'sha': TAG, 'parents': []}]

        def page_one(*entries):
            return ok([listing[0]] + list(entries))

        cases = {
            'the history, failing': error(502, 'Bad Gateway'),
            'the history, not found': error(404, 'Not Found'),
            'a history that is not a list': ok({'message': 'surprise'}),
            'an empty first page': ok([]),
            'a first page that does not start at the tip': ok(listing[::-1]),
            'an entry that is not an object': page_one('surprise'),
            'an entry with no sha': page_one({'parents': []}),
            'an entry whose sha is not a sha': page_one({'sha': 'HEAD', 'parents': []}),
            'an entry with no parents': page_one({'sha': TAG}),
            'an entry whose parents are not a list': page_one({'sha': TAG, 'parents': {'sha': ROOT}}),
            'a parent that is not an object': ok([{'sha': TIP, 'parents': [TAG]}, listing[1]]),
            'a parent that is not a sha': ok([{'sha': TIP, 'parents': [{'sha': 'HEAD~1'}]}, listing[1]]),
            'a history that ends without the first parent': ok([{'sha': TIP, 'parents': [{'sha': PARENT}]}]),
            'a history that loops': ok([{'sha': TIP, 'parents': [{'sha': PARENT}]},
                                        {'sha': PARENT, 'parents': [{'sha': TIP}]}]),
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                github = GitHub().tip(TIP).runs('ci.yml', TAG, run())
                github.responses[history_path(TIP)] = response
                result = run_script(github)
                self.assertFails(result, 'Could not ask GitHub whether CI succeeded on %s' % TAG, 'this is not a pass')
                self.assertFalse(result.asked(runs_path('ci.yml', TAG)))

    def test_a_malformed_later_page_is_a_github_failure(self):
        chain, entries = linear(150)
        github = GitHub().history(*entries).runs('ci.yml', chain[120], run(head_sha=chain[120]))
        github.responses[history_path(chain[0], 2)] = ok([{'sha': chain[100]}])
        self.assertFails(run_script(github, commit=chain[120]), 'Could not ask GitHub', 'no sha or no parents')


class AttemptsTest(GateTestCase):
    """Every qualifying run is judged, each at its latest attempt.

    The listing shows each run once, at its latest attempt, with its
    run_attempt; listing every attempt separately is judged the same way.
    """

    def test_a_failed_latest_attempt_after_an_earlier_success_fails(self):
        cases = {
            'as listed': [run(attempt=2, conclusion='failure')],
            'every attempt listed': [run(attempt=1), run(attempt=2, conclusion='failure')],
            'every attempt listed, latest first': [run(attempt=2, conclusion='failure'), run(attempt=1)],
        }
        for name, entries in cases.items():
            with self.subTest(case=name):
                result = run_script(GitHub().runs('ci.yml', TAG, *entries))
                self.assertFails(result, 'ci.yml concluded failure on %s' % TAG, run(attempt=2)['html_url'])
                self.assertDidNotWalk(result)

    def test_success_after_a_re_run_passes(self):
        cases = {
            'as listed': [run(attempt=2)],
            'every attempt listed': [run(attempt=1, conclusion='failure'), run(attempt=2)],
            'every attempt listed, latest first': [run(attempt=2), run(attempt=1, conclusion='failure')],
        }
        for name, entries in cases.items():
            with self.subTest(case=name):
                result = run_script(GitHub().runs('ci.yml', TAG, *entries))
                self.assertPasses(result)
                self.assertIn(run(attempt=2)['html_url'], result.stdout)

    def test_every_run_is_judged(self):
        # A later run on the same commit, from a re-queue or a second push,
        # neither supersedes an earlier failure nor is excused by an
        # earlier success.
        result = run_script(GitHub().runs('ci.yml', TAG, run(number=11, conclusion='failure'), run(number=10)))
        self.assertFails(result, 'ci.yml concluded failure on %s' % TAG, run(number=11)['html_url'])
        result = run_script(GitHub().runs('ci.yml', TAG, run(number=10, conclusion='failure'), run(number=11)))
        self.assertFails(result, 'ci.yml concluded failure on %s' % TAG, run(number=10)['html_url'])
        result = run_script(GitHub().runs('ci.yml', TAG, run(number=11), run(number=10, conclusion='failure')))
        self.assertFails(result, 'ci.yml concluded failure on %s' % TAG, run(number=10)['html_url'])

    def test_a_later_push_does_not_outrank_a_failed_merge_queue_run(self):
        result = run_script(GitHub().runs('ci.yml', TAG, run(number=11), queued(number=10, conclusion='failure')))
        message = self.assertFails(result, 'ci.yml concluded failure on %s' % TAG, queued(number=10)['html_url'])
        self.assertNotIn(run(number=11)['html_url'], message)

    def test_every_run_that_failed_is_named(self):
        result = run_script(GitHub().runs('ci.yml', TAG, run(number=11, conclusion='cancelled'),
                                          queued(number=10, conclusion='failure'), run(number=12)))
        self.assertFails(result, 'ci.yml concluded failure', queued(number=10)['html_url'],
                         'ci.yml concluded cancelled', run(number=11)['html_url'])

    def test_several_runs_that_all_succeeded_pass(self):
        result = run_script(GitHub().runs('ci.yml', TAG, queued(number=10), run(number=11)))
        self.assertPasses(result)
        self.assertIn(queued(number=10)['html_url'], result.stdout)
        self.assertIn(run(number=11)['html_url'], result.stdout)

    def test_each_run_is_judged_at_its_latest_attempt(self):
        entries = [run(number=10, attempt=1, conclusion='failure'), run(number=10, attempt=2), run(number=11)]
        self.assertPasses(run_script(GitHub().runs('ci.yml', TAG, *entries)))
        entries = [run(number=10, attempt=2, conclusion='failure'), run(number=10, attempt=1), run(number=11)]
        self.assertFails(run_script(GitHub().runs('ci.yml', TAG, *entries)), run(number=10, attempt=2)['html_url'])

    def test_a_newer_run_that_does_not_count_supersedes_nothing(self):
        result = run_script(GitHub().runs('ci.yml', TAG, run(number=11, event='schedule', conclusion='failure'),
                                          run(number=10)))
        self.assertPasses(result)

    def test_a_run_that_has_not_finished_fails(self):
        for status in ('in_progress', 'queued', 'waiting', 'pending', 'requested'):
            with self.subTest(status=status):
                result = run_script(GitHub().runs('ci.yml', TAG, run(status=status)))
                self.assertFails(result, 'ci.yml is still %s on %s' % (status, TAG), 'Once it has succeeded, re-run')
                self.assertDidNotWalk(result)

    def test_a_run_that_did_not_succeed_fails(self):
        for conclusion in ('failure', 'cancelled', 'timed_out', 'skipped', 'neutral', 'action_required',
                           'startup_failure', 'stale', None):
            with self.subTest(conclusion=conclusion):
                result = run_script(GitHub().runs('ci.yml', TAG, run(conclusion=conclusion)))
                self.assertFails(result, 'ci.yml concluded ')
                self.assertDidNotWalk(result)

    def test_runs_on_later_pages_are_read(self):
        schedules = [run(event='schedule', number=100 + n) for n in range(100)]
        github = (GitHub().runs('ci.yml', TAG, *schedules, total=101)
                  .runs('ci.yml', TAG, run(number=10, conclusion='failure'), page=2, total=101))
        self.assertFails(run_script(github), 'ci.yml concluded failure on %s' % TAG)

    def test_a_listing_that_ends_before_its_count_fails(self):
        github = (GitHub().runs('ci.yml', TAG, run(), run(number=11), total=5)
                  .runs('ci.yml', TAG, page=2, total=5))
        self.assertFails(run_script(github), 'Could not ask GitHub', 'the listing ended early', '2 of 5 runs')
        github = GitHub().runs('ci.yml', TAG, total=3)
        self.assertFails(run_script(github), 'Could not ask GitHub', 'the listing ended early', '0 of 3 runs')

    def test_too_many_runs_to_read_fails(self):
        github = GitHub()
        for page in range(1, 11):
            github.runs('ci.yml', TAG, *[run(event='schedule')] * 100, page=page, total=5000)
        self.assertFails(run_script(github), 'Could not ask GitHub', 'too many to judge')


class PartialSetTest(GateTestCase):
    def test_one_workflow_passing_and_another_with_no_run_fails_without_walking(self):
        github = GitHub().runs('ci.yml', TAG, run()).runs('functional.yml', TAG)
        result = run_script(github, workflows='ci.yml\nfunctional.yml')
        self.assertFails(result, 'functional.yml has no push or merge queue run', 'only part-tested')
        self.assertDidNotWalk(result)
        self.assertIn('- `functional.yml`: no push or merge queue run on %s' % TAG, result.summary)
        self.assertIn('- `ci.yml`: succeeded on %s' % TAG, result.summary)

    def test_a_workflow_with_only_ignored_runs_is_missing(self):
        github = GitHub().runs('ci.yml', TAG, run()).runs('functional.yml', TAG, run(event='schedule'))
        result = run_script(github, workflows='ci.yml\nfunctional.yml')
        self.assertFails(result, 'functional.yml has no push or merge queue run')
        self.assertDidNotWalk(result)

    def test_a_failure_and_a_missing_workflow_are_both_named(self):
        github = (GitHub().runs('ci.yml', TAG, run(conclusion='failure')).runs('lint.yml', TAG)
                  .runs('functional.yml', TAG))
        result = run_script(github, workflows='ci.yml\nlint.yml\nfunctional.yml')
        self.assertFails(result, 'ci.yml concluded failure', 'lint.yml, functional.yml have no push')
        self.assertDidNotWalk(result)


class WalkTest(GateTestCase):
    def test_a_walk_across_one_review_mark_commit(self):
        github = GitHub().runs('ci.yml', TAG).review_marks(TAG, PARENT).runs('ci.yml', PARENT, run(head_sha=PARENT))
        result = run_script(github)
        self.assertPasses(result, PARENT)
        self.assertIn('The tag is on %s, 1 commit above it' % TAG, result.stdout)
        self.assertIn('- `ci.yml`: succeeded on %s' % PARENT, result.summary)

    def test_a_walk_across_two_review_mark_commits(self):
        github = (GitHub().runs('ci.yml', TAG).review_marks(TAG, PARENT)
                  .runs('ci.yml', PARENT).review_marks(PARENT, GRANDPARENT)
                  .runs('ci.yml', GRANDPARENT, queued(head_sha=GRANDPARENT)))
        result = run_script(github)
        self.assertPasses(result, GRANDPARENT)
        self.assertIn('2 commits above it', result.stdout)

    def test_every_named_workflow_is_looked_for_on_each_commit(self):
        github = (GitHub().runs('ci.yml', TAG).runs('lint.yml', TAG).review_marks(TAG, PARENT)
                  .runs('ci.yml', PARENT, run(head_sha=PARENT)).runs('lint.yml', PARENT, run(head_sha=PARENT)))
        self.assertPasses(run_script(github, workflows='ci.yml\nlint.yml'), PARENT)

    def test_an_ancestor_is_judged_like_the_tag(self):
        # Walking reaches a run; it does not lower the bar for it.
        github = (GitHub().runs('ci.yml', TAG).review_marks(TAG, PARENT)
                  .runs('ci.yml', PARENT, run(head_sha=PARENT, conclusion='failure')))
        self.assertFails(run_script(github), 'ci.yml concluded failure on %s' % PARENT)
        github = (GitHub().runs('ci.yml', TAG).runs('lint.yml', TAG).review_marks(TAG, PARENT)
                  .runs('ci.yml', PARENT, run(head_sha=PARENT)).runs('lint.yml', PARENT))
        result = run_script(github, workflows='ci.yml\nlint.yml')
        self.assertFails(result, 'CI ran on %s, but lint.yml has no push' % PARENT)
        self.assertFalse(result.asked(commit_path(PARENT)))

    def test_a_walk_is_refused_across_a_commit_that_touched_readme(self):
        github = (GitHub().runs('ci.yml', TAG).review_marks(TAG, PARENT)
                  .runs('ci.yml', PARENT).commit(PARENT, [GRANDPARENT], ['REVIEWS.md', 'README.md'])
                  .runs('ci.yml', GRANDPARENT, run(head_sha=GRANDPARENT)))
        result = run_script(github)
        self.assertFails(result, 'on %s or the 1 commits below it' % TAG, 'because %s changed README.md' % PARENT,
                         'Tag the last commit CI ran on, or re-run CI')
        self.assertFalse(result.asked(runs_path('ci.yml', GRANDPARENT)))

    def test_a_tagged_commit_that_touched_code_is_not_walked_from(self):
        github = GitHub().runs('ci.yml', TAG).commit(TAG, [PARENT], ['REVIEWS.md', 'src/main.rs'])
        self.assertFails(run_script(github), 'because %s changed src/main.rs' % TAG)

    def test_a_merge_commit_is_not_walked_across(self):
        # Even one whose first-parent diff is review marks alone.
        github = (GitHub().runs('ci.yml', TAG).commit(TAG, [PARENT, OTHER], ['REVIEWS.md'])
                  .runs('ci.yml', PARENT, run(head_sha=PARENT)))
        result = run_script(github)
        self.assertFails(result, 'because %s is a merge commit' % TAG)
        self.assertFalse(result.asked(runs_path('ci.yml', PARENT)))

    def test_a_root_commit_ends_the_walk(self):
        github = GitHub().runs('ci.yml', TAG).commit(TAG, [], ['REVIEWS.md'])
        self.assertFails(run_script(github), 'because %s has no parent' % TAG)

    def test_only_the_files_prune_reviews_writes_are_crossed(self):
        crossable = [['REVIEWS.md'], ['.vscode/mikal.weaudit'], ['.vscode/imports.weaudit-shas.json'],
                     [{'filename': 'REVIEWS.md', 'status': 'removed'}]]
        refused = {
            'docs': ['docs/index.md'],
            'a nested review file': ['docs/REVIEWS.md'],
            'a sidecar in a subdirectory': ['.vscode/x/mikal.weaudit'],
            'the review scope': ['.vscode/review-scope.toml'],
            'editor settings': ['.vscode/settings.json'],
            'a script named like a mark': ['.vscode/mikal.weaudit.py'],
            'a case variant': ['reviews.md'],
            'a rename into a review file': [{'filename': 'REVIEWS.md', 'previous_filename': 'setup.py',
                                             'status': 'renamed'}],
            'no files listed': [],
            'a list that may be truncated': ['.vscode/m%d.weaudit' % n for n in range(300)],
        }
        for files in crossable:
            with self.subTest(files=files):
                github = (GitHub().runs('ci.yml', TAG).commit(TAG, [PARENT], files)
                          .runs('ci.yml', PARENT, run(head_sha=PARENT)))
                self.assertPasses(run_script(github), PARENT)
        for name, files in refused.items():
            with self.subTest(case=name):
                github = (GitHub().runs('ci.yml', TAG).commit(TAG, [PARENT], files)
                          .runs('ci.yml', PARENT, run(head_sha=PARENT)))
                result = run_script(github)
                self.assertFails(result, 'cannot look further back')
                self.assertFalse(result.asked(runs_path('ci.yml', PARENT)))

    def test_the_walk_stops_after_twenty_steps(self):
        chain = [sha(100 + n) for n in range(22)]

        def github(passing):
            g = GitHub().tip(chain[0])
            for n, commit in enumerate(chain[:-1]):
                g.runs('ci.yml', commit).review_marks(commit, chain[n + 1])
            return g.runs('ci.yml', chain[passing], run(head_sha=chain[passing]))

        # Twenty commits crossed to reach the run passes.
        self.assertPasses(run_script(github(20), commit=chain[0]), chain[20])
        # A twenty-first does not, and is never asked about.
        result = run_script(github(21), commit=chain[0])
        self.assertFails(result, 'no further than 20 commits back', 'Tag the last commit CI ran on')
        self.assertFalse(result.asked(commit_path(chain[20])))
        self.assertFalse(result.asked(runs_path('ci.yml', chain[21])))


class InputTest(GateTestCase):
    def test_no_workflows_is_an_error(self):
        for workflows in ('', '\n', '  \n\t\n'):
            with self.subTest(workflows=workflows):
                result = run_script(GitHub(), workflows=workflows)
                self.assertFails(result, 'No workflows were named')
                self.assertEqual(result.gh_calls, [])

    def test_blank_lines_and_surrounding_whitespace_are_ignored(self):
        github = GitHub().runs('ci.yml', TAG, run()).runs('lint.yaml', TAG, run())
        result = run_script(github, workflows='\n  ci.yml  \n\n\tlint.yaml\nci.yml\n')
        self.assertPasses(result)
        self.assertEqual(sum(1 for call in result.gh_calls if '/workflows/ci.yml/' in call), 1)

    def test_a_workflow_that_is_not_a_file_name_is_an_error(self):
        for name in ('.github/workflows/ci.yml', 'ci', '../ci.yml', 'ci.yml?x=1', 'c i.yml', '-ci.yml'):
            with self.subTest(name=name):
                result = run_script(GitHub(), workflows=name)
                self.assertFails(result, 'is not a workflow file name')
                self.assertEqual(result.gh_calls, [])

    def test_a_commit_that_is_not_a_full_sha_is_an_error(self):
        for commit in ('', TAG[:7], 'A' * 40, 'refs/tags/v1.0', TAG + 'a'):
            with self.subTest(commit=commit):
                result = run_script(GitHub(), commit=commit)
                self.assertFails(result, 'is not a full commit sha')
                self.assertEqual(result.gh_calls, [])

    def test_a_missing_repository_or_token_is_an_error(self):
        result = run_script(GitHub(), env={'GITHUB_REPOSITORY': ''})
        self.assertFails(result, 'GITHUB_REPOSITORY')
        result = run_script(GitHub(), env={'GH_TOKEN': ''})
        self.assertFails(result, 'GH_TOKEN is not set')
        self.assertEqual(result.gh_calls, [])

    def test_an_error_is_one_workflow_command(self):
        # The runner reads a workflow command to the end of its line, so a
        # message carrying a newline would lose the rest.
        result = run_script(GitHub(), workflows='ci.yml\n50%\n')
        message = self.assertFails(result)
        self.assertIn('50%25', message)


class GitHubFailureTest(GateTestCase):
    def test_a_workflow_file_that_does_not_exist_fails_naming_it(self):
        github = GitHub().runs('ci.yml', TAG, run())
        github.responses[runs_path('functional.yml', TAG)] = error(404, 'Not Found')
        result = run_script(github, workflows='ci.yml\nfunctional.yml')
        self.assertFails(result, '%s has no workflow named functional.yml, or the token cannot read it' % REPO)
        self.assertIn('- `functional.yml`: no such workflow', result.summary)
        self.assertDidNotWalk(result)

    def test_a_404_is_read_from_stderr_or_from_the_body(self):
        cases = {
            'stderr only': {'status': 404, 'body': {'message': 'Not Found'}},
            'body only': {'status': 404, 'body': {'message': 'Not Found', 'status': '404'},
                          'stderr': 'gh: Not Found\n'},
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                github = GitHub().runs('ci.yml', TAG, run())
                github.responses[runs_path('functional.yml', TAG)] = response
                result = run_script(github, workflows='ci.yml\nfunctional.yml')
                self.assertFails(result, '%s has no workflow named functional.yml' % REPO)

    def test_any_other_failure_to_ask_github_fails(self):
        cases = {
            'the repository': ('repos/%s' % REPO, error(403, 'Resource not accessible by integration')),
            'the runs': (runs_path('ci.yml', TAG), error(502, 'Bad Gateway')),
            'the runs, rate limited': (runs_path('ci.yml', TAG), error(429, 'API rate limit exceeded')),
            'the commit': (commit_path(TAG), error(500, 'Server Error')),
            # Only a missing workflow is a 404 worth naming as such.
            'the commit, not found': (commit_path(TAG), error(404, 'Not Found')),
            'a body that is not a listing': (runs_path('ci.yml', TAG), ok({'message': 'surprise'})),
            'a repository with no default branch': ('repos/%s' % REPO, ok({})),
            'a run with no attempt': (runs_path('ci.yml', TAG), ok({'total_count': 1, 'workflow_runs': [
                {k: v for k, v in run().items() if k != 'run_attempt'}]})),
            'a repository whose default branch is empty': ('repos/%s' % REPO, ok({'default_branch': ''})),
            'the branch': (branch_path(), error(500, 'Server Error')),
            'the branch, not found': (branch_path(), error(404, 'Branch not found')),
            'a branch with no commit': (branch_path(), ok({'name': 'develop'})),
            'a branch whose commit is not a sha': (branch_path(), ok({'name': 'develop', 'commit': {'sha': 'HEAD'}})),
            'a 404 body naming a status that is not 404': (runs_path('ci.yml', TAG), {
                'status': 403, 'body': {'message': 'Forbidden', 'status': '403'}, 'stderr': 'gh: Forbidden\n'}),
            'a failure whose body is not JSON': (runs_path('ci.yml', TAG), {
                'status': 500, 'body': None, 'stdout': '<html>', 'stderr': 'gh: Server Error (HTTP 500)\n'}),
            'a run that is not an object': (runs_path('ci.yml', TAG), ok({'total_count': 1, 'workflow_runs': [
                'surprise']})),
            'a run that is not an object beside one that passes': (runs_path('ci.yml', TAG), ok({
                'total_count': 2, 'workflow_runs': [run(), None]})),
            'a listing with no total': (runs_path('ci.yml', TAG), ok({'workflow_runs': [run()]})),
            'a listing whose total is not an integer': (runs_path('ci.yml', TAG), ok({
                'total_count': '1', 'workflow_runs': [run()]})),
            'a listing whose total is a boolean': (runs_path('ci.yml', TAG), ok({
                'total_count': True, 'workflow_runs': [run()]})),
            'a parent that is not a sha': (commit_path(TAG), ok({
                'parents': [{'sha': 'HEAD~1'}], 'files': [{'filename': 'REVIEWS.md'}]})),
            'files that are not a list': (commit_path(TAG), ok({
                'parents': [{'sha': PARENT}], 'files': {'filename': 'REVIEWS.md'}})),
            'a changed file with no name': (commit_path(TAG), ok({
                'parents': [{'sha': PARENT}], 'files': [{'status': 'added'}]})),
            'a changed file whose name is not a string': (commit_path(TAG), ok({
                'parents': [{'sha': PARENT}], 'files': [{'filename': ['REVIEWS.md']}]})),
            'a changed file that is not an object': (commit_path(TAG), ok({
                'parents': [{'sha': PARENT}], 'files': ['REVIEWS.md']})),
        }
        for name, (path, response) in cases.items():
            with self.subTest(case=name):
                github = GitHub().runs('ci.yml', TAG).commit(TAG, [PARENT], ['REVIEWS.md'])
                github.responses[path] = response
                result = run_script(github)
                self.assertFails(result, 'Could not ask GitHub whether CI succeeded on %s' % TAG,
                                 'this is not a pass')
                self.assertIn('- `ci.yml`: not checked: could not ask GitHub', result.summary)

    def test_a_run_that_is_not_an_object_is_not_skipped(self):
        # Skipping it would judge the commit on the runs around it, and the
        # one that could not be read may be the one that failed.
        for entry in ('surprise', None, ['a', 'list'], 7):
            with self.subTest(entry=entry):
                github = GitHub().runs('ci.yml', TAG, run(), entry)
                self.assertFails(run_script(github), 'Could not ask GitHub', 'lists a run that is not an object')

    def test_gh_that_cannot_run_fails(self):
        with tempfile.TemporaryDirectory() as empty:
            result = run_script(GitHub(), env={'PATH': empty}, command=[sys.executable, '-I', SCRIPT])
        self.assertFails(result, 'Could not ask GitHub', 'could not run gh')


class ActionTest(unittest.TestCase):
    """action.yml hands the script its inputs through env: alone, and runs it."""

    def setUp(self):
        with open(ACTION) as f:
            self.parsed = yaml.safe_load(f)
        self.steps = self.parsed['runs']['steps']

    def test_inputs_reach_the_script_only_through_env(self):
        for step in self.steps:
            with self.subTest(step=step.get('name')):
                # An expression in run: is spliced into the shell before it
                # runs, so an input there could carry shell.
                self.assertNotIn('${{', step['run'])
                for key, value in step.items():
                    if key != 'env':
                        self.assertNotIn('inputs.', json.dumps(value), key)
        referenced = set()
        for step in self.steps:
            for value in step.get('env', {}).values():
                referenced.update(re.findall(r'\$\{\{\s*inputs\.([\w-]+)\s*\}\}', str(value)))
        self.assertEqual(referenced, set(self.parsed['inputs']))

    def test_workflows_is_required_and_has_no_default(self):
        workflows = self.parsed['inputs']['workflows']
        self.assertIs(workflows['required'], True)
        self.assertNotIn('default', workflows)

    def test_the_defaults_are_the_triggering_commit_and_the_job_token(self):
        self.assertEqual(self.parsed['inputs']['sha']['default'], '${{ github.sha }}')
        self.assertEqual(self.parsed['inputs']['token']['default'], '${{ github.token }}')

    def test_the_step_runs_the_script(self):
        step, = self.steps
        inputs = {'workflows': 'ci.yml\n', 'sha': TAG, 'token': 'x'}
        env = {name: re.sub(r'\$\{\{\s*inputs\.([\w-]+)\s*\}\}', lambda m: inputs[m.group(1)], str(value))
               for name, value in step['env'].items()}
        env['GITHUB_ACTION_PATH'] = os.path.dirname(ACTION)
        command = [step['shell'], '-c', step['run']]
        result = run_script(GitHub().runs('ci.yml', TAG, run()), env=env, command=command)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('CI succeeded on %s' % TAG, result.stdout)
        result = run_script(GitHub().runs('ci.yml', TAG, run(conclusion='failure')), env=env, command=command)
        self.assertEqual(result.returncode, 1, result.stdout)


if __name__ == '__main__':
    unittest.main()
