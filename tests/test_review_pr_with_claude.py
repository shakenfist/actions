#!/usr/bin/env python3

"""Tests for how review-pr-with-claude.sh launches the reviewer.

The reviewer reads untrusted text: the pull request's diff and its whole
tree. What is pinned here is what it is given to act with, by running
the shipped script against a stub gh and a stub claude and recording
what the stub claude received -- not by searching the script's text,
which a reworded invocation would pass while still handing the model a
shell and a token.
"""

import json
import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest

from tests.helpers import REPO_ROOT


SCRIPT = os.path.join(
    REPO_ROOT, 'review-pr-with-claude', 'review-pr-with-claude.sh')
ACTION = os.path.join(REPO_ROOT, 'review-pr-with-claude', 'action.yml')

# Answers the calls the script makes before it reaches the reviewer: no
# earlier comment, no earlier review, a pull request, and a one-line
# diff. Everything else -- posting a comment or a review -- succeeds and
# does nothing. Every call is logged, so a run that stops short of the
# reviewer shows which call it stopped after.
#
# A test can script the rest through the environment, all unset by
# default:
#
#   FAKE_GH_REVIEWS, FAKE_GH_COMMENTS  a pull request JSON document that
#       answers --json reviews or --json comments. The script's own --jq
#       filter is applied to it with jq, so which authors the filter
#       selects is exercised rather than assumed.
#   FAKE_GH_REVIEWS_FAIL, FAKE_GH_COMMENTS_FAIL  fail that query:
#       'error' writes only to stderr, and 'partial' also leaves text on
#       stdout, as real gh can for an HTTP error.
#   FAKE_GH_DIFF_LINES  the number of lines in the diff.
#   FAKE_GH_POSTED  a file every posted comment or review body is
#       appended to.
FAKE_GH = textwrap.dedent('''\
    #!/bin/bash
    echo "gh $*" >> "${FAKE_GH_LOG}"
    jq_filter=''
    body_file=''
    prev=''
    for arg in "$@"; do
        case "${prev}" in
            --jq) jq_filter="${arg}" ;;
            --body-file) body_file="${arg}" ;;
        esac
        prev="${arg}"
    done
    answer_query() {
        local fail_var="FAKE_GH_${1}_FAIL" doc_var="FAKE_GH_${1}"
        case "${!fail_var:-}" in
            error)
                echo 'error connecting to api.github.com' >&2
                exit 1 ;;
            partial)
                echo '{"message": "Bad credentials"}'
                echo 'HTTP 401: Bad credentials' >&2
                exit 1 ;;
        esac
        if [ -n "${!doc_var:-}" ]; then
            jq -r "${jq_filter}" "${!doc_var}" || exit 98
        fi
        exit 0
    }
    case "$1 $2" in
        'pr view')
            case "$*" in
                *--json\\ reviews*) answer_query REVIEWS ;;
                *--json\\ comments*) answer_query COMMENTS ;;
            esac
            echo '{"title": "t", "body": "b", "author": {"login": "a"},' \\
                '"baseRefName": "main", "headRefName": "topic"}'
            ;;
        'pr diff')
            printf 'diff --git a/x b/x\\n'
            if [ -n "${FAKE_GH_DIFF_LINES:-}" ]; then
                seq 2 "${FAKE_GH_DIFF_LINES}" | sed 's/^/+line /'
            else
                printf '+one line\\n'
            fi
            ;;
        'pr comment'|'pr review')
            if [ -n "${FAKE_GH_POSTED:-}" ] && [ -n "${body_file}" ]; then
                cat "${body_file}" >> "${FAKE_GH_POSTED}"
            fi
            ;;
    esac
    exit 0
    ''')

