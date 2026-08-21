#!/usr/bin/env python3

"""Tests that workflows only name things which exist.

A workflow that dispatches a missing file, or reads a tool from a
directory its checkout never fetched, fails at the moment somebody is
relying on it and not before. That is not hypothetical here: the shared
template these bot workflows came from dispatches
`functional-tests.yml`, which this repository does not have, and the
whole reason they were deployed at all is that a missing
`pr-re-review.yml` let two pull requests merge with their review fixes
unreviewed. Nothing else in CI looks at these files beyond their YAML
syntax.
"""

import os
import re
import subprocess
import tempfile
import unittest

import yaml

from tests.helpers import REPO_ROOT


WORKFLOW_DIR = os.path.join(REPO_ROOT, '.github', 'workflows')


def workflows():
    for name in sorted(os.listdir(WORKFLOW_DIR)):
        if name.endswith('.yml') or name.endswith('.yaml'):
            path = os.path.join(WORKFLOW_DIR, name)
            with open(path) as f:
                yield name, f.read()


class DispatchTargetTest(unittest.TestCase):
    def test_every_dispatched_workflow_exists(self):
        # `gh workflow run X` on a name GitHub does not know fails at
        # run time with "could not find any workflows named X", which
        # the caller reports as a dispatch failure rather than a bug.
        found = False
        for name, text in workflows():
            for target in re.findall(r'gh workflow run\s+(\S+)', text):
                found = True
                with self.subTest(workflow=name, target=target):
                    self.assertTrue(
                        os.path.exists(os.path.join(WORKFLOW_DIR, target)),
                        '%s dispatches %s, which does not exist in '
                        '.github/workflows/' % (name, target))
        self.assertTrue(found, 'no gh workflow run calls found to check')


class LocalReusableWorkflowTest(unittest.TestCase):
    def test_every_relative_uses_target_exists(self):
        # Relative `uses:` is how ci.yml reviews its own reviewer and
        # how canary.yml calls the smoke lane. A typo here is a workflow
        # that never starts.
        found = False
        for name, text in workflows():
            for target in re.findall(r'uses:\s+(\./\S+)', text):
                found = True
                with self.subTest(workflow=name, target=target):
                    self.assertTrue(
                        os.path.exists(os.path.join(REPO_ROOT, target)),
                        '%s uses %s, which does not exist' % (name, target))
        self.assertTrue(found, 'no relative uses: targets found to check')


class AddressCommentsCheckoutTest(unittest.TestCase):
    """The trusted-tools checkout must fetch what the step then reads.

    pr-address-comments.yml deliberately runs the base branch's copy of
    the tooling rather than the pull request's, so the sparse checkout
    list and the directories TOOLS_DIR and SCRIPT_DIR point into have to
    agree. They are written far apart in the file and neither actionlint
    nor a YAML parse would notice them drifting.
    """

    WORKFLOW = os.path.join(WORKFLOW_DIR, 'pr-address-comments.yml')

    def setUp(self):
        with open(self.WORKFLOW) as f:
            self.text = f.read()
        self.parsed = yaml.safe_load(self.text)

    def sparse_paths(self):
        for job in self.parsed['jobs'].values():
            for step in job.get('steps', []):
                sparse = (step.get('with') or {}).get('sparse-checkout')
                if sparse:
                    return sparse.split()
        self.fail('no sparse-checkout found in %s' % self.WORKFLOW)

    def test_the_directories_read_from_the_trusted_checkout_are_fetched(self):
        sparse = self.sparse_paths()
        # Each of these is "${{ github.workspace }}/trusted-tools/<dir>".
        used = re.findall(
            r'(?:TOOLS_DIR|SCRIPT_DIR):.*?/trusted-tools/(\S+)', self.text)
        self.assertTrue(used, 'no trusted-tools directories referenced')
        for directory in used:
            with self.subTest(directory=directory):
                self.assertIn(
                    directory, sparse,
                    '%s is read from the trusted checkout but is not in its '
                    'sparse-checkout list, so the directory will be empty'
                    % directory)

    def test_the_sparse_checkout_directories_exist_in_the_repository(self):
        for directory in self.sparse_paths():
            with self.subTest(directory=directory):
                self.assertTrue(
                    os.path.isdir(os.path.join(REPO_ROOT, directory)),
                    'sparse-checkout names %s, which is not a directory in '
                    'this repository' % directory)

    def test_render_review_sits_beside_its_schema_in_the_tools_dir(self):
        # render-review.py resolves SCHEMA_PATH as
        # Path(__file__).parent / 'review-schema.json'. Point TOOLS_DIR at
        # a directory holding the script but not the schema and
        # load_schema() returns None, at which point validate_review()
        # returns success without checking anything -- every review
        # validates, including ones the schema would reject. (Its
        # structural fallback is a separate branch, reached only when
        # jsonschema is not importable at all.) That is the failure this
        # repository avoids by not copying the script into tools/.
        tools_dir = re.search(
            r'TOOLS_DIR:.*?/trusted-tools/(\S+)', self.text).group(1)
        for filename in ('render-review.py', 'review-schema.json'):
            with self.subTest(filename=filename):
                self.assertTrue(
                    os.path.exists(
                        os.path.join(REPO_ROOT, tools_dir, filename)),
                    'TOOLS_DIR points at %s, which has no %s'
                    % (tools_dir, filename))


