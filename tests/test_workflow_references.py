#!/usr/bin/env python3

"""Tests that workflows only name things which exist.

A workflow that dispatches a missing file, or invokes a script no longer
in the tree, fails at the moment somebody is relying on it and not
before. That is not hypothetical here: the shared template these bot
workflows came from dispatches `functional-tests.yml`, which this
repository does not have, and the whole reason they were deployed at all
is that a missing `pr-re-review.yml` let two pull requests merge with
their review fixes unreviewed. Nothing else in CI looks at these files
beyond their YAML syntax.
"""

import fnmatch
import os
import re
import subprocess
import tempfile
import tomllib
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
        # of another would fire every lane it prefixes from a single
        # comment, each of them booking a runner.
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

    The executable bit is only asserted when the script is the command
    word. `tools/run_remote ${primary} "sudo bash tools/ci_drain_check.sh"`
    names a path on a cluster node, executed there through bash, so the
    mode of this checkout's copy says nothing about whether it will run
    -- and asserting it would fail on the first reference to a script
    like tools/ci_node_checks.sh, which is 0644 and correctly so.
    """

    # Not preceded by a word character, dot, slash or hyphen, so
    # trusted-tools/ and .../shakenfist/tools/ do not match. The name
    # must start with a word character, so prose ending in "tools/."
    # does not either. An interpreter prefix is captured when present,
    # because that is what distinguishes "run this file" from "hand
    # this path to something else".
    REFERENCE = re.compile(
        r'(?<![\w./-])(?:(bash|sh|python3?)\s+)?tools/(\w[\w.-]*)')

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
            references = set((interp, script.rstrip('.,'))
                             for interp, script
                             in self.REFERENCE.findall(block))
            for interpreter, script in references:
                found = True
                path = os.path.join(REPO_ROOT, 'tools', script)
                with self.subTest(workflow=name, job=job_name, script=script):
                    self.assertTrue(
                        os.path.isfile(path),
                        '%s (job %s) names tools/%s, which does not exist'
                        % (name, job_name, script))
                    if interpreter:
                        continue
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

    def ci_exclusions(self):
        """Every negated pattern in ci.yml's code filter, '!' stripped."""
        with open(os.path.join(WORKFLOW_DIR, 'ci.yml')) as f:
            parsed = yaml.safe_load(f)
        for step in parsed['jobs']['check_paths']['steps']:
            filters = (step.get('with') or {}).get('filters')
            if filters:
                patterns = yaml.safe_load(filters)['code']
                found = [p[1:] for p in patterns if p.startswith('!')]
                self.assertTrue(
                    found, "ci.yml's code filter excludes nothing")
                return found
        self.fail('no paths-filter step found in ci.yml')

    def test_canary_ignores_what_the_ci_filter_excludes(self):
        # Every exclusion, not just the .vscode/ ones: a maintainer who
        # adds '!ARCHITECTURE.md' to the filter should be told if
        # canary.yml does not follow, which is the whole point of
        # claiming the invariant is enforced.
        #
        # Coverage is not always a literal match. canary.yml ignores
        # '**.md', which subsumes any markdown path ci.yml names -- and
        # deliberately reaches further, since a change to README.md
        # cannot alter an action but is still worth linting. The
        # asymmetry only ever runs one way: canary ignores at least
        # what the filter excludes, never less.
        with open(os.path.join(WORKFLOW_DIR, 'canary.yml')) as f:
            canary = yaml.safe_load(f)
        # 'on' is parsed as the boolean True by YAML 1.1.
        ignored = canary[True]['push']['paths-ignore']
        for pattern in self.ci_exclusions():
            with self.subTest(pattern=pattern):
                covered = (
                    pattern in ignored
                    or (pattern.endswith('.md') and '**.md' in ignored))
                self.assertTrue(
                    covered,
                    "ci.yml's path filter excludes %s but canary.yml's "
                    'paths-ignore neither names it nor subsumes it, so a '
                    'merged change to it books a smoke cluster' % pattern)

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