# Records its arguments, whether either GitHub token reached it, and the
# size of the prompt on stdin, then answers as the CLI does for a run
# that produced nothing.
FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    prompt = sys.stdin.read()
    with open(os.environ['FAKE_CLAUDE_RECORD'], 'w') as f:
        json.dump({
            'argv': sys.argv[1:],
            'tokens': sorted(k for k in ('GH_TOKEN', 'GITHUB_TOKEN')
                             if k in os.environ),
            'prompt_bytes': len(prompt),
        }, f)
    print(json.dumps({'type': 'result', 'subtype': 'success',
                      'result': '', 'num_turns': 1}))
    ''')

# The tools a review needs.
READ_TOOLS = {'Read', 'Grep', 'Glob'}

# Tools that would let the reviewer do more than read the checkout.
UNSAFE_TOOLS = {
    'Bash', 'Edit', 'Write', 'NotebookEdit', 'WebFetch', 'WebSearch'}


def write_executable(path, content):
    with open(path, 'w') as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def flag_values(argv, flag):
    """The arguments following flag, up to the next option."""
    if flag not in argv:
        return None
    values = []
    for arg in argv[argv.index(flag) + 1:]:
        if arg.startswith('-'):
            break
        values.extend(v for v in arg.replace(',', ' ').split() if v)
    return values


class ReviewerLaunchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        bin_dir = os.path.join(cls.tmp.name, 'bin')
        os.mkdir(bin_dir)
        write_executable(os.path.join(bin_dir, 'gh'), FAKE_GH)
        claude = os.path.join(bin_dir, 'fake-claude')
        write_executable(claude, FAKE_CLAUDE)
        record = os.path.join(cls.tmp.name, 'record.json')
        cls.gh_log = os.path.join(cls.tmp.name, 'gh.log')

        env = dict(os.environ)
        env.update({
            'PATH': bin_dir + os.pathsep + env.get('PATH', ''),
            'CLAUDE_BIN': claude,
            'FAKE_CLAUDE_RECORD': record,
            'FAKE_GH_LOG': cls.gh_log,
            'GH_TOKEN': 'gh-token-the-reviewer-must-not-see',
            'GITHUB_TOKEN': 'github-token-the-reviewer-must-not-see',
            'GITHUB_REPOSITORY': 'shakenfist/example',
            'GITHUB_STEP_SUMMARY': os.path.join(cls.tmp.name, 'summary'),
            'INPUT_PR_NUMBER': '7',
            'INPUT_MAX_TURNS': '',
            'INPUT_FORCE': 'false',
        })
        cls.run_result = subprocess.run(
            ['bash', SCRIPT], cwd=cls.tmp.name, env=env,
            capture_output=True, text=True, timeout=60)
        try:
            with open(record) as f:
                cls.record = json.load(f)
        except FileNotFoundError:
            cls.record = None

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        if self.record is None:
            try:
                with open(self.gh_log) as f:
                    gh_calls = f.read()
            except FileNotFoundError:
                gh_calls = '(none)\n'
            self.fail('the script never launched the reviewer.\n'
                      'gh calls:\n' + gh_calls
                      + 'output:\n' + self.run_result.stdout
                      + self.run_result.stderr)
        self.argv = self.record['argv']

    def test_the_prompt_reaches_the_reviewer(self):
        self.assertGreater(self.record['prompt_bytes'], 0)

    def test_no_github_token_reaches_the_reviewer(self):
        self.assertEqual(self.record['tokens'], [])

    def test_permission_checks_are_not_bypassed(self):
        self.assertNotIn('--dangerously-skip-permissions', self.argv)
        self.assertNotIn('bypassPermissions', self.argv)

    def test_only_read_tools_exist(self):
        # An allowlist of the tools loaded at all, so a tool missing
        # from the deny list -- Agent, Skill, or one a later CLI adds --
        # is not there to be approved.
        tools = flag_values(self.argv, '--tools')
        self.assertIsNotNone(tools, self.argv)
        self.assertLessEqual(set(tools), READ_TOOLS)

    def test_only_read_tools_are_allowed(self):
        allowed = flag_values(self.argv, '--allowedTools')
        self.assertIsNotNone(allowed, self.argv)
        self.assertLessEqual(set(allowed), READ_TOOLS)

    def test_shell_network_and_write_tools_are_denied(self):
        denied = flag_values(self.argv, '--disallowedTools')
        self.assertIsNotNone(denied, self.argv)
        self.assertLessEqual(UNSAFE_TOOLS, set(denied))

    def test_file_tools_are_confined_to_the_checkout(self):
        # A bare Read opens any path: the runner's credentials, or
        # /proc/self/environ, quoted into a public review.
        self.assertIn('--restricted', self.argv)

    def test_no_settings_file_can_configure_the_cli(self):
        # A .claude/settings.json in the pull request, or one another
        # job left in the shared runner's home, would run its hooks as
        # shell commands; a .mcp.json would start servers. --restricted
        # ignores every settings file, and no setting source is named
        # to load one back.
        self.assertIn('--restricted', self.argv)
        self.assertIn(flag_values(self.argv, '--setting-sources'),
                      (None, []))
        self.assertIn('--strict-mcp-config', self.argv)


# A gh that answers each outcome's calls. FAKE_GH_REVIEWED makes the bot
# look as if it had already reviewed the pull request, and
# FAKE_GH_DIFF_TOO_LARGE fails the diff fetch the way the API does past
# its line cap. The --jq filters are the script's; this answers with
# what they would select.
OUTCOME_GH = textwrap.dedent('''\
    #!/bin/bash
    case "$1 $2" in
        'pr view')
            case "$*" in
                *--json\\ reviews*)
                    [ -n "${FAKE_GH_REVIEWED:-}" ] && echo 'PRR_1'
                    exit 0 ;;
                *--json\\ comments*) exit 0 ;;
            esac
            echo '{"title": "t", "body": "b", "author": {"login": "a"},' \\
                '"baseRefName": "main", "headRefName": "topic"}'
            ;;
        'pr diff')
            if [ -n "${FAKE_GH_DIFF_TOO_LARGE:-}" ]; then
                echo 'HTTP 406: the diff exceeded the maximum number of lines' >&2
                exit 1
            fi
            printf 'diff --git a/x b/x\\n+one line\\n'
            ;;
    esac
    exit 0
    ''')

# A claude that returns a complete, valid review. FAKE_CLAUDE_SUBTYPE
# replaces the envelope's subtype, which the script reports as an output.
REVIEWING_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    sys.stdin.read()
    review = {'summary': 'Fine.', 'items': [
        {'id': 1, 'title': 'A nit', 'category': 'style', 'action': 'none'}]}
    result = '```json\\n' + json.dumps(review) + '\\n```\\n'
    subtype = os.environ.get('FAKE_CLAUDE_SUBTYPE', 'success')
    print(json.dumps({'type': 'result', 'subtype': subtype,
                      'result': result, 'num_turns': 3,
                      'duration_ms': 1200, 'total_cost_usd': 0.25}))
    ''')


