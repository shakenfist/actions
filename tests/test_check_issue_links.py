# Copyright 2026 Michael Still and contributors

"""Tests for tools/check-issue-links.py.

The cases below are the real ones from the 2026-09-07 audit of
shakenfist/shakenfist rather than invented shapes, because the whole
difficulty of this check is telling a directive from a sentence, and real
pull request prose is what it has to survive.
"""

import importlib.util
import os
import unittest


def _load():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'tools', 'check-issue-links.py')
    spec = importlib.util.spec_from_file_location('check_issue_links', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cil = _load()


class StandaloneStanzaTestCase(unittest.TestCase):
    def test_a_plain_stanza_is_a_directive(self):
        self.assertEqual({4095}, cil.standalone_stanzas('Fixes #4095'))

    def test_a_trailing_full_stop_is_still_a_directive(self):
        self.assertEqual({3662}, cil.standalone_stanzas('Fixes #3662.'))

    def test_a_backticked_stanza_is_still_intent(self):
        # PR #4106. GitHub ignores this, which is the entire point: the
        # author meant it, so the check has to see it in order to complain.
        self.assertEqual({4087}, cil.standalone_stanzas('`Fixes #4087`'))

    def test_two_directives_may_share_a_line(self):
        # PR #3506.
        self.assertEqual(
            {3499, 3500},
            cil.standalone_stanzas('Closes #3499. Closes #3500.'))

    def test_prose_is_not_a_directive(self):
        # PR #4108, the sentence which closed #4092 by accident.
        self.assertEqual(
            set(),
            cil.standalone_stanzas(
                'The fix #4092 asks for would have been deleted by the next '
                're-derivation.'))

    def test_a_disclaimer_is_not_a_directive(self):
        # The #3816 commit whose sentence disclaiming a closure caused one.
        self.assertEqual(
            set(),
            cil.standalone_stanzas(
                'Deliberately does not close #3813 -- this records the bug.'))

    def test_a_heading_is_not_a_directive(self):
        # PR #3829. Deliberate, but we would rather stay quiet than guess.
        self.assertEqual(
            set(),
            cil.standalone_stanzas('## Fixes #3733 -- pre-push audit file'))

    def test_a_wrapped_keyword_does_not_reach_across_lines(self):
        # GitHub itself would parse this; we deliberately do not, because
        # the sentence it comes from is a disclaimer, not a directive.
        self.assertEqual(
            set(),
            cil.standalone_stanzas('Deliberately does not close\n#3813 --'))


class DeclaredIntentTestCase(unittest.TestCase):
    def test_the_branch_name_declares_intent(self):
        self.assertEqual(
            {4087}, cil.declared_intent('issue-fix-4087', '', []))

    def test_the_older_bug_prefix_also_declares_intent(self):
        # PR #3374 used bug-3370, before the issue-fix convention.
        self.assertEqual({3370}, cil.declared_intent('bug-3370', '', []))

    def test_an_unrelated_branch_declares_nothing(self):
        self.assertEqual(
            set(), cil.declared_intent('sync-docs', 'A docs sync.', []))

    def test_a_commit_message_declares_intent(self):
        # PR #3374 had an empty description and three commit stanzas.
        self.assertEqual(
            {3370, 3371, 3373},
            cil.declared_intent(
                'bug-3370', '',
                ['Fix the crash loop\n\nFixes #3370',
                 'Log at the right level\n\nFixes #3371',
                 'Separate absence from unavailability\n\nFixes #3373']))

    def test_an_opt_out_withdraws_a_claim(self):
        self.assertEqual(
            set(),
            cil.declared_intent(
                'issue-fix-4087', 'Only part of it.\n\nX-No-Autoclose: #4087',
                []))


class EvaluateTestCase(unittest.TestCase):
    def test_a_backticked_stanza_fails_the_check(self):
        # PR #4106 as it actually merged. Both signals present, GitHub
        # parsed neither, #4087 stayed open.
        missing, unexpected, declared = cil.evaluate(
            'issue-fix-4087', '`Fixes #4087`\n\n## The mechanism\n',
            ['Guard placement\n\nFixes #4087'], closing_refs=set())
        self.assertEqual([4087], missing)
        self.assertEqual([], unexpected)
        self.assertEqual([4087], declared)

    def test_an_empty_description_fails_the_check(self):
        # PR #3374. Nothing in the description at all.
        missing, _, _ = cil.evaluate(
            'bug-3370', '', ['Fix the crash loop\n\nFixes #3370'],
            closing_refs=set())
        self.assertEqual([3370], missing)

    def test_a_plain_stanza_passes(self):
        # PR #4105, which closed #4095 exactly as intended.
        missing, unexpected, _ = cil.evaluate(
            'issue-fix-4095', 'Fixes #4095\n\nA description.',
            ['Let explicit requests take over halos\n\nFixes #4095'],
            closing_refs={4095})
        self.assertEqual([], missing)
        self.assertEqual([], unexpected)

    def test_an_already_closed_issue_is_not_demanded(self):
        # PR #3699 dropped its stanza because #3685 had closed #3642 hours
        # earlier. Insisting on a link there would be noise.
        missing, _, _ = cil.evaluate(
            'issue-fix-3642', 'Superseded by #3685.', [],
            closing_refs=set(), already_closed={3642})
        self.assertEqual([], missing)

    def test_an_unasked_for_closure_is_reported_but_does_not_fail(self):
        # The #3816 shape: prose closing an issue nobody claimed.
        missing, unexpected, _ = cil.evaluate(
            'scheduler-demand-guard-3813',
            "none of D6's three positions closes #3565, because soft "
            'affinity is never reached',
            [], closing_refs={3565})
        self.assertEqual([], missing)
        self.assertEqual([3565], unexpected)

    def test_a_pull_request_claiming_nothing_passes_quietly(self):
        missing, unexpected, declared = cil.evaluate(
            'sync-docs', 'Automated documentation sync.', [],
            closing_refs=set())
        self.assertEqual(([], [], []), (missing, unexpected, declared))


class ReportTestCase(unittest.TestCase):
    def test_the_failure_names_the_issue_and_the_remedy(self):
        lines = '\n'.join(cil.build_report([4087], set(), [4087], []))
        self.assertIn('#4087 will NOT be closed', lines)
        self.assertIn('Fixes #4087', lines)
        self.assertIn('X-No-Autoclose: #4087', lines)

    def test_an_unexpected_closure_says_it_does_not_fail(self):
        lines = '\n'.join(cil.build_report([], {3565}, [], [3565]))
        self.assertIn('does not fail the build', lines)
        self.assertNotIn('FAILED', lines)


if __name__ == '__main__':
    unittest.main()