class LaneConditionTest(unittest.TestCase):
    """The conditions on ci.yml's lanes fail silently when they rot.

    Each term here is load bearing in a way that produces no error when
    it is lost -- a lane that quietly stops running, or one that quietly
    starts running fork code. Both are the shape this file exists for:
    a defect nothing reports until somebody is relying on the guard.
    """

    def setUp(self):
        with open(os.path.join(WORKFLOW_DIR, 'ci.yml')) as f:
            self.ci = yaml.safe_load(f)

    def test_lanes_gated_on_check_paths_fail_open(self):
        # Without !cancelled(), GitHub skips a job whose dependency did
        # not succeed -- and a skipped job satisfies branch protection.
        # A broken check_paths would then read as "the required checks
        # are green" on a repository where a merge to main deploys to
        # ten others.
        found = False
        for name, job in self.ci['jobs'].items():
            if 'check_paths' not in (job.get('needs') or []):
                continue
            found = True
            with self.subTest(job=name):
                self.assertIn(
                    '!cancelled()', job.get('if', ''),
                    '%s depends on check_paths without !cancelled(), so a '
                    'failure of the path filter would skip this lane and '
                    'still satisfy branch protection' % name)
                self.assertIn(
                    "!= 'false'", job.get('if', ''),
                    "%s should run unless the filter positively answered "
                    "'false'; testing for == 'true' would skip the lane on "
                    'an empty output' % name)
        self.assertTrue(found, 'no job depends on check_paths')

    def test_every_vm_lane_carries_the_fork_guard(self):
        # These runners hold /srv/github/id_ci, the key that reaches
        # every node in the CI mesh, and each lane executes the branch's
        # own code. The guard is per job, so a new lane added without it
        # is the whole estate.
        found = False
        for name, job in self.ci['jobs'].items():
            runs_on = job.get('runs-on')
            if not isinstance(runs_on, list) or 'vm' not in runs_on:
                continue
            found = True
            with self.subTest(job=name):
                self.assertIn(
                    'github.event.pull_request.head.repo.full_name == '
                    'github.repository', job.get('if', ''),
                    '%s runs branch code on a self-hosted VM runner '
                    'without the fork guard' % name)
        self.assertTrue(found, 'no self-hosted VM lanes found')

    def test_the_secret_scanner_does_not_consume_the_path_filter(self):
        # The rule this whole design rests on, stated in ci.yml's
        # comment, canary.yml's comment and docs/ci.md, and enforced
        # until now by nothing: a scanner exists to read the
        # human-written text a filter skips. Wiring gitleaks to
        # check_paths would make the secret scan skip exactly the
        # documentation and review notes it is there to read -- which
        # is the bug the fleet's other copy of this pattern has today,
        # and not repeating it is the point.
        gitleaks = self.ci['jobs']['gitleaks']
        self.assertNotIn(
            'check_paths', gitleaks.get('needs') or [],
            'gitleaks must not depend on check_paths: prose is where a '
            'secret or a smuggled character lands, so the scanner has to '
            'read what the filter skips')
        self.assertNotIn(
            'code_changed', gitleaks.get('if', ''),
            'gitleaks must not consume the path filter output, for the '
            'reason its needs: list already avoids')

    def test_the_filter_includes_everything_and_subtracts(self):
        # An "everything except" filter is only correct while it
        # actually starts from everything. Losing the '**' inclusion,
        # or reverting the quantifier to the default 'some', turns
        # every '!' exclusion below into a no-op -- silently, and in
        # the direction where the lanes simply always run, which
        # nobody notices.
        for step in self.ci['jobs']['check_paths']['steps']:
            spec = step.get('with') or {}
            if not spec.get('filters'):
                continue
            self.assertEqual(
                spec.get('predicate-quantifier'), 'some-with-excludes',
                "the filter needs 'some-with-excludes': the default 'some' "
                "makes '**' match everything and voids every exclusion")
            patterns = yaml.safe_load(spec['filters'])['code']
            self.assertIn(
                '**', patterns,
                "the code filter must start from '**'; without it the "
                'exclusions below subtract from nothing')
            return
        self.fail('no paths-filter step found in ci.yml')

    def test_check_paths_does_not_check_out_the_branch(self):
        # The absence of a fork guard on check_paths is only correct
        # because the job never obtains the branch's content: on a
        # pull_request event paths-filter reads pulls.listFiles instead.
        # Adding a checkout back would put a fork's files on the static
        # runner, which is what the missing guard would then be hiding.
        for step in self.ci['jobs']['check_paths']['steps']:
            self.assertNotIn(
                'actions/checkout', step.get('uses', ''),
                'check_paths carries no fork guard, which is only safe '
                'while it does not check the branch out')


