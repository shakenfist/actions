#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Fail unless CI succeeded on the commit a release is being cut from.

Run by the require-ci-success action, which supplies WORKFLOWS (a
newline-separated list of workflow file names), COMMIT_SHA and GH_TOKEN;
the runner supplies GITHUB_REPOSITORY and, optionally,
GITHUB_STEP_SUMMARY. Every read goes through `gh api`, and nothing here
writes to GitHub.

A run counts only if it ran on the default branch, as a push to it or as
a merge queue run for it, from this repository rather than a fork. Every
other event is ignored: a `head_sha` query also returns scheduled runs
(Renovate, CodeQL, configuration export) for as long as the commit is the
tip, and a pull request run's head is never the merged commit. The branch
matters as much as the event, because a push to any other branch runs the
workflow file as that branch has it, and anyone who can push a branch can
make CI there succeed trivially and then tag the commit.

A run's metadata does not tell a tag from a branch, though. Pushing a tag
creates `push` runs whose head_branch is the tag's name, so a tag named
like the default branch, pushed onto any commit, would produce runs that
look like pushes to it. So before any run is judged, the commit must be on
the default branch's first-parent history: the commits its tip reaches by
following first parents, which are the commits the branch itself pointed
at. Being reachable is not enough, because a commit on a feature branch
merged with a merge commit is reachable too, though the branch never
pointed at it, so no push to the branch ever ran CI there. That also
refuses a merge queue run on a queue commit that never landed. Only the
newest few hundred commits are looked through, since a release is tagged
at or near the tip. It is checked once, on the starting commit, because
the walk below follows first parents too, and the first parent of a
commit on that history is on it.

Every run that counts is judged, each at its latest attempt, so a re-run
that succeeded passes and a re-run that failed does not. A workflow
passes only if all of its runs on the commit completed successfully: a
later push run does not outrank a merge queue run on the same commit
that failed. A commit passes when every named workflow passes. If some
have runs and the rest have no run there at all, the commit was only
part-tested, and that fails rather than looking further back.

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
import urllib.parse


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

# The default branch's history is read a hundred commits to a page, and
# the tagged commit must be found on its first-parent chain within this
# many pages. Not an input, for the same reason as MAX_STEPS. A release is
# tagged at or near the tip, so a commit further back than this is refused
# rather than looked for: the bound only ever fails closed, because a
# commit it stops short of is one the gate has not shown to be on the
# branch.
HISTORY_PER_PAGE = 100
MAX_FIRST_PARENT_PAGES = 5

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


def error_status(stdout):
    """The status GitHub gave in an error body, or None if there is none to read."""
    try:
        body = json.loads(stdout)
    except ValueError:
        return None
    return body.get('status') if isinstance(body, dict) else None


def gh_api(path):
    """GET a path through gh api and return the decoded JSON."""
    try:
        result = subprocess.run(['gh', 'api', '--method', 'GET', path], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, check=False)
    except OSError as e:
        raise GitHubError('could not run gh: %s' % e)
    if result.returncode != 0:
        # gh writes the status to stderr and the error body, which names
        # the status too, to stdout. Either tells a 404 from every other
        # failure.
        if '(HTTP 404)' in result.stderr or error_status(result.stdout) == '404':
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


def branch_tip(repo, branch):
    """The sha the default branch points at."""
    path = 'repos/%s/branches/%s' % (repo, urllib.parse.quote(branch, safe='/'))
    body = gh_api(path)
    commit = body.get('commit') if isinstance(body, dict) else None
    tip = commit.get('sha') if isinstance(commit, dict) else None
    if not isinstance(tip, str) or not SHA_RE.fullmatch(tip):
        raise GitHubError('%s names no commit' % path)
    return tip


