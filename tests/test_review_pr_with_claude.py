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
import stat
import subprocess
import tempfile
import textwrap
import unittest

from tests.helpers import REPO_ROOT


SCRIPT = os.path.join(
    REPO_ROOT, 'review-pr-with-claude', 'review-pr-with-claude.sh')

# Answers the calls the script makes before it reaches the reviewer: no
# earlier comment, no earlier review, a pull request, and a one-line
# diff. Everything else -- posting a comment or a review -- succeeds and
# does nothing. Every call is logged, so a run that stops short of the
# reviewer shows which call it stopped after.
FAKE_GH = textwrap.dedent('''\
    #!/bin/bash
    echo "gh $*" >> "${FAKE_GH_LOG}"
    case "$1 $2" in
        'pr view')
            case "$*" in
                *--json\\ comments*|*--json\\ reviews*) exit 0 ;;
            esac
            echo '{"title": "t", "body": "b", "author": {"login": "a"},' \\
                '"baseRefName": "main", "headRefName": "topic"}'
            ;;
        'pr diff')
            printf 'diff --git a/x b/x\\n+one line\\n'
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


if __name__ == '__main__':
    unittest.main()