class PruneGuardTest(unittest.TestCase):
    def test_prune_only_runs_on_main(self):
        # ci-prune-reviews.sh pushes to main whatever ref is checked
        # out, so a workflow_dispatch on a feature branch would push
        # that branch's unmerged commits to main, skipping review
        # entirely. The push trigger already only fires on main; this
        # guard is what makes workflow_dispatch match.
        with open(os.path.join(WORKFLOW_DIR, 'prune-reviews.yml')) as f:
            parsed = yaml.safe_load(f)
        self.assertIn('workflow_dispatch', parsed[True])
        for name, job in parsed['jobs'].items():
            with self.subTest(job=name):
                self.assertIn(
                    "github.ref == 'refs/heads/main'", job.get('if', ''),
                    '%s pushes to main whatever ref is checked out, so it '
                    'must refuse to run on any other ref' % name)


class ReviewScopeTest(unittest.TestCase):
    """The scope file decides what gets reviewed, and nothing else reads it.

    A typo in a pattern does not fail anything -- it silently shrinks
    the set of files under review, which is the one failure this whole
    adoption cannot afford and the one nothing else would report.
    """

    SCOPE = os.path.join(REPO_ROOT, '.vscode', 'review-scope.toml')

    def setUp(self):
        with open(self.SCOPE, 'rb') as f:
            self.scope = tomllib.load(f)

    def tracked_files(self):
        out = subprocess.run(
            ['git', 'ls-files'], cwd=REPO_ROOT, check=True,
            stdout=subprocess.PIPE, text=True).stdout.split()
        self.assertTrue(out, 'git ls-files returned nothing')
        return out

    def test_every_include_pattern_matches_a_tracked_file(self):
        # fnmatch against the whole repo-relative path, with '*'
        # matching across separators -- the semantics the scope file's
        # own header documents.
        files = self.tracked_files()
        for pattern in self.scope['include']:
            with self.subTest(pattern=pattern):
                self.assertTrue(
                    any(fnmatch.fnmatch(f, pattern) for f in files),
                    'include pattern %r matches no tracked file, so it is '
                    'either a typo or names a file that has gone away'
                    % pattern)

    def test_every_exclude_pattern_matches_a_tracked_file(self):
        files = self.tracked_files()
        for pattern in self.scope['exclude']:
            with self.subTest(pattern=pattern):
                self.assertTrue(
                    any(fnmatch.fnmatch(f, pattern) for f in files),
                    'exclude pattern %r matches no tracked file' % pattern)

    def test_the_documented_out_of_scope_files_are_out_of_scope(self):
        # The header names four files it deliberately leaves out and
        # says why for each. If a later widening of the include list
        # pulls one back in, that argument needs revisiting rather than
        # silently ceasing to be true.
        deliberately_out = [
            '.flake8',
            '.gitignore',
            'etc/ovirt-45-rocky-8-repos.patch',
            'ansible/files/shakenfist-ci-failure-loki.cwd',
        ]
        include = self.scope['include']
        for path in deliberately_out:
            with self.subTest(path=path):
                self.assertIn(path, self.tracked_files())
                self.assertFalse(
                    any(fnmatch.fnmatch(path, p) for p in include),
                    '%s is named in review-scope.toml as deliberately out '
                    'of scope, with a reason, but the include patterns now '
                    'match it' % path)


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