def history_page(repo, tip, page):
    """One page of the commits reachable from tip, as a map of sha to first parent (None for a root)."""
    path = 'repos/%s/commits?sha=%s&per_page=%d&page=%d' % (repo, tip, HISTORY_PER_PAGE, page)
    body = gh_api(path)
    if not isinstance(body, list):
        raise GitHubError('%s did not list commits' % path)
    first_parents = {}
    for entry in body:
        commit = entry.get('sha') if isinstance(entry, dict) else None
        parents = entry.get('parents') if isinstance(entry, dict) else None
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit) or not isinstance(parents, list):
            raise GitHubError('%s lists a commit with no sha or no parents' % path)
        parent = parents[0].get('sha') if parents and isinstance(parents[0], dict) else None
        if parents and (not isinstance(parent, str) or not SHA_RE.fullmatch(parent)):
            raise GitHubError('%s names a parent of %s that is not a sha' % (path, commit))
        first_parents[commit] = parent
    if page == 1 and (not body or body[0].get('sha') != tip):
        raise GitHubError('%s does not start at %s' % (path, tip))
    return first_parents, len(body) < HISTORY_PER_PAGE


def require_on_branch(repo, branch, sha, workflows):
    """Raise GateFailure unless sha is on the default branch's first-parent history.

    Return how many first parents were followed from the tip to reach it.
    """
    # The tip is read from the branch itself and the history listed from
    # that sha, never from the branch's name: a tag named like the branch
    # would make the name ambiguous, and git resolves an ambiguous name to
    # the tag.
    tip = branch_tip(repo, branch)
    why_not = ('A run there is not CI on %s, whatever its head_branch says: a tag named like a branch gives its '
               'runs that branch name, so only a commit %s itself pointed at can carry CI for it. Tag a commit '
               'on the first-parent history of %s that CI succeeded on.' % (branch, branch, branch))
    summary = {workflow: 'not checked: %s is not on the first-parent history of %s' % (sha, branch)
               for workflow in workflows}
    first_parents = {}
    pages = 0
    ended = False
    commit = tip
    steps = 0
    while commit != sha:
        if commit not in first_parents:
            if ended:
                raise GitHubError('the history of %s ends without listing %s, a first parent on it'
                                  % (branch, commit))
            if pages == MAX_FIRST_PARENT_PAGES:
                raise GateFailure(
                    'The tagged commit %s is not on the first-parent history of %s as far back as the gate '
                    'reads it, the newest %d commits listed. A release is tagged at or near the tip, so the gate '
                    'looks no further. %s' % (sha, branch, HISTORY_PER_PAGE * MAX_FIRST_PARENT_PAGES, why_not),
                    summary)
            pages += 1
            batch, ended = history_page(repo, tip, pages)
            first_parents.update(batch)
            continue
        # Every step goes to an older commit, so a chain longer than the
        # listing is one that loops, which no real history does.
        if steps > len(first_parents):
            raise GitHubError('the history of %s loops back on itself at %s' % (branch, commit))
        parent = first_parents[commit]
        if parent is None:
            raise GateFailure(
                'The tagged commit %s is not on the first-parent history of %s, which ends at the root commit '
                '%s without reaching it. %s' % (sha, branch, commit, why_not), summary)
        commit = parent
        steps += 1
    return steps


def list_runs(repo, workflow, sha):
    """Every run of a workflow on a commit, or NotFound if there is no such workflow."""
    runs = []
    for page in range(1, MAX_RUN_PAGES + 1):
        path = 'repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=%d&page=%d' % (
            repo, workflow, sha, RUNS_PER_PAGE, page)
        body = gh_api(path)
        batch = body.get('workflow_runs') if isinstance(body, dict) else None
        total = body.get('total_count') if isinstance(body, dict) else None
        if not isinstance(batch, list) or not isinstance(total, int) or isinstance(total, bool):
            raise GitHubError('%s did not list workflow runs' % path)
        if not all(isinstance(run, dict) for run in batch):
            raise GitHubError('%s lists a run that is not an object' % path)
        runs.extend(batch)
        if len(runs) >= total:
            return runs
        # A missing run could be the one that failed, so a listing that
        # stops short of its own count is not judged.
        if not batch:
            raise GitHubError('the listing ended early: %s lists %d of %d runs of %s on %s'
                              % (path, len(runs), total, workflow, sha))
    raise GitHubError('%s has more than %d runs on %s, too many to judge'
                      % (workflow, RUNS_PER_PAGE * MAX_RUN_PAGES, sha))


