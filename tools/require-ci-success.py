#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Fail unless CI succeeded on the commit a release is being cut from.

Run by the require-ci-success action, which supplies WORKFLOWS (a
newline-separated list of workflow file names), COMMIT_SHA and GH_TOKEN;
the runner supplies GITHUB_REPOSITORY and, optionally,
GITHUB_STEP_SUMMARY. Every read goes through `gh api`, and nothing here
writes to GitHub.

A run counts only if it ran on the default branch, as a push to it or as
a merge queue run for it. Every other event is ignored: a `head_sha`
query also returns scheduled runs (Renovate, CodeQL, configuration
export) for as long as the commit is the tip, and a pull request run's
head is never the merged commit. The branch matters as much as the event,
because a push to any other branch runs the workflow file as that branch
has it, and anyone who can push a branch can make CI there succeed
trivially and then tag the commit.

Of the runs that count, the newest run of each workflow is judged, and
within it the latest attempt, so a re-run that succeeded passes and a
re-run that failed does not. A commit passes when every named workflow
has such a run that completed successfully. If some do and the rest have
no run there at all, the commit was only part-tested, and that fails
rather than looking further back.

A commit that no named workflow ran on at all is usually one the
prune-reviews workflow pushed after the merge CI did run on. So the gate
steps to the first parent, but only across a commit that changed nothing
except review marks, and never across a merge. Anything else fails, and
the recovery is to tag the last commit CI ran on, before anything has
been published.

Any failure to ask GitHub is a failure, never a pass.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys


# The files a commit may change and still be crossed on the way to an
# ancestor that CI ran on: what the prune-reviews workflow commits. Each
# glob matches within its own directory only. This is fixed here rather
# than taken as an input because a per-repository list grows: it would
# gain "docs only", then "anything that is not code", and a release would
# be gated by whatever the last person to edit it thought was harmless.
ALLOWLIST = ('REVIEWS.md', '.vscode/*.weaudit', '.vscode/*.weaudit-shas.json')

# How many commits the walk may cross before it gives up. Not an input for
# the same reason: a per-repository limit only ever grows, each time a
# repository goes long enough without CI to hit it.
MAX_STEPS = 20

# The commits API lists at most this many changed files, and a commit
# that lists this many may have changed more than it shows.
COMMIT_FILES_CAP = 300

# Runs are read a hundred to a page. Ten pages of runs of one workflow on
# one commit is far past anything real, so more than that is refused
# rather than judged on a partial list.
RUNS_PER_PAGE = 100
MAX_RUN_PAGES = 10

SHA_RE = re.compile(r'[0-9a-f]{40}')
REPO_RE = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
WORKFLOW_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml')

RECOVERY = ('Tag the last commit CI ran on, or re-run CI on this commit and then re-run this job '
            'with "Re-run jobs".')


class GateFailure(Exception):
    """CI has not been shown to succeed on the commit.

    summary maps each workflow to a one-line verdict for the job summary.
    """

    def __init__(self, message, summary=None):
        super().__init__(message)
        self.message = message
        self.summary = summary or {}


class GitHubError(Exception):
    """GitHub could not be asked, or gave an answer that cannot be read."""


class NotFound(GitHubError):
    """GitHub answered 404."""


def gh_api(path):
    """GET a path through gh api and return the decoded JSON."""
    try:
        result = subprocess.run(['gh', 'api', '--method', 'GET', path], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, check=False)
    except OSError as e:
        raise GitHubError('could not run gh: %s' % e)
    if result.returncode != 0:
        # gh writes the error body to stdout and the status to stderr, so
        # stderr is what tells a 404 from every other failure.
        if '(HTTP 404)' in result.stderr:
            raise NotFound(path)
        raise GitHubError('gh api %s exited %d: %s' % (path, result.returncode,
                                                       result.stderr.strip() or 'no error text'))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise GitHubError('gh api %s did not return JSON' % path)


def default_branch(repo):
    body = gh_api('repos/%s' % repo)
    branch = body.get('default_branch') if isinstance(body, dict) else None
    if not isinstance(branch, str) or not branch:
        raise GitHubError('repos/%s names no default branch' % repo)
    return branch


def list_runs(repo, workflow, sha):
    """Every run of a workflow on a commit, or NotFound if there is no such workflow."""
    runs = []
    for page in range(1, MAX_RUN_PAGES + 1):
        path = 'repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=%d&page=%d' % (
            repo, workflow, sha, RUNS_PER_PAGE, page)
        body = gh_api(path)
        batch = body.get('workflow_runs') if isinstance(body, dict) else None
        total = body.get('total_count') if isinstance(body, dict) else None
        if not isinstance(batch, list) or not isinstance(total, int):
            raise GitHubError('%s did not list workflow runs' % path)
        runs.extend(batch)
        if not batch or len(runs) >= total:
            return runs
    raise GitHubError('%s has more than %d runs on %s, too many to judge'
                      % (workflow, RUNS_PER_PAGE * MAX_RUN_PAGES, sha))