class ReviewerOutputsTest(unittest.TestCase):
    """A green job does not say whether a review was posted; these do.

    Each outcome runs the shipped script and reads back what it wrote to
    GITHUB_OUTPUT, which is what action.yml publishes to the caller.
    """

    def run_script(self, github_output=True, **fake_env):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = os.path.join(tmp.name, 'bin')
        os.mkdir(bin_dir)
        write_executable(os.path.join(bin_dir, 'gh'), OUTCOME_GH)
        claude = os.path.join(bin_dir, 'fake-claude')
        write_executable(claude, REVIEWING_CLAUDE)
        output_file = os.path.join(tmp.name, 'github-output')

        env = dict(os.environ)
        env.pop('GITHUB_OUTPUT', None)
        env.pop('GITHUB_STEP_SUMMARY', None)
        env.update({
            'PATH': bin_dir + os.pathsep + env.get('PATH', ''),
            'CLAUDE_BIN': claude,
            'GITHUB_REPOSITORY': 'shakenfist/example',
            'INPUT_PR_NUMBER': '7',
            'INPUT_MAX_TURNS': '',
            'INPUT_FORCE': 'false',
        })
        if github_output:
            env['GITHUB_OUTPUT'] = output_file
        env.update(fake_env)
        result = subprocess.run(
            ['bash', SCRIPT], cwd=tmp.name, env=env,
            capture_output=True, text=True, timeout=60)
        outputs = {}
        if os.path.exists(output_file):
            with open(output_file) as f:
                for line in f:
                    key, _, value = line.rstrip('\n').partition('=')
                    outputs[key] = value
        return result, outputs

    def test_a_posted_review_says_so(self):
        result, outputs = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs.get('review_posted'), 'true', outputs)
        self.assertNotIn('review_skipped', outputs)
        self.assertEqual(outputs.get('claude_turns'), '3')
        self.assertEqual(outputs.get('claude_subtype'), 'success')

    def test_an_already_reviewed_pull_request_is_green_and_unreviewed(self):
        result, outputs = self.run_script(FAKE_GH_REVIEWED='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs.get('review_posted'), 'false', outputs)
        self.assertEqual(outputs.get('review_skipped'), 'already_reviewed')

    def test_a_diff_too_large_is_green_and_unreviewed(self):
        result, outputs = self.run_script(FAKE_GH_DIFF_TOO_LARGE='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs.get('review_posted'), 'false', outputs)
        self.assertEqual(outputs.get('review_skipped'), 'diff_too_large')

    def test_a_newline_in_a_value_cannot_set_another_output(self):
        result, outputs = self.run_script(
            FAKE_CLAUDE_SUBTYPE='odd\nreview_skipped=injected')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('review_skipped', outputs)
        self.assertEqual(outputs.get('claude_subtype'),
                         'odd review_skipped=injected')

    def test_runs_outside_actions_without_an_output_file(self):
        result, outputs = self.run_script(github_output=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs, {})
        self.assertIn('review_posted=true', result.stdout)

    def test_every_output_the_script_writes_is_published(self):
        # A key written to GITHUB_OUTPUT that action.yml does not map to
        # an output never reaches the caller.
        with open(SCRIPT) as f:
            written = set(re.findall(r'ci_output "([a-z_]+)"', f.read()))
        with open(ACTION) as f:
            published = set(re.findall(
                r'steps\.review\.outputs\.([a-z_]+)', f.read()))
        self.assertTrue(written)
        self.assertEqual(written, published)


# Answers with the response held in FAKE_CLAUDE_RESPONSE, as the CLI
# does for a run that finished.
FAKE_CLAUDE_RESPONDING = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    sys.stdin.read()
    with open(os.environ['FAKE_CLAUDE_RESPONSE']) as f:
        response = f.read()
    print(json.dumps({'type': 'result', 'subtype': 'success',
                      'result': response, 'num_turns': 1}))
    ''')


class ReviewOutcomeTest(unittest.TestCase):
    """Which outcome a review that will not validate ends in.

    A salvaged review that fails schema validation is posted green,
    less the parts that failed and with a warning naming them, or gets
    the green "cut off" explanation when no finding survives. A
    complete review that fails it is the prompt and the schema
    disagreeing, which is red. So which of the two the extractor calls
    a review decides whether a tooling bug is seen or explained away.
    """

    # Valid as far as the salvage predicate looks, but the severity is
    # not one the schema allows, so validation rejects it.
    ITEMS = [
        {'id': 1, 'title': 't', 'category': 'bug', 'action': 'fix',
         'severity': 'urgent'},
        {'id': 2, 'title': 'u', 'category': 'bug', 'action': 'fix'},
    ]

    def run_reviewer(self, response):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = os.path.join(tmp.name, 'bin')
        os.mkdir(bin_dir)
        write_executable(os.path.join(bin_dir, 'gh-inner'), FAKE_GH)
        write_executable(os.path.join(bin_dir, 'gh'), POSTING_GH)
        self.posted_dir = os.path.join(tmp.name, 'posted')
        os.mkdir(self.posted_dir)
        claude = os.path.join(bin_dir, 'fake-claude')
        write_executable(claude, FAKE_CLAUDE_RESPONDING)
        response_path = os.path.join(tmp.name, 'response.txt')
        with open(response_path, 'w') as f:
            f.write(response)
        summary = os.path.join(tmp.name, 'summary')

        env = dict(os.environ)
        env.update({
            'PATH': bin_dir + os.pathsep + env.get('PATH', ''),
            'CLAUDE_BIN': claude,
            'FAKE_CLAUDE_RESPONSE': response_path,
            'FAKE_GH_LOG': os.path.join(tmp.name, 'gh.log'),
            'FAKE_GH_POSTED': self.posted_dir,
            'GITHUB_REPOSITORY': 'shakenfist/example',
            'GITHUB_STEP_SUMMARY': summary,
            'INPUT_PR_NUMBER': '7',
            'INPUT_MAX_TURNS': '',
            'INPUT_FORCE': 'false',
        })
        result = subprocess.run(
            ['bash', SCRIPT], cwd=tmp.name, env=env,
            capture_output=True, text=True, timeout=60)
        try:
            with open(summary) as f:
                summary_text = f.read()
        except FileNotFoundError:
            summary_text = ''
        return result, summary_text

    def test_a_salvaged_review_with_a_bad_optional_field_is_posted_green(self):
        text = json.dumps({'summary': 's', 'items': self.ITEMS + [
            {'id': 3, 'title': 'v', 'category': 'bug', 'action': 'fix'}]})
        result, summary = self.run_reviewer(
            '```json\n' + text[:text.index('{"id": 3')])
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertIn('Extraction: status=salvaged', result.stdout)
        review = self.posted_review()
        self.assertEqual([item['id'] for item in review['items']], [1, 2])
        self.assertNotIn('severity', review['items'][0])
        self.assertIn('Incomplete review.', self.posted['review'])
        self.assertIn('dropped rather than posted: review.items[0].severity.',
                      review['caveat'])
        self.assertIn('Partial review posted with parts dropped', summary)

    def test_an_invalid_whole_review_with_prose_after_it_is_red(self):
        # Prose after the object used to make this a salvaged review,
        # which sent a schema mismatch to the green explanation.
        result, summary = self.run_reviewer(
            json.dumps({'summary': 's', 'items': self.ITEMS})
            + '\n\nHope that helps.')
        self.assertEqual(result.returncode, 1,
                         result.stdout + result.stderr)
        self.assertIn('Extraction: status=ok', result.stdout)
        self.assertIn('failed schema validation', summary)

    def test_a_whole_review_with_an_unknown_action_is_red(self):
        # The salvage predicate would reject this item, and used to
        # walk back over it, posting the rest as partial and green.
        items = [dict(self.ITEMS[1], id=1),
                 dict(self.ITEMS[1], id=2, action='escalate')]
        result, summary = self.run_reviewer(
            json.dumps({'summary': 's', 'items': items})
            + '\n\nHope that helps.')
        self.assertEqual(result.returncode, 1,
                         result.stdout + result.stderr)
        self.assertIn('Extraction: status=ok', result.stdout)
        self.assertIn('failed schema validation', summary)

    @property
    def posted(self):
        """{'review' or 'comment': body} for what the run posted."""
        bodies = {}
        for name in os.listdir(self.posted_dir):
            with open(os.path.join(self.posted_dir, name)) as f:
                bodies[name] = f.read()
        return bodies

    def posted_review(self):
        """The review JSON embedded in the posted review."""
        self.assertIn('review', self.posted, self.posted)
        embedded = self.posted['review'].split('```json\n', 1)[1]
        return json.loads(embedded.split('\n```', 1)[0])

    def test_a_salvaged_review_with_a_bad_positive_feedback_entry_is_posted_green(self):
        text = json.dumps({
            'summary': 's', 'items': [self.ITEMS[1]],
            'positive_feedback': [{'title': 'good', 'description': 'why'},
                                  {'title': 3, 'description': 'how'}],
            'test_coverage': {'adequate': True, 'missing': []},
        })
        result, summary = self.run_reviewer(
            '```json\n' + text[:text.index('"missing"')])
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertIn('Extraction: status=salvaged', result.stdout)
        review = self.posted_review()
        self.assertEqual(review['positive_feedback'],
                         [{'title': 'good', 'description': 'why'}])
        self.assertEqual(len(review['items']), 1)
        self.assertIn('review.positive_feedback[1].', review['caveat'])
        self.assertIn('Partial review posted with parts dropped', summary)

    def test_a_salvaged_review_with_nothing_left_is_explained_green(self):
        # The summary is required, so a wrong one leaves nothing to post.
        text = json.dumps({'summary': 3, 'items': self.ITEMS})
        result, summary = self.run_reviewer(
            '```json\n' + text[:text.index('{"id": 2')])
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertIn('Extraction: status=salvaged', result.stdout)
        self.assertIn('cut off before it said anything', summary)
        self.assertNotIn('review', self.posted)

    def test_an_invalid_whole_review_is_red_and_not_pruned(self):
        # The same bad optional field a salvaged review is posted
        # without, in a fence that closed.
        result, summary = self.run_reviewer(
            '```json\n' + json.dumps({'summary': 's', 'items': self.ITEMS})
            + '\n```\n')
        self.assertEqual(result.returncode, 1,
                         result.stdout + result.stderr)
        self.assertIn('Extraction: status=ok', result.stdout)
        self.assertIn('failed schema validation', summary)
        self.assertNotIn('review', self.posted)
        self.assertNotIn('Dropped:', result.stdout)


# Keeps a copy of each body the script posts, under FAKE_GH_POSTED, as
# review or comment, then answers as FAKE_GH does, installed beside it
# as gh-inner. The script deletes its own copy when it exits.
POSTING_GH = textwrap.dedent('''\
    #!/bin/bash
    kind=''
    case "$1 $2" in
        'pr review') kind=review ;;
        'pr comment') kind=comment ;;
    esac
    if [ -n "${kind}" ]; then
        prev=''
        for arg in "$@"; do
            if [ "${prev}" = '--body-file' ]; then
                cp "${arg}" "${FAKE_GH_POSTED}/${kind}"
            fi
            prev="${arg}"
        done
    fi
    exec "$(dirname "$0")/gh-inner" "$@"
    ''')


# Records its arguments, then ends the run the way FAKE_CLAUDE_OUTCOME
# names: 'review' (the default) with a complete, valid review,
# 'out_of_turns' with no result and the budget spent, 'out_of_turns_prose'
# with the budget spent and prose in place of a review, and 'sdk_error'
# with no result and the SDK having failed.
GATE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    sys.stdin.read()
    with open(os.environ['FAKE_CLAUDE_RECORD'], 'w') as f:
        json.dump(sys.argv[1:], f)
    outcome = os.environ.get('FAKE_CLAUDE_OUTCOME', 'review')
    if outcome == 'review':
        review = {'summary': 'Fine.', 'items': [
            {'id': 1, 'title': 'A nit', 'category': 'style',
             'action': 'none'}]}
        envelope = {'subtype': 'success',
                    'result': '```json\\n' + json.dumps(review) + '\\n```\\n'}
    elif outcome == 'out_of_turns':
        envelope = {'subtype': 'error_max_turns'}
    elif outcome == 'out_of_turns_prose':
        envelope = {'subtype': 'error_max_turns',
                    'result': 'Still reading the tree, no review yet.'}
    else:
        envelope = {'subtype': 'error_during_execution'}
    envelope.update({'type': 'result', 'num_turns': 1})
    print(json.dumps(envelope))
    ''')