class TriggerPhraseTest(unittest.TestCase):
    """The phrase a workflow matches must be the one it then acts on.

    Each bot workflow names its phrase twice: once in the job's
    `contains()` condition and once as pr-bot-trigger's `trigger-phrase`
    input. A mismatch is a silent no-op -- the workflow starts, the
    action reports triggered=false, the downstream job is skipped on an
    empty authorized output, and nothing is posted or reacted to, so the
    requester sees no response and no error.
    """

    def bot_workflows(self):
        found = {}
        for name, text in workflows():
            phrase = re.search(
                r"trigger-phrase:\s*'([^']+)'", text)
            if phrase:
                found[name] = (phrase.group(1), text)
        self.assertTrue(found, 'no workflows use pr-bot-trigger')
        return found

    def test_the_matched_phrase_is_the_phrase_passed_to_the_action(self):
        for name, (phrase, text) in self.bot_workflows().items():
            with self.subTest(workflow=name):
                self.assertIn(
                    "contains(github.event.comment.body, "
                    "'@shakenfist-bot %s')" % phrase, text,
                    '%s passes trigger-phrase %r to pr-bot-trigger but '
                    'does not gate on that same phrase, so it fires and '
                    'then does nothing' % (name, phrase))

    def test_no_phrase_is_a_prefix_of_another(self):
        # contains() is a substring match, so a phrase that is a prefix
        # of another would fire both lanes from one comment -- and two
        # of the three push commits.
        phrases = [p for p, _ in self.bot_workflows().values()]
        for phrase in phrases:
            for other in phrases:
                if phrase is other:
                    continue
                with self.subTest(phrase=phrase, other=other):
                    self.assertNotIn(phrase, other)

    def test_the_documented_phrases_match_the_workflows(self):
        # AGENTS.md points readers at the docs/ci.md table as the list of
        # phrases not to write in a comment. A phrase missing from it is
        # a trap that reads as safe.
        with open(os.path.join(REPO_ROOT, 'docs', 'ci.md')) as f:
            docs = f.read()
        for phrase in [p for p, _ in self.bot_workflows().values()]:
            with self.subTest(phrase=phrase):
                self.assertIn(
                    '@shakenfist-bot %s' % phrase, docs,
                    '@shakenfist-bot %s fires a workflow but is not in '
                    'docs/ci.md, which AGENTS.md cites as the list of '
                    'phrases to avoid writing in a comment' % phrase)