def qualifies(run, repo, sha, branch):
    """Whether a run is a push to, or a merge queue run for, the default branch, on this commit."""
    # The listing is asked for this commit's runs. Checking again costs
    # nothing, and a filter GitHub silently ignored would otherwise judge
    # some other commit's run.
    if run.get('head_sha') != sha:
        return False
    # A push or merge queue run comes from this repository. One that came
    # from anywhere else ran on code nobody here merged.
    head_repository = run.get('head_repository')
    full_name = head_repository.get('full_name') if isinstance(head_repository, dict) else None
    if not isinstance(full_name, str) or full_name.lower() != repo.lower():
        return False
    head_branch = run.get('head_branch') or ''
    if run.get('event') == 'push':
        return head_branch == branch
    if run.get('event') == 'merge_group':
        return head_branch.startswith('gh-readonly-queue/%s/' % branch)
    return False


def qualifying_runs(repo, workflow, sha, branch):
    """The latest attempt of every qualifying run, oldest run first; empty if there is none."""
    latest = {}
    for run in list_runs(repo, workflow, sha):
        if not qualifies(run, repo, sha, branch):
            continue
        if not isinstance(run.get('run_number'), int) or not isinstance(run.get('run_attempt'), int):
            raise GitHubError('a run of %s on %s has no run_number or run_attempt' % (workflow, sha))
        # The listing shows each run once, at its latest attempt. Keeping
        # the highest attempt of each run is right whether that holds or
        # every attempt is listed separately.
        number = run['run_number']
        if number not in latest or run['run_attempt'] > latest[number]['run_attempt']:
            latest[number] = run
    return [latest[number] for number in sorted(latest)]


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
    if not isinstance(files, list):
        raise GitHubError('%s does not list changed files' % path)
    if not files:
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


def urls(runs):
    return ', '.join(str(run.get('html_url')) for run in runs)


def judge(workflows, commit, found):
    """Pass or fail a commit on which at least one named workflow qualified.

    found maps each workflow that qualified to its runs there, each at its
    latest attempt. Every one of them must have succeeded.
    """
    summary = {}
    problems = []
    for workflow in workflows:
        runs = found.get(workflow)
        if not runs:
            summary[workflow] = 'no push or merge queue run on %s' % commit
            continue
        failed = [run for run in runs if not succeeded(run)]
        if not failed:
            summary[workflow] = 'succeeded on %s: %s' % (commit, urls(runs))
            continue
        summary[workflow] = '; '.join('%s on %s: %s' % (state(run), commit, run.get('html_url')) for run in failed)
        for run in failed:
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
    workflow to its runs there.
    """
    branch = default_branch(repo)
    steps = require_on_branch(repo, branch, sha, workflows)
    print('%s is on the first-parent history of %s, %d commit%s below its tip.'
          % (sha, branch, steps, '' if steps == 1 else 's'))
    commit = sha
    crossed = []
    while True:
        found = {}
        for workflow in workflows:
            try:
                runs = qualifying_runs(repo, workflow, commit, branch)
            except NotFound:
                raise GateFailure(
                    '%s has no workflow named %s, or the token cannot read it. Name the workflow file as it is '
                    'in .github/workflows/.' % (repo, workflow), {workflow: 'no such workflow in %s' % repo})
            if runs:
                found[workflow] = runs
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
        print('  %s: %s' % (workflow, urls(runs[workflow])))
    if crossed:
        print('The tag is on %s, %d commit%s above it. Crossed, each changing only review marks: %s'
              % (sha, len(crossed), '' if len(crossed) == 1 else 's', ', '.join(crossed)))
    write_summary(env, sha, summary, passed=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(os.environ))