# The marker review_unavailable() puts on a turn budget explanation.
OUT_OF_TURNS_MARKER = '<!-- sf-reviewer-unavailable: turn_budget_exhausted -->'


def reviews_doc(*authors):
    """A --json reviews answer holding one review by each author."""
    return {'reviews': [
        {'id': 'PRR_%d' % i, 'author': {'login': author},
         'state': 'COMMENTED', 'body': 'b'}
        for i, author in enumerate(authors)]}


def comments_doc(*comments):
    """A --json comments answer from (author, body) pairs."""
    return {'comments': [
        {'id': 'IC_%d' % i, 'author': {'login': author}, 'body': body}
        for i, (author, body) in enumerate(comments)]}


class GateRun:
    """What one run of the script did, as FAKE_GH and GATE_CLAUDE saw it."""

    def __init__(self, result, tmp):
        self.returncode = result.returncode
        self.log = result.stdout + result.stderr

        def read(name):
            try:
                with open(os.path.join(tmp, name)) as f:
                    return f.read()
            except FileNotFoundError:
                return ''

        self.gh_calls = read('gh.log').splitlines()
        self.posted = read('posted')
        self.summary = read('summary')
        record = read('record.json')
        self.reviewer_argv = json.loads(record) if record else None
        self.outputs = {}
        for line in read('github-output').splitlines():
            key, _, value = line.partition('=')
            self.outputs[key] = value

    @property
    def posts(self):
        """Every call that wrote to the pull request."""
        return [c for c in self.gh_calls
                if c.startswith(('gh pr comment', 'gh pr review'))]

    @property
    def max_turns(self):
        if self.reviewer_argv is None:
            return None
        return self.reviewer_argv[self.reviewer_argv.index('--max-turns') + 1]


