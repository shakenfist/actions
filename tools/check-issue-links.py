#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Verify that a pull request will actually close the issues it says it fixes.

GitHub only acts on issue-closing keywords found in a pull request's
*description*. Two things in this ecosystem hide that from an author:

- A stanza wrapped in backticks is a markdown code span. GitHub renders it
  and parses nothing, so there is no link, no close, and not even a
  cross-reference on the issue. It looks tidier and it is inert.
- A stanza in a commit message closes an issue only when the commit reaches
  the default branch by an ordinary push. A merge queue advances the branch
  from its own staging ref, and commit messages are not parsed on that path,
  so a queued pull request's commit stanzas do nothing at all.

Either one alone silently leaves a fixed issue open. This compares what the
pull request *says* it fixes against `closingIssuesReferences`, which is
GitHub's own parse of the description, and fails while the description can
still be edited.

Intent is read from three places: a branch named for an issue, a standalone
stanza in the description, and a standalone stanza in any commit message. A
stanza only counts when it is essentially the whole line, so prose which
happens to put a keyword next to a reference is not mistaken for a request
to close something. That deliberately errs towards silence: a missed intent
leaves the check quiet, which is exactly where we are today, while a false
alarm would train people to ignore it.

The reverse direction -- an issue GitHub will close that nobody asked it to
-- is reported but never fails the build. Real pull request prose produces
too many innocent-looking matches (headings, table cells, several directives
sharing one line) for a machine to judge, so this prints what will be closed
and leaves the judgement to a human.
"""

import argparse
import json
import os
import re
import subprocess
import sys


# Branch naming conventions which name the issue being fixed.
BRANCH_RE = re.compile(r'(?:issue-fix|bug|fix)-(\d+)$')

# GitHub's own closing keywords, and a reference to an issue in this repo.
KEYWORD = r'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)'
REF_RE = re.compile(r'\b' + KEYWORD + r'\s+#(\d+)\b', re.IGNORECASE)

# An explicit opt out, for a pull request which names an issue it is only
# part of. "X-No-Autoclose: #123" on a line of its own.
OPT_OUT_RE = re.compile(r'^\s*X-No-Autoclose:\s*#(\d+)\s*$',
                        re.IGNORECASE | re.MULTILINE)

# Decoration to peel off a line before judging whether a stanza stands
# alone: markdown emphasis, code spans, list bullets and quote markers.
DECORATION = '`*_>-+ \t'


def standalone_stanzas(text):
    """Issue numbers named by a stanza which is essentially a whole line.

    "Fixes #4087" counts. So does "`Fixes #4087`", which is the bug this
    exists to catch, and "Closes #1. Closes #2." which is two directives
    sharing a line. "The fix #4092 asks for would have been deleted" does
    not, and neither does "Deliberately does not close #3813" -- the words
    left over after removing the match are what separates a directive from
    a sentence, and it is the same test in both directions.
    """
    found = set()
    for line in text.splitlines():
        stripped = line.strip().strip(DECORATION)
        matches = list(REF_RE.finditer(stripped))
        if not matches:
            continue

        remainder = REF_RE.sub(' ', stripped)
        remainder = re.sub(r'[^0-9A-Za-z]+', ' ', remainder)
        if len(remainder.split()) <= 1:
            found.update(int(m.group(1)) for m in matches)
    return found


def declared_intent(branch, body, commit_messages):
    """Every issue this pull request claims, from all three sources."""
    intent = set()

    matched = BRANCH_RE.search(branch or '')
    if matched:
        intent.add(int(matched.group(1)))

    intent |= standalone_stanzas(body or '')
    for message in commit_messages:
        intent |= standalone_stanzas(message or '')

    for opted_out in OPT_OUT_RE.finditer(body or ''):
        intent.discard(int(opted_out.group(1)))

    return intent


def evaluate(branch, body, commit_messages, closing_refs, already_closed=()):
    """Compare declared intent against GitHub's parse of the description.

    ``closing_refs`` is the set of issue numbers GitHub says this pull
    request will close. ``already_closed`` are issues which are shut
    already, and so need no link -- a pull request touching an issue
    somebody else finished should not be made to claim it.
    """
    intent = declared_intent(branch, body, commit_messages)
    missing = sorted(intent - set(closing_refs) - set(already_closed))
    unexpected = sorted(set(closing_refs) - intent)
    return missing, unexpected, sorted(intent)


def commit_messages(pull_request):
    """The full message of every commit on the pull request."""
    messages = []
    for commit in pull_request.get('commits') or []:
        messages.append(
            (commit.get('messageHeadline') or '') + '\n'
            + (commit.get('messageBody') or ''))
    return messages


def same_repo_closing_refs(pull_request, repo):
    """The issues GitHub says this will close, in this repository only."""
    wanted = repo.lower()
    refs = set()
    for ref in pull_request.get('closingIssuesReferences') or []:
        repository = ref.get('repository') or {}
        owner = (repository.get('owner') or {}).get('login', '')
        full = ('%s/%s' % (owner, repository.get('name', ''))).lower()
        if full == wanted:
            refs.add(ref['number'])
    return refs


def _gh_json(args):
    """Run a gh command expected to emit JSON, and parse it."""
    completed = subprocess.run(
        ['gh'] + args, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def _issue_is_closed(repo, number):
    """Whether an issue is already closed, tolerating one that is absent."""
    try:
        state = _gh_json(['issue', 'view', str(number), '--repo', repo,
                          '--json', 'state'])['state']
    except subprocess.CalledProcessError:
        # A reference to something which is not an issue in this repository
        # -- a pull request number, or a deleted issue. Nothing to insist on.
        return True
    return state == 'CLOSED'


def _report(lines):
    text = '\n'.join(lines)
    print(text)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as f:
            f.write(text + '\n')


def build_report(declared, closing_refs, missing, unexpected):
    """The human readable result, shared by the run and its tests."""
    lines = ['## Issue links', '']

    if declared:
        lines.append('Declared as fixed by this pull request: '
                     + ', '.join('#%d' % n for n in declared))
    else:
        lines.append('This pull request declares no issue as fixed.')

    if closing_refs:
        lines.append('GitHub will close on merge: '
                     + ', '.join('#%d' % n for n in sorted(closing_refs)))
    else:
        lines.append('GitHub will close nothing on merge.')

    if unexpected:
        lines += [
            '',
            'Note: GitHub will also close '
            + ', '.join('#%d' % n for n in unexpected)
            + ', which nothing here asked it to. That is usually a closing '
              'keyword sitting next to a reference in ordinary prose. Check '
              'that it is intended -- this does not fail the build.']

    if missing:
        first = missing[0]
        lines += [
            '',
            'FAILED: '
            + ', '.join('#%d' % n for n in missing)
            + ' will NOT be closed when this merges.',
            '',
            'Put a plain, unbackticked stanza on its own line in the pull '
            'request description:',
            '',
            '    Fixes #%d' % first,
            '',
            'A stanza inside backticks is a code span, and GitHub ignores '
            'it. A stanza in a commit message is not parsed at all for '
            'anything merged through a merge queue. Only the description '
            'works on every path. If this pull request is deliberately only '
            'part of the fix, say so with "X-No-Autoclose: #%d" in the '
            'description.' % first]

    return lines


def main():
    parser = argparse.ArgumentParser(
        description='Check a pull request will close what it claims to fix.')
    parser.add_argument('--repo', required=True, help='owner/name')
    parser.add_argument('--pr', required=True, type=int)
    args = parser.parse_args()

    pull_request = _gh_json([
        'pr', 'view', str(args.pr), '--repo', args.repo, '--json',
        'headRefName,body,closingIssuesReferences,commits'])

    closing_refs = same_repo_closing_refs(pull_request, args.repo)
    messages = commit_messages(pull_request)
    branch = pull_request.get('headRefName')
    body = pull_request.get('body')

    # Only the issues we would otherwise complain about are worth an API
    # call each, and there are usually none.
    intent = declared_intent(branch, body, messages)
    already_closed = {n for n in intent - closing_refs
                      if _issue_is_closed(args.repo, n)}

    missing, unexpected, declared = evaluate(
        branch, body, messages, closing_refs, already_closed)

    _report(build_report(declared, closing_refs, missing, unexpected))
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