def qualifies(run, sha, branch):
    """Whether a run is a push to, or a merge queue run for, the default branch, on this commit."""
    # The listing is asked for this commit's runs. Checking again costs
    # nothing, and a filter GitHub silently ignored would otherwise judge
    # some other commit's run.
    if run.get('head_sha') != sha:
        return False
    head_branch = run.get('head_branch') or ''
    if run.get('event') == 'push':
        return head_branch == branch
    if run.get('event') == 'merge_group':
        return head_branch.startswith('gh-readonly-queue/%s/' % branch)
    return False


def latest_run(repo, workflow, sha, branch):
    """The latest attempt of the newest qualifying run, or None if there is none."""
    runs = [run for run in list_runs(repo, workflow, sha) if isinstance(run, dict) and qualifies(run, sha, branch)]
    for run in runs:
        if not isinstance(run.get('run_number'), int) or not isinstance(run.get('run_attempt'), int):
            raise GitHubError('a run of %s on %s has no run_number or run_attempt' % (workflow, sha))
    # The listing shows each run once, at its latest attempt. Taking the
    # highest attempt within the newest run is right whether that holds
    # or every attempt is listed separately.
    return max(runs, key=lambda run: (run['run_number'], run['run_attempt']), default=None)


def succeeded(run):
    return run.get('status') == 'completed' and run.get('conclusion') == 'success'


def state(run):
    if run.get('status') != 'completed':
        return 'is still %s' % (run.get('status') or 'in an unknown state')
    return 'concluded %s' % (run.get('conclusion') or 'with no conclusion')


def describe(run):
    return 'attempt %s of run %s' % (run['run_attempt'], run.get('html_url') or run['run_number'])


def allowlisted(path):
    return any(fnmatch.fnmatchcase(path, pattern) and path.count('/') == pattern.count('/')
               for pattern in ALLOWLIST)


def step_back(repo, sha):
    """Return (parent, None) if the walk may step from sha to its parent, or (None, why) if not."""
    path = 'repos/%s/commits/%s' % (repo, sha)
    body = gh_api(path)
    parents = body.get('parents') if isinstance(body, dict) else None
    files = body.get('files') if isinstance(body, dict) else None
    if not isinstance(parents, list):
        raise GitHubError('%s lists no parents' % path)
    if len(parents) > 1:
        return None, 'is a merge commit, and the gate never walks across one'
    if not parents:
        return None, 'has no parent'
    parent = parents[0].get('sha') if isinstance(parents[0], dict) else None
    if not isinstance(parent, str) or not SHA_RE.fullmatch(parent):
        raise GitHubError('%s names a parent that is not a sha' % path)
    if not isinstance(files, list) or not files:
        return None, 'lists no changed files, so what it changed cannot be checked'
    if len(files) >= COMMIT_FILES_CAP:
        return None, 'lists %d changed files, so the list may be truncated' % len(files)
    changed = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get('filename'), str):
            raise GitHubError('%s lists a changed file with no name' % path)
        changed.append(entry['filename'])
        # A rename out of a path changes that path too.
        if isinstance(entry.get('previous_filename'), str):
            changed.append(entry['previous_filename'])
    outside = sorted(set(name for name in changed if not allowlisted(name)))
    if outside:
        return None, 'changed %s, which %s not review marks' % (
            ', '.join(outside), 'is' if len(outside) == 1 else 'are')
    return parent, None


def judge(workflows, commit, found):
    """Pass or fail a commit on which at least one named workflow qualified."""
    summary = {}
    problems = []
    for workflow in workflows:
        run = found.get(workflow)
        if run is None:
            summary[workflow] = 'no push or merge queue run on %s' % commit
        elif succeeded(run):
            summary[workflow] = 'succeeded on %s: %s' % (commit, run.get('html_url'))
        else:
            summary[workflow] = '%s on %s: %s' % (state(run), commit, run.get('html_url'))
            problems.append('%s %s on %s, in %s. Once it has succeeded, re-run this job.'
                            % (workflow, state(run), commit, describe(run)))
    missing = [workflow for workflow in workflows if workflow not in found]
    if missing:
        problems.append(
            'CI ran on %s, but %s %s no push or merge queue run on the default branch there, so the commit '
            'was only part-tested. Tag a commit that every named workflow succeeded on.'
            % (commit, ', '.join(missing), 'has' if len(missing) == 1 else 'have'))
    if problems:
        raise GateFailure(' '.join(problems), summary)
    return summary