class GateHarness:
    """Runs the shipped script against FAKE_GH and GATE_CLAUDE."""

    def run_script(self, reviews=None, comments=None, **env_overrides):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = os.path.join(tmp.name, 'bin')
        os.mkdir(bin_dir)
        write_executable(os.path.join(bin_dir, 'gh'), FAKE_GH)
        claude = os.path.join(bin_dir, 'fake-claude')
        write_executable(claude, GATE_CLAUDE)

        # Nothing from the environment running the tests may script the
        # stubs or receive the script's outputs.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('FAKE_', 'GITHUB_', 'INPUT_'))}
        env.update({
            'PATH': bin_dir + os.pathsep + env.get('PATH', ''),
            'CLAUDE_BIN': claude,
            'FAKE_CLAUDE_RECORD': os.path.join(tmp.name, 'record.json'),
            'FAKE_GH_LOG': os.path.join(tmp.name, 'gh.log'),
            'FAKE_GH_POSTED': os.path.join(tmp.name, 'posted'),
            'GITHUB_REPOSITORY': 'shakenfist/example',
            'GITHUB_OUTPUT': os.path.join(tmp.name, 'github-output'),
            'GITHUB_STEP_SUMMARY': os.path.join(tmp.name, 'summary'),
            'INPUT_PR_NUMBER': '7',
            'INPUT_MAX_TURNS': '',
            'INPUT_FORCE': 'false',
        })
        for name, doc in (('REVIEWS', reviews), ('COMMENTS', comments)):
            if doc is not None:
                path = os.path.join(tmp.name, name.lower() + '.json')
                with open(path, 'w') as f:
                    json.dump(doc, f)
                env['FAKE_GH_' + name] = path
        env.update(env_overrides)
        result = subprocess.run(
            ['bash', SCRIPT], cwd=tmp.name, env=env,
            capture_output=True, text=True, timeout=60)
        return GateRun(result, tmp.name)

    def assertReviewed(self, run):
        self.assertEqual(run.returncode, 0, run.log)
        self.assertIsNotNone(run.reviewer_argv, run.log)
        self.assertEqual(run.outputs.get('review_posted'), 'true', run.log)
        self.assertEqual([c.split()[:3] for c in run.posts],
                         [['gh', 'pr', 'review']], run.log)

    def assertNotReviewed(self, run):
        self.assertIsNone(run.reviewer_argv, run.log)
        self.assertEqual(run.posts, [], run.log)
        self.assertEqual(run.outputs.get('review_posted'), 'false', run.log)


