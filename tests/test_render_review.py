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
import sys
import tempfile
import unittest
import unittest.mock

from tests.helpers import load_script


render = load_script(
    'review-pr-with-claude/render-review.py', 'render_review')


def load_without_jsonschema():
    """Load the renderer as it runs where jsonschema is not installed.

    A None entry in sys.modules makes the import raise ImportError,
    which is the branch render-review.py takes to its own validator.
    """
    with unittest.mock.patch.dict(sys.modules, {'jsonschema': None}):
        return load_script('review-pr-with-claude/render-review.py',
                           'render_review_without_jsonschema')


render_fallback = load_without_jsonschema()


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


class ValidateReviewCases:
    """What either validator has to accept and reject.

    Run once against each branch of validate_review(), since which one
    a review meets depends on whether the runner has jsonschema.
    """

    module = None

    def assert_invalid(self, review):
        valid, error = self.module.validate_review(review)
        self.assertFalse(valid, review)
        return error

    def test_accepts_a_full_review(self):
        valid, error = self.module.validate_review({
            'summary': 's',
            'caveat': 'c',
            'items': [item(1, 'fix', severity='low', description='d',
                           location='l', suggestion='s', rationale='r')],
            'positive_feedback': [{'title': 't', 'description': 'd'}],
            'test_coverage': {'adequate': False, 'missing': ['m']},
        })
        self.assertTrue(valid, error)

    def test_accepts_a_minimal_review(self):
        valid, error = self.module.validate_review(
            {'summary': 'Looks fine', 'items': []})
        self.assertTrue(valid, error)

    def test_rejects_an_unknown_action(self):
        error = self.assert_invalid(
            {'summary': 's', 'items': [item(1, 'explode')]})
        self.assertIn('explode', error)

    def test_rejects_an_unknown_category(self):
        self.assert_invalid({
            'summary': 's', 'items': [item(1, 'fix', category='vibes')]})

    def test_rejects_an_unknown_severity(self):
        self.assert_invalid({
            'summary': 's', 'items': [item(1, 'fix', severity='urgent')]})

    def test_rejects_an_id_that_is_not_an_integer(self):
        for bad in ('1', 1.5, True):
            self.assert_invalid(
                {'summary': 's', 'items': [item(bad, 'fix')]})

    def test_rejects_an_item_that_is_not_an_object(self):
        self.assert_invalid({'summary': 's', 'items': ['a finding']})

    def test_rejects_a_missing_summary(self):
        self.assert_invalid({'items': []})

    def test_rejects_items_that_are_not_a_list(self):
        self.assert_invalid({'summary': 's', 'items': 'nope'})

    def test_rejects_an_item_missing_a_required_field(self):
        self.assert_invalid({
            'summary': 's',
            'items': [{'id': 1, 'title': 't', 'category': 'bug'}],
        })

    def test_rejects_positive_feedback_without_a_title(self):
        # render_markdown() indexes the title, so letting this through
        # turns a bad review into a traceback instead of a red job.
        self.assert_invalid({
            'summary': 's', 'items': [],
            'positive_feedback': [{'description': 'd'}]})

    def test_rejects_test_coverage_of_the_wrong_shape(self):
        self.assert_invalid({
            'summary': 's', 'items': [],
            'test_coverage': {'adequate': 'yes'}})
        self.assert_invalid({
            'summary': 's', 'items': [],
            'test_coverage': {'missing': [1]}})


class ValidateReviewTest(ValidateReviewCases, unittest.TestCase):
    module = render

    def test_the_schema_sits_beside_the_script(self):
        # load_schema() returns None when review-schema.json is not next
        # to render-review.py, and validate_review() then returns valid
        # without checking anything -- every review passes, including
        # ones the schema would reject. That is a bypass rather than
        # weaker validation, so the co-location is pinned here rather
        # than left to the docstring that describes it.
        self.assertTrue(render.SCHEMA_PATH.exists(), render.SCHEMA_PATH)
        self.assertIsInstance(render.load_schema(), dict)


class FallbackValidateReviewTest(ValidateReviewCases, unittest.TestCase):
    """The validator used where jsonschema is not installed."""

    module = render_fallback

    def test_this_is_the_fallback_branch(self):
        self.assertFalse(self.module.HAS_JSONSCHEMA)

    def test_the_fallback_reads_the_schema_file(self):
        # Widen the category enum in a copy of the schema, and the
        # fallback has to follow it: nothing in the script restates it.
        schema = self.module.load_schema()
        item_schema = schema['properties']['items']['items']
        item_schema['properties']['category']['enum'].append('vibes')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, 'review-schema.json')
        with open(path, 'w') as f:
            json.dump(schema, f)

        review = {'summary': 's', 'items': [item(1, 'fix', category='vibes')]}
        self.assertFalse(self.module.validate_review(review)[0])
        with unittest.mock.patch.object(
                self.module, 'SCHEMA_PATH', render.Path(path)):
            valid, error = self.module.validate_review(review)
        self.assertTrue(valid, error)

    def test_the_schema_uses_only_keywords_the_fallback_enforces(self):
        # A keyword the fallback does not know -- minLength, say -- is a
        # constraint it would skip without a word, which is exactly the
        # drift between the two validators this exists to prevent.
        known = set(self.module.FALLBACK_KEYWORDS
                    + self.module.ANNOTATION_KEYWORDS)

        def walk(node, path):
            unknown = set(node) - known
            self.assertFalse(unknown, f'{path} uses {sorted(unknown)}')
            for name, subschema in node.get('properties', {}).items():
                walk(subschema, f'{path}.{name}')
            if 'items' in node:
                walk(node['items'], f'{path}[]')

        walk(self.module.load_schema(), 'schema')

    def test_an_invalid_review_is_a_clean_failure_not_a_traceback(self):
        # Without jsonschema this used to validate, and then raise
        # KeyError rendering the feedback's title.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, 'review.json')
        with open(path, 'w') as f:
            json.dump({'summary': 's', 'items': [],
                       'positive_feedback': [{'description': 'd'}]}, f)
        stderr = io.StringIO()
        argv = ['render-review.py', path]
        with contextlib.redirect_stderr(stderr):
            with unittest.mock.patch.object(sys, 'argv', argv):
                with self.assertRaises(SystemExit) as caught:
                    self.module.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('Invalid review JSON', stderr.getvalue())


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