def gate(repo, workflows, sha):
    """Return (commit, crossed, runs, summary) for the commit CI succeeded on, or raise GateFailure.

    crossed is the commits stepped across to reach it, and runs maps each
    workflow to its run there.
    """
    branch = default_branch(repo)
    commit = sha
    crossed = []
    while True:
        found = {}
        for workflow in workflows:
            try:
                run = latest_run(repo, workflow, commit, branch)
            except NotFound:
                raise GateFailure(
                    '%s has no workflow named %s. Name the workflow file as it is in .github/workflows/.'
                    % (repo, workflow), {workflow: 'no such workflow in %s' % repo})
            if run is not None:
                found[workflow] = run
        if found:
            return commit, crossed, found, judge(workflows, commit, found)

        where = sha if not crossed else '%s or the %d commits below it' % (sha, len(crossed))
        if len(crossed) == MAX_STEPS:
            raise GateFailure(
                'No named workflow has a push or merge queue run on %s, and the gate looks no further than '
                '%d commits back. %s' % (where, MAX_STEPS, RECOVERY),
                {workflow: 'no run on %s' % where for workflow in workflows})
        parent, why = step_back(repo, commit)
        if why:
            raise GateFailure(
                'No named workflow has a push or merge queue run on %s, and the gate cannot look further '
                'back, because %s %s. %s' % (where, commit, why, RECOVERY),
                {workflow: 'no run on %s' % where for workflow in workflows})
        print('No named workflow ran on %s, which changed only review marks; trying its parent %s.'
              % (commit, parent))
        crossed.append(commit)
        commit = parent


def read_inputs(env):
    """Return (repo, workflows, sha) from the environment, or raise GateFailure."""
    workflows = []
    for line in env.get('WORKFLOWS', '').splitlines():
        name = line.strip()
        if not name:
            continue
        if not WORKFLOW_RE.fullmatch(name):
            raise GateFailure('%r is not a workflow file name such as ci.yml.' % name)
        if name not in workflows:
            workflows.append(name)
    if not workflows:
        raise GateFailure('No workflows were named. List the workflow files whose success a release needs.')
    sha = env.get('COMMIT_SHA', '')
    if not SHA_RE.fullmatch(sha):
        raise GateFailure('%r is not a full commit sha.' % sha)
    repo = env.get('GITHUB_REPOSITORY', '')
    if not REPO_RE.fullmatch(repo):
        raise GateFailure('GITHUB_REPOSITORY is %r, which is not owner/name.' % repo)
    # Without it gh would fall back to whatever login the runner's home
    # directory holds, which on a shared runner is not this job's.
    if not env.get('GH_TOKEN'):
        raise GateFailure('GH_TOKEN is not set, so there is no token to read CI results with.')
    return repo, workflows, sha


def annotate(message):
    """A workflow command's message, escaped the way the runner unescapes it."""
    return message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def write_summary(env, sha, summary, passed):
    path = env.get('GITHUB_STEP_SUMMARY')
    if not path or not summary:
        return
    lines = ['### CI gate for %s: %s' % (sha, 'passed' if passed else 'failed')]
    lines.extend('- `%s`: %s' % (workflow, verdict) for workflow, verdict in summary.items())
    with open(path, 'a') as f:
        f.write('\n'.join(lines) + '\n')


def main(env):
    sha = env.get('COMMIT_SHA', '')
    try:
        repo, workflows, sha = read_inputs(env)
        try:
            commit, crossed, runs, summary = gate(repo, workflows, sha)
        except GitHubError as e:
            raise GateFailure('Could not ask GitHub whether CI succeeded on %s, so this is not a pass: %s'
                              % (sha, e), {workflow: 'not checked: could not ask GitHub' for workflow in workflows})
    except GateFailure as e:
        print('::error::%s' % annotate(e.message))
        write_summary(env, sha, e.summary, passed=False)
        return 1

    print('CI succeeded on %s:' % commit)
    for workflow in workflows:
        print('  %s: %s' % (workflow, runs[workflow].get('html_url')))
    if crossed:
        print('The tag is on %s, %d commit%s above it. Crossed, each changing only review marks: %s'
              % (sha, len(crossed), '' if len(crossed) == 1 else 's', ', '.join(crossed)))
    write_summary(env, sha, summary, passed=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(os.environ))