class EarlierReviewGateTest(GateHarness, unittest.TestCase):
    """Whether the script reviews at all, given the reviews already there.

    The gate is what stops every push to a pull request buying another
    review. Its failure mode that matters is a query that fails being
    read as "no earlier review", which reviews and posts again.
    """

    def test_no_earlier_review_is_reviewed(self):
        self.assertReviewed(self.run_script(reviews=reviews_doc()))

    def test_a_review_by_a_person_does_not_count(self):
        self.assertReviewed(self.run_script(
            reviews=reviews_doc('alice', 'github-actions[bot]')))

    def test_a_review_by_either_bot_skips_green(self):
        for authors in (('github-actions',), ('shakenfist-bot',),
                        ('alice', 'shakenfist-bot')):
            with self.subTest(authors=authors):
                run = self.run_script(reviews=reviews_doc(*authors))
                self.assertEqual(run.returncode, 0, run.log)
                self.assertNotReviewed(run)
                self.assertEqual(run.outputs.get('review_skipped'),
                                 'already_reviewed')

    def test_force_reviews_again(self):
        run = self.run_script(reviews=reviews_doc('shakenfist-bot'),
                              INPUT_FORCE='true')
        self.assertReviewed(run)
        self.assertIn('force specified', run.log)

    def test_only_an_exact_true_forces(self):
        for value in ('True', 'yes', '1', ''):
            with self.subTest(force=value):
                run = self.run_script(reviews=reviews_doc('shakenfist-bot'),
                                      INPUT_FORCE=value)
                self.assertNotReviewed(run)
                self.assertEqual(run.outputs.get('review_skipped'),
                                 'already_reviewed')

    def test_a_failed_query_skips_red_and_posts_nothing(self):
        # 'partial' leaves an error body on stdout, which the gate must
        # not read as the id of an earlier review either: that would be
        # a green "already reviewed" for a check that never happened.
        for failure in ('error', 'partial'):
            with self.subTest(failure=failure):
                run = self.run_script(FAKE_GH_REVIEWS_FAIL=failure)
                self.assertEqual(run.returncode, 1, run.log)
                self.assertNotReviewed(run)
                self.assertNotIn('review_skipped', run.outputs)
                self.assertIn('could not check for an earlier review',
                              run.summary)

    def test_a_failed_query_under_force_still_reviews(self):
        for failure in ('error', 'partial'):
            with self.subTest(failure=failure):
                run = self.run_script(FAKE_GH_REVIEWS_FAIL=failure,
                                      INPUT_FORCE='true')
                self.assertReviewed(run)
                self.assertIn('could not check for an earlier review', run.log)


