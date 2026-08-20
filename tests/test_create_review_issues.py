#!/usr/bin/env python3

"""Tests for review-pr-with-claude/create-review-issues.py.

The label mapping here decides how every automated-review issue across
the fleet is triaged, and the body is the only context the issue carries
once it has outlived the pull request that produced it.
"""

import unittest

from tests.helpers import load_script


issues = load_script(
    'review-pr-with-claude/create-review-issues.py', 'create_review_issues')


class LabelsTest(unittest.TestCase):
    def test_every_issue_is_marked_as_automated(self):
        # This label is how the automated-review issues are told apart
        # from human-filed ones, so it belongs on all of them.
        for category in ('security', 'bug', 'documentation', 'testing',
                         'style', 'other', ''):
            labels = issues.get_labels_for_item({'category': category})
            self.assertIn('automated-review', labels)

    def test_recognised_categories_add_their_own_label(self):
        for category in ('security', 'bug', 'documentation', 'testing'):
            self.assertIn(
                category, issues.get_labels_for_item({'category': category}))

    def test_unrecognised_categories_add_no_category_label(self):
        self.assertEqual(
            issues.get_labels_for_item({'category': 'style'}),
            ['automated-review'])
        self.assertEqual(
            issues.get_labels_for_item({'category': 'other'}),
            ['automated-review'])

    def test_missing_category_is_tolerated(self):
        self.assertEqual(
            issues.get_labels_for_item({}), ['automated-review'])

    def test_critical_and_high_severity_are_both_high_priority(self):
        for severity in ('critical', 'high'):
            self.assertIn(
                'priority:high',
                issues.get_labels_for_item({'severity': severity}))

    def test_medium_severity_is_medium_priority(self):
        self.assertIn(
            'priority:medium',
            issues.get_labels_for_item({'severity': 'medium'}))

    def test_low_severity_gets_no_priority_label(self):
        labels = issues.get_labels_for_item({'severity': 'low'})
        self.assertNotIn('priority:high', labels)
        self.assertNotIn('priority:medium', labels)

    def test_absent_severity_gets_no_priority_label(self):
        labels = issues.get_labels_for_item({'category': 'bug'})
        self.assertEqual(labels, ['automated-review', 'bug'])

    def test_category_and_severity_combine(self):
        labels = issues.get_labels_for_item(
            {'category': 'security', 'severity': 'critical'})
        self.assertEqual(
            labels, ['automated-review', 'security', 'priority:high'])


class IssueBodyTest(unittest.TestCase):
    def test_body_names_the_originating_pr(self):
        body = issues.build_issue_body({'title': 'A thing'}, 314)
        self.assertIn('automated review of PR #314', body)

    def test_description_is_included_under_its_own_heading(self):
        body = issues.build_issue_body(
            {'description': 'The widget leaks.'}, 1)
        self.assertIn('## Description', body)
        self.assertIn('The widget leaks.', body)

    def test_suggestion_is_included_under_its_own_heading(self):
        body = issues.build_issue_body(
            {'suggestion': 'Close the widget.'}, 1)
        self.assertIn('## Suggestion', body)
        self.assertIn('Close the widget.', body)

    def test_location_is_rendered_as_code(self):
        body = issues.build_issue_body({'location': 'widget.py:42'}, 1)
        self.assertIn('`widget.py:42`', body)

    def test_optional_fields_are_omitted_when_absent(self):
        body = issues.build_issue_body({'title': 'Bare'}, 1)
        self.assertNotIn('## Description', body)
        self.assertNotIn('## Suggestion', body)
        self.assertNotIn('**Location:**', body)

    def test_body_carries_no_inert_closes_line(self):
        # "Closes #N" only closes anything when it appears in a pull
        # request. The working reference is the one render-review.py
        # puts in the review comment on the PR, pointing at this issue;
        # the reverse direction in an issue body does nothing, and used
        # to be emitted here as "Closes #42 addresses this issue."
        body = issues.build_issue_body({'title': 'T'}, 42)
        self.assertNotIn('Closes #', body)

    def test_body_still_points_back_at_the_pull_request(self):
        body = issues.build_issue_body({'title': 'T'}, 42)
        self.assertIn('PR #42', body)
        self.assertIn('closed by that pull request when it merges', body)

    def test_an_item_with_nothing_but_a_title_still_produces_a_body(self):
        body = issues.build_issue_body({'title': 'Bare'}, 7)
        self.assertTrue(body.strip())
        self.assertIn('PR #7', body)


class CreateIssueTest(unittest.TestCase):
    """create_issue shells out to gh, so drive it with a fake subprocess."""

    def setUp(self):
        self.real_run = issues.subprocess.run
        self.addCleanup(setattr, issues.subprocess, 'run', self.real_run)

    def fake_run(self, result=None, exception=None):
        calls = []

        def _run(cmd, **kwargs):
            calls.append(cmd)
            if exception is not None:
                raise exception
            return result

        issues.subprocess.run = _run
        return calls

    def test_issue_number_is_parsed_from_the_returned_url(self):
        class Result:
            stdout = 'https://github.com/shakenfist/actions/issues/123\n'
        self.fake_run(result=Result())
        self.assertEqual(
            issues.create_issue('shakenfist/actions', 't', 'b', [], 1),
            (123, 'https://github.com/shakenfist/actions/issues/123'))

    def test_trailing_slash_on_the_url_is_tolerated(self):
        class Result:
            stdout = 'https://github.com/shakenfist/actions/issues/9/\n'
        self.fake_run(result=Result())
        number, _ = issues.create_issue('shakenfist/actions', 't', 'b', [], 1)
        self.assertEqual(number, 9)

    def test_labels_are_passed_through_to_gh(self):
        class Result:
            stdout = 'https://github.com/shakenfist/actions/issues/1'
        calls = self.fake_run(result=Result())
        issues.create_issue(
            'shakenfist/actions', 'title', 'body', ['bug', 'security'], 1)
        cmd = calls[0]
        self.assertEqual(cmd[:3], ['gh', 'issue', 'create'])
        self.assertIn('--repo', cmd)
        self.assertEqual(cmd[cmd.index('--repo') + 1], 'shakenfist/actions')
        self.assertEqual(
            [cmd[i + 1] for i, a in enumerate(cmd) if a == '--label'],
            ['bug', 'security'])

    def test_a_gh_failure_returns_none_rather_than_raising(self):
        # A review that cannot file its issues should still post its
        # comment, so this path degrades instead of aborting the run.
        error = issues.subprocess.CalledProcessError(1, 'gh')
        error.stderr = 'nope'
        self.fake_run(exception=error)
        self.assertIsNone(
            issues.create_issue('shakenfist/actions', 't', 'b', [], 1))

    def test_an_unparseable_url_returns_none_rather_than_raising(self):
        class Result:
            stdout = 'not a url at all'
        self.fake_run(result=Result())
        self.assertIsNone(
            issues.create_issue('shakenfist/actions', 't', 'b', [], 1))


if __name__ == '__main__':
    unittest.main()