class ForkGuardTest(unittest.TestCase):
    """Bot commands must not act on a fork pull request's head branch.

    pr-bot-trigger's pr-ref output is .head.ref, the branch name in the
    *head* repository. Callers hand it to actions/checkout and to
    `git push origin HEAD:refs/heads/<ref>` against this repository. A
    fork pull request opened from the fork's default branch makes that
    name `main`, so the checkout succeeds against this repository's main
    and the push lands bot commits on the branch the whole fleet pins.

    The guard lives in the action so it cannot be dropped by a caller,
    and so the fix reaches every repository consuming it at @main.
    """

    ACTION = os.path.join(REPO_ROOT, 'pr-bot-trigger', 'action.yml')

    def setUp(self):
        with open(self.ACTION) as f:
            self.text = f.read()
        self.parsed = yaml.safe_load(self.text)

    def steps_by_id(self):
        return {s.get('id'): s for s in self.parsed['runs']['steps']}

    def test_the_action_compares_the_head_repository(self):
        self.assertIn('.head.repo.full_name', self.text)

    def test_authorized_requires_both_permission_and_same_repo(self):
        # Callers gate only on `authorized`, so the fork check has to be
        # folded into it -- otherwise every caller needs its own edit and
        # the guard is lost the moment one is forgotten.
        value = self.parsed['outputs']['authorized']['value']
        gate_id = re.search(r'steps\.(\w+)\.outputs\.authorized', value)
        self.assertIsNotNone(gate_id, 'authorized output is not a step output')
        gate = self.steps_by_id()[gate_id.group(1)]
        self.assertIn('PERMITTED', gate['run'])
        self.assertIn('SAME_REPO', gate['run'])

    def test_the_gate_only_authorizes_permitted_and_same_repo(self):
        """Run the shipped gate script over the whole truth table."""
        gate = self.steps_by_id()['gate']
        expected = {
            ('true', 'true'): 'true',
            ('true', 'false'): 'false',
            ('false', 'true'): 'false',
            ('false', 'false'): 'false',
            # An empty value is what a skipped step yields.
            ('', ''): 'false',
            ('true', ''): 'false',
        }
        for (permitted, same_repo), want in expected.items():
            with self.subTest(permitted=permitted, same_repo=same_repo):
                with tempfile.TemporaryDirectory() as tmp:
                    output = os.path.join(tmp, 'gh-output')
                    open(output, 'w').close()
                    env = dict(os.environ,
                               PERMITTED=permitted, SAME_REPO=same_repo,
                               GITHUB_OUTPUT=output)
                    result = subprocess.run(
                        ['bash', '-c', gate['run']], env=env, check=False,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    with open(output) as f:
                        written = f.read()
                self.assertIn(
                    'authorized=%s' % want, written,
                    'PERMITTED=%r SAME_REPO=%r should authorize %s'
                    % (permitted, same_repo, want))

    def test_a_fork_gets_its_own_message_not_a_permissions_one(self):
        # Telling a maintainer they lack write access when the real
        # reason is that the pull request is from a fork sends them to
        # the wrong place entirely.
        names = [s.get('name', '') for s in self.parsed['runs']['steps']]
        self.assertIn('Post fork-not-supported message', names)


class ToolScriptReferenceTest(unittest.TestCase):
    """A `run:` step naming a script under tools/ must find it there.

    This is the same class of cross-file reference as the dispatch
    targets above, and it fails the same way: renaming or deleting a
    script leaves a workflow that only breaks the next time somebody
    depends on it. prune-reviews.yml invoking tools/ci-prune-reviews.sh
    directly -- not through an interpreter -- also means the executable
    bit is load bearing, and check-executables-have-shebangs tests only
    the converse.

    Only bare `tools/...` references are checked. Paths reached through
    another checkout, like ${GITHUB_WORKSPACE}/shakenfist/tools/..., are
    another repository's files and are not ours to assert about.
    """

    # Not preceded by a word character, dot, slash or hyphen, so
    # trusted-tools/ and .../shakenfist/tools/ do not match. The name
    # must start with a word character, so prose ending in "tools/."
    # does not either.
    REFERENCE = re.compile(r'(?<![\w./-])tools/(\w[\w.-]*)')

    def run_blocks(self):
        for name, text in workflows():
            parsed = yaml.safe_load(text)
            for job_name, job in parsed.get('jobs', {}).items():
                for step in job.get('steps') or []:
                    if not step.get('run'):
                        continue
                    # Shell comments inside a run: block are prose, and
                    # prose mentions paths it does not execute.
                    lines = [line for line in step['run'].splitlines()
                             if not line.lstrip().startswith('#')]
                    yield name, job_name, '\n'.join(lines)

    def test_every_referenced_tools_script_exists_and_is_executable(self):
        found = False
        for name, job_name, block in self.run_blocks():
            for script in set(s.rstrip('.,')
                              for s in self.REFERENCE.findall(block)):
                found = True
                path = os.path.join(REPO_ROOT, 'tools', script)
                with self.subTest(workflow=name, job=job_name, script=script):
                    self.assertTrue(
                        os.path.isfile(path),
                        '%s (job %s) runs tools/%s, which does not exist'
                        % (name, job_name, script))
                    self.assertTrue(
                        os.access(path, os.X_OK),
                        '%s (job %s) runs tools/%s directly, but it is not '
                        'executable' % (name, job_name, script))
        self.assertTrue(found, 'no tools/ references found to check')


class ReviewTrackingExclusionTest(unittest.TestCase):
    """The review-tracking exclusions have to agree across three files.

    Recording a review writes generated files under .vscode/. Those are
    named in ci.yml's path filter so a review-only pull request does not
    book four ephemeral VMs, in canary.yml's paths-ignore so a merged
    stamp does not book a smoke cluster, and in two pre-commit hook
    excludes because the generator emits them without a trailing
    newline. The lists are written in three different syntaxes in three
    different files, and nothing else notices one of them going stale --
    which is how canary.yml came to be missing them in the first place.
    """

    def ci_vscode_exclusions(self):
        with open(os.path.join(WORKFLOW_DIR, 'ci.yml')) as f:
            parsed = yaml.safe_load(f)
        for step in parsed['jobs']['check_paths']['steps']:
            filters = (step.get('with') or {}).get('filters')
            if filters:
                patterns = yaml.safe_load(filters)['code']
                found = [p[1:] for p in patterns
                         if p.startswith('!.vscode/')]
                self.assertTrue(
                    found, "ci.yml's filter excludes no .vscode/ paths")
                return found
        self.fail('no paths-filter step found in ci.yml')

    def test_canary_ignores_what_the_ci_filter_excludes(self):
        with open(os.path.join(WORKFLOW_DIR, 'canary.yml')) as f:
            canary = yaml.safe_load(f)
        # 'on' is parsed as the boolean True by YAML 1.1.
        ignored = canary[True]['push']['paths-ignore']
        for pattern in self.ci_vscode_exclusions():
            with self.subTest(pattern=pattern):
                self.assertIn(
                    pattern, ignored,
                    "ci.yml's path filter excludes %s but canary.yml's "
                    'paths-ignore does not, so a merged review stamp books '
                    'a smoke cluster' % pattern)

    def test_the_generated_review_files_skip_the_rewriting_hooks(self):
        # trailing-whitespace and end-of-file-fixer rewrite the weAudit
        # file and its sidecar on every run and then fail, because the
        # generator emits them without a trailing newline and committing
        # one only means the next regen drops it again. The exclude is
        # scoped to those two hooks rather than set at the top level, so
        # gitleaks and check-json still read the review notes -- prose is
        # where a secret lands.
        with open(os.path.join(REPO_ROOT, '.pre-commit-config.yaml')) as f:
            config = yaml.safe_load(f)
        excludes = {}
        for repo in config['repos']:
            for hook in repo['hooks']:
                if hook.get('exclude'):
                    excludes[hook['id']] = hook['exclude']

        generated = ['.vscode/example.weaudit',
                     '.vscode/example.weaudit-shas.json']
        for hook_id in ('trailing-whitespace', 'end-of-file-fixer'):
            with self.subTest(hook=hook_id):
                self.assertIn(
                    hook_id, excludes,
                    '%s has no exclude, so it will rewrite the generated '
                    'review files and then fail on them' % hook_id)
                pattern = re.compile(excludes[hook_id])
                for path in generated:
                    self.assertIsNotNone(
                        pattern.search(path),
                        "%s's exclude %r does not cover %s"
                        % (hook_id, excludes[hook_id], path))

    def test_the_exclude_does_not_reach_beyond_the_generated_files(self):
        # review-scope.toml is hand edited and should keep getting the
        # hygiene fixes; only the generated pair is exempt.
        with open(os.path.join(REPO_ROOT, '.pre-commit-config.yaml')) as f:
            config = yaml.safe_load(f)
        for repo in config['repos']:
            for hook in repo['hooks']:
                if hook['id'] not in ('trailing-whitespace',
                                      'end-of-file-fixer'):
                    continue
                if not hook.get('exclude'):
                    # A missing exclude is the other test's failure.
                    continue
                with self.subTest(hook=hook['id']):
                    self.assertIsNone(
                        re.compile(hook['exclude']).search(
                            '.vscode/review-scope.toml'),
                        "%s's exclude also covers review-scope.toml, which "
                        'is hand edited' % hook['id'])


class WorkflowPermissionsTest(unittest.TestCase):
    def test_every_workflow_declares_top_level_permissions(self):
        # AGENTS.md lists this as a hard convention: without it a
        # workflow inherits the default GITHUB_TOKEN scope.
        for name, text in workflows():
            with self.subTest(workflow=name):
                parsed = yaml.safe_load(text)
                self.assertIn(
                    'permissions', parsed,
                    '%s has no top-level permissions block' % name)


if __name__ == '__main__':
    unittest.main()