class TurnBudgetTest(GateHarness, unittest.TestCase):
    """The turn budget the reviewer is given, and running out of it."""

    def test_the_budget_scales_with_the_diff(self):
        # Fifty turns, ten more per whole 500 lines, capped at 150.
        for max_turns, lines, expected in (
                ('', 2, '50'), ('auto', 2, '50'), ('', 499, '50'),
                ('', 500, '60'), ('', 1200, '70'), ('auto', 1200, '70'),
                ('', 5000, '150'), ('', 5600, '150')):
            with self.subTest(max_turns=max_turns, lines=lines):
                run = self.run_script(INPUT_MAX_TURNS=max_turns,
                                      FAKE_GH_DIFF_LINES=str(lines))
                self.assertEqual(run.max_turns, expected, run.log)
                self.assertIn('scaled from diff size', run.log)

    def test_a_pinned_budget_is_used_as_given(self):
        for max_turns in ('7', '9999'):
            with self.subTest(max_turns=max_turns):
                run = self.run_script(INPUT_MAX_TURNS=max_turns,
                                      FAKE_GH_DIFF_LINES='1200')
                self.assertEqual(run.max_turns, max_turns, run.log)
                self.assertIn('set by the caller', run.log)

    def test_an_unusable_budget_scales_instead(self):
        for max_turns in ('0', '00', 'abc', '-5', '7 8', '1\n2', '12345',
                          '99999999999999999999'):
            with self.subTest(max_turns=max_turns):
                run = self.run_script(INPUT_MAX_TURNS=max_turns)
                self.assertEqual(run.max_turns, '50', run.log)
                self.assertIn('is not a usable turn budget', run.log)

    def test_running_out_of_turns_is_explained_green(self):
        run = self.run_script(INPUT_MAX_TURNS='7',
                              FAKE_CLAUDE_OUTCOME='out_of_turns')
        self.assertEqual(run.returncode, 0, run.log)
        self.assertEqual(run.outputs.get('review_skipped'),
                         'turn_budget_exhausted')
        self.assertEqual(run.outputs.get('review_posted'), 'false')
        self.assertEqual(len(run.posts), 1, run.log)
        self.assertIn('budget of 7 turns on this\n2 line diff', run.posted)
        self.assertIn(OUT_OF_TURNS_MARKER, run.posted)
        self.assertIn('ran out of turns', run.summary)

    def test_running_out_of_turns_with_prose_is_explained_green(self):
        # Prose in place of a review is the budget running out too, not
        # the reviewer breaking, so it is not posted as unparsed output.
        run = self.run_script(INPUT_MAX_TURNS='7',
                              FAKE_CLAUDE_OUTCOME='out_of_turns_prose')
        self.assertEqual(run.returncode, 0, run.log)
        self.assertEqual(run.outputs.get('review_skipped'),
                         'turn_budget_exhausted')
        self.assertEqual(len(run.posts), 1, run.log)
        self.assertIn(OUT_OF_TURNS_MARKER, run.posted)
        self.assertNotIn('Still reading the tree', run.posted)

    def test_the_explanation_names_a_scaled_budget(self):
        run = self.run_script(FAKE_GH_DIFF_LINES='1200',
                              FAKE_CLAUDE_OUTCOME='out_of_turns')
        self.assertEqual(run.returncode, 0, run.log)
        self.assertIn('budget of 70 turns on this\n1200 line diff',
                      run.posted)

    def test_no_result_with_turns_to_spare_is_red(self):
        run = self.run_script(FAKE_CLAUDE_OUTCOME='sdk_error')
        self.assertEqual(run.returncode, 1, run.log)
        self.assertEqual(run.posts, [], run.log)
        self.assertNotIn('review_skipped', run.outputs)
        self.assertIn('produced no result', run.summary)


