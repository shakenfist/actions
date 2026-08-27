#!/usr/bin/env python3

"""Tests for review-pr-with-claude/render-review.py.

This turns the reviewer model's JSON into the markdown comment posted on
every pull request across the fleet, and into the machine-readable block
appended to it. The round trip is the part worth pinning down: the
posted comment is the only durable copy of a review once the run's
artifacts expire, so if the embedded JSON stops being recoverable the
structured form of every review is lost with it.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from tests.helpers import load_script


render = load_script(
    'review-pr-with-claude/render-review.py', 'render_review')


def item(item_id, action, title='A finding', **kwargs):
    base = {
        'id': item_id,
        'title': title,
        'category': kwargs.pop('category', 'bug'),
        'action': action,
    }
    base.update(kwargs)
    return base


class StripNullsTest(unittest.TestCase):
    def test_removes_null_valued_keys(self):
        self.assertEqual(
            render.strip_nulls({'a': 1, 'b': None}), {'a': 1})

    def test_recurses_into_nested_dicts_and_lists(self):
        data = {
            'items': [
                {'id': 1, 'rationale': None, 'nested': {'x': None, 'y': 2}},
            ],
            'summary': 'hi',
        }
        self.assertEqual(render.strip_nulls(data), {
            'items': [{'id': 1, 'nested': {'y': 2}}],
            'summary': 'hi',
        })

    def test_preserves_falsey_values_that_are_not_null(self):
        # An explicit false or zero or empty string means something;
        # only null is the model declining to fill a field in.
        data = {'adequate': False, 'count': 0, 'note': '', 'missing': []}
        self.assertEqual(render.strip_nulls(data), data)

    def test_leaves_scalars_alone(self):
        self.assertEqual(render.strip_nulls('text'), 'text')
        self.assertEqual(render.strip_nulls(7), 7)


class ValidateReviewTest(unittest.TestCase):
    def test_the_schema_sits_beside_the_script(self):
        # load_schema() returns None when review-schema.json is not next
        # to render-review.py, and validate_review() then returns valid
        # without checking anything -- every review passes, including
        # ones the schema would reject. That is a bypass rather than
        # weaker validation, so the co-location is pinned here rather
        # than left to the docstring that describes it.
        self.assertTrue(render.SCHEMA_PATH.exists(), render.SCHEMA_PATH)
        self.assertIsInstance(render.load_schema(), dict)

    def test_accepts_a_minimal_valid_review(self):
        valid, error = render.validate_review(
            {'summary': 'Looks fine', 'items': []})
        self.assertTrue(valid, error)

    def test_rejects_an_unknown_action(self):
        valid, error = render.validate_review({
            'summary': 's',
            'items': [item(1, 'explode')],
        })
        self.assertFalse(valid)
        self.assertIn('explode', error)

    def test_rejects_a_missing_summary(self):
        valid, _ = render.validate_review({'items': []})
        self.assertFalse(valid)

    def test_rejects_items_that_are_not_a_list(self):
        valid, _ = render.validate_review(
            {'summary': 's', 'items': 'nope'})
        self.assertFalse(valid)

    def test_rejects_an_item_missing_a_required_field(self):
        valid, _ = render.validate_review({
            'summary': 's',
            'items': [{'id': 1, 'title': 't', 'category': 'bug'}],
        })
        self.assertFalse(valid)


class RenderMarkdownTest(unittest.TestCase):
    def test_summary_is_rendered(self):
        text = render.render_markdown(
            {'summary': 'All good here', 'items': []})
        self.assertIn('## PR Review', text)
        self.assertIn('### Summary', text)
        self.assertIn('All good here', text)

    def test_missing_summary_falls_back_rather_than_raising(self):
        text = render.render_markdown({'items': []})
        self.assertIn('No summary provided.', text)

    def test_a_caveat_is_rendered_above_the_summary(self):
        # A salvaged review carries a caveat saying so. It has to be
        # the first thing read: a partial review taken for a complete
        # one says the parts nobody looked at are fine.
        text = render.render_markdown({
            'summary': 'All good here',
            'caveat': 'The response was truncated.',
            'items': [],
        })
        self.assertIn('Incomplete review', text)
        self.assertIn('The response was truncated.', text)
        self.assertLess(text.index('Incomplete review'),
                        text.index('### Summary'))

    def test_no_caveat_means_no_warning(self):
        text = render.render_markdown({'summary': 's', 'items': []})
        self.assertNotIn('Incomplete review', text)

    def test_a_review_with_a_caveat_still_validates(self):
        valid, error = render.validate_review({
            'summary': 's', 'items': [], 'caveat': 'Truncated.'})
        self.assertTrue(valid, error)

    def test_fix_and_document_items_share_the_action_items_section(self):
        text = render.render_markdown({
            'summary': 's',
            'items': [item(1, 'fix', 'Broken thing'),
                      item(2, 'document', 'Undocumented thing')],
        })
        self.assertIn('### Action Items', text)
        self.assertIn('Broken thing', text)
        self.assertIn('Undocumented thing', text)
        self.assertNotIn('### Suggestions', text)
        self.assertNotIn('### Observations', text)

    def test_consider_items_are_suggestions_not_action_items(self):
        text = render.render_markdown({
            'summary': 's',
            'items': [item(1, 'consider', 'Maybe rename this')],
        })
        self.assertIn('### Suggestions', text)
        self.assertNotIn('### Action Items', text)

    def test_none_items_are_observations(self):
        text = render.render_markdown({
            'summary': 's',
            'items': [item(1, 'none', 'Just noting')],
        })
        self.assertIn('### Observations', text)
        self.assertNotIn('### Action Items', text)

    def test_unknown_action_is_treated_as_informational(self):
        text = render.render_markdown({
            'summary': 's',
            'items': [item(1, 'something-else', 'Odd one')],
        })
        self.assertIn('### Observations', text)

    def test_empty_review_renders_no_finding_sections(self):
        text = render.render_markdown({'summary': 'Nothing found', 'items': []})
        for heading in ('### Action Items', '### Suggestions',
                        '### Observations', '### Related Issues'):
            self.assertNotIn(heading, text)

    def test_issue_numbers_become_closes_lines(self):
        # This is the mechanism that closes the review's issues when the
        # PR merges, so the exact "Closes #N" spelling matters.
        text = render.render_markdown({
            'summary': 's',
            'items': [item(1, 'fix', issue_number=42),
                      item(2, 'fix', issue_number=43)],
        })
        self.assertIn('### Related Issues', text)
        self.assertIn('- Closes #42', text)
        self.assertIn('- Closes #43', text)

    def test_items_without_issue_numbers_produce_no_related_section(self):
        text = render.render_markdown({
            'summary': 's', 'items': [item(1, 'fix')]})
        self.assertNotIn('### Related Issues', text)

    def test_positive_feedback_is_rendered(self):
        text = render.render_markdown({
            'summary': 's',
            'items': [],
            'positive_feedback': [
                {'title': 'Good tests', 'description': 'Thorough.'}],
        })
        self.assertIn("### What's Good", text)
        self.assertIn('Good tests', text)
        self.assertIn('Thorough.', text)

    def test_adequate_test_coverage_is_reported(self):
        text = render.render_markdown({
            'summary': 's', 'items': [],
            'test_coverage': {'adequate': True},
        })
        self.assertIn('### Test Coverage', text)
        self.assertIn('adequate', text)

    def test_inadequate_test_coverage_lists_missing_scenarios(self):
        text = render.render_markdown({
            'summary': 's', 'items': [],
            'test_coverage': {
                'adequate': False,
                'missing': ['the error path', 'the empty case'],
            },
        })
        self.assertIn('may need improvement', text)
        self.assertIn('- the error path', text)
        self.assertIn('- the empty case', text)

    def test_embedded_json_round_trips(self):
        # This block is how a review is recovered from the posted
        # comment. If it stops being valid JSON, anything reading a
        # review back quietly loses its input.
        data = {
            'summary': 'A summary',
            'items': [item(1, 'fix', 'Thing', location='a.py:1')],
        }
        text = render.render_markdown(data, embed_json=True)
        self.assertIn('<details>', text)
        body = text.split('```json', 1)[1].split('```', 1)[0]
        self.assertEqual(json.loads(body), data)

    def test_json_is_not_embedded_by_default(self):
        text = render.render_markdown({'summary': 's', 'items': []})
        self.assertNotIn('```json', text)

    def test_footer_advertises_no_retired_trigger(self):
        # The comment addresser was retired in August 2026. The footer
        # used to tell every reader on every fleet pull request to type
        # its trigger phrase, which now names a command nothing answers
        # -- no workflow, no reply, no failure.
        text = render.render_markdown({'summary': 's', 'items': []})
        self.assertIn('automated reviewer', text)
        self.assertNotIn('please address comments', text)


class RenderItemTest(unittest.TestCase):
    def test_action_label_and_id_lead_the_title(self):
        lines = render.render_item(item(7, 'fix', 'Off by one'))
        self.assertIn('**7. [FIX]**', lines[0])
        self.assertIn('Off by one', lines[0])

    def test_each_action_gets_its_own_label(self):
        for action, label in (('fix', 'FIX'), ('document', 'DOC'),
                              ('consider', 'CONSIDER'), ('none', 'INFO')):
            lines = render.render_item(item(1, action))
            self.assertIn('[%s]' % label, lines[0])

    def test_unknown_action_and_category_fall_back(self):
        lines = render.render_item(
            item(1, 'mystery', category='mystery'))
        self.assertIn('[INFO]', lines[0])
        self.assertIn('📋', lines[0])

    def test_severity_emoji_is_included_when_present(self):
        lines = render.render_item(item(1, 'fix', severity='critical'))
        self.assertIn('🔴', lines[0])

    def test_absent_severity_adds_no_emoji(self):
        lines = render.render_item(item(1, 'fix'))
        for emoji in render.SEVERITY_EMOJI.values():
            self.assertNotIn(emoji, lines[0])

    def test_issue_number_is_linked_in_the_title(self):
        lines = render.render_item(item(1, 'fix', issue_number=99))
        self.assertIn('(#99)', lines[0])

    def test_optional_fields_are_rendered_when_present(self):
        text = '\n'.join(render.render_item(item(
            1, 'fix',
            description='It is wrong',
            location='foo.py:10',
            suggestion='Make it right',
            rationale='Because')))
        self.assertIn('It is wrong', text)
        self.assertIn('`foo.py:10`', text)
        self.assertIn('Make it right', text)
        self.assertIn('Because', text)

    def test_optional_fields_are_omitted_when_absent(self):
        text = '\n'.join(render.render_item(item(1, 'fix')))
        self.assertNotIn('📍', text)
        self.assertNotIn('💡', text)
        self.assertNotIn('ℹ️', text)


class LoadReviewTest(unittest.TestCase):
    """The input is model output, so it does not always parse."""

    def write(self, content):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, 'review.json')
        with open(path, 'w') as f:
            f.write(content)
        return path

    def test_a_review_is_loaded_with_its_nulls_stripped(self):
        path = self.write(json.dumps(
            {'summary': 's', 'items': [], 'test_coverage': None}))
        self.assertEqual(
            render.load_review(path), {'summary': 's', 'items': []})

    def test_invalid_json_exits_one_with_a_message_on_stderr(self):
        # A traceback here reads as the renderer being broken. The
        # extractor can hand this script a file only when something
        # upstream went wrong, and the CI log has to say which.
        path = self.write('{"summary": "s", "items": [')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                render.load_review(path)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('is not valid JSON', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