class ExplanationDedupeTest(GateHarness, unittest.TestCase):
    """An explanation of a skipped review is posted once per pull request.

    Running out of turns is the way in: it reaches review_unavailable(),
    which every green "no review" path shares.
    """

    def run_out_of_turns(self, **kwargs):
        run = self.run_script(FAKE_CLAUDE_OUTCOME='out_of_turns', **kwargs)
        self.assertEqual(run.returncode, 0, run.log)
        self.assertEqual(run.outputs.get('review_skipped'),
                         'turn_budget_exhausted')
        self.assertIn('ran out of turns', run.summary)
        return run

    def test_the_first_explanation_is_posted(self):
        run = self.run_out_of_turns(comments=comments_doc(
            ('alice', 'Looks good to me.')))
        self.assertEqual(len(run.posts), 1, run.log)
        self.assertTrue(run.posts[0].startswith('gh pr comment'))
        self.assertNotIn('was not posted to the pull request', run.summary)

    def test_an_earlier_copy_from_either_bot_is_not_repeated(self):
        for author in ('github-actions', 'shakenfist-bot'):
            with self.subTest(author=author):
                run = self.run_out_of_turns(comments=comments_doc(
                    ('alice', 'Hmm.'),
                    (author, 'Earlier words.\n\n' + OUT_OF_TURNS_MARKER
                     + '\n')))
                self.assertEqual(run.posts, [], run.log)
                self.assertIn('already carries this explanation', run.log)
                self.assertNotIn('was not posted to the pull request',
                                 run.summary)

    def test_a_person_quoting_the_marker_does_not_count(self):
        run = self.run_out_of_turns(comments=comments_doc(
            ('alice', OUT_OF_TURNS_MARKER)))
        self.assertEqual(len(run.posts), 1, run.log)

    def test_an_explanation_for_another_reason_does_not_count(self):
        run = self.run_out_of_turns(comments=comments_doc(
            ('shakenfist-bot',
             '<!-- sf-reviewer-unavailable: diff_too_large -->')))
        self.assertEqual(len(run.posts), 1, run.log)

    def test_a_failed_query_posts_nothing(self):
        # Not knowing whether the explanation is there already is not
        # knowing that it is absent. The job summary still carries it.
        for failure in ('error', 'partial'):
            with self.subTest(failure=failure):
                run = self.run_out_of_turns(FAKE_GH_COMMENTS_FAIL=failure)
                self.assertEqual(run.posts, [], run.log)
                self.assertIn('could not read the PR', run.log)
                self.assertIn('was not posted to the pull request',
                              run.summary)


if __name__ == '__main__':
    unittest.main()
