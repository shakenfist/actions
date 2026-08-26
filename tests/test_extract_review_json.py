#!/usr/bin/env python3

"""Tests for review-pr-with-claude/extract-review-json.py.

This is the piece that decides whether a review survives. The reviewer
runs against every pull request in the fleet, and on a large diff its
response arrives cut off mid-JSON often enough that discarding those
throws away most of a review that has already been paid for. What is
pinned here is the salvage: what it recovers, what it refuses to
recover, and that anything recovered is labelled partial so a truncated
review is never read as a clean one.
"""

import json
import unittest

from tests.helpers import load_script


extract = load_script(
    'review-pr-with-claude/extract-review-json.py', 'extract_review_json')


def item(item_id, title='A finding'):
    return {
        'id': item_id,
        'title': title,
        'category': 'bug',
        'action': 'none',
    }


def fenced(payload):
    return f'Here is the review.\n\n```json\n{payload}\n```\n'


REVIEW = {
    'summary': 'Adds a thing',
    'items': [item(1), item(2, 'Another finding')],
}


class FindJsonBlockTest(unittest.TestCase):
    def test_finds_a_fenced_block(self):
        block = extract.find_json_block(fenced('{"summary": "s"}'))
        self.assertEqual(block, '{"summary": "s"}')

    def test_ignores_prose_around_the_block(self):
        text = ('I read the diff.\n\n```json\n{"a": 1}\n```\n\n'
                'Hope that helps.\n')
        self.assertEqual(extract.find_json_block(text), '{"a": 1}')

    def test_an_unclosed_fence_yields_everything_after_it(self):
        # This is the truncation case: the response stopped before the
        # closing fence was written.
        text = 'Review:\n\n```json\n{"summary": "s", "items": [{"id": 1'
        self.assertEqual(
            extract.find_json_block(text),
            '{"summary": "s", "items": [{"id": 1')

    def test_falls_back_to_a_bare_object(self):
        text = 'No fences today. {"summary": "s", "items": []}'
        self.assertEqual(
            extract.find_json_block(text),
            '{"summary": "s", "items": []}')

    def test_a_brace_that_is_not_the_review_is_not_taken(self):
        # A JSON snippet quoted in prose has no summary near it, so it
        # is passed over rather than returned as the review.
        self.assertIsNone(
            extract.find_json_block('Consider {"adequate": true} here.'))

    def test_returns_none_when_there_is_no_json_at_all(self):
        self.assertIsNone(
            extract.find_json_block('I could not review this PR.'))


class SalvageTest(unittest.TestCase):
    def test_recovers_items_completed_before_the_cut(self):
        text = json.dumps(REVIEW)
        truncated = text[:text.index('"Another finding"')] + '"Anoth'
        data = extract.salvage(truncated)
        self.assertEqual(data['summary'], 'Adds a thing')
        self.assertEqual([i['id'] for i in data['items']], [1])

    def test_a_brace_inside_a_string_is_not_a_cut_point(self):
        # A description mentioning a brace must not be mistaken for
        # structure when walking back to a safe cut point.
        review = {
            'summary': 's',
            'items': [dict(item(1), description='look at }] here'),
                      item(2)],
        }
        text = json.dumps(review)
        data = extract.salvage(text[:text.index('{"id": 2')])
        self.assertEqual([i['id'] for i in data['items']], [1])
        self.assertIn('}]', data['items'][0]['description'])

    def test_an_item_cut_before_it_closed_is_dropped_entirely(self):
        # Half an item validates as nothing: the schema wants id,
        # title, category and action on every one of them. Better to
        # lose the unfinished finding than the whole review.
        text = json.dumps(REVIEW)
        data = extract.salvage(text[:text.index('"Another finding"')])
        self.assertEqual([i['id'] for i in data['items']], [1])

    def test_trailing_prose_after_a_complete_object_is_dropped(self):
        text = json.dumps(REVIEW) + '\n\nHope that helps!'
        self.assertEqual(extract.salvage(text), REVIEW)

    def test_recovers_when_the_cut_is_at_a_trailing_comma(self):
        text = json.dumps(REVIEW)
        truncated = text[:text.index('{"id": 2')]
        data = extract.salvage(truncated)
        self.assertEqual([i['id'] for i in data['items']], [1])

    def test_refuses_when_nothing_complete_arrived(self):
        with self.assertRaises(extract.ExtractionError):
            extract.salvage('{"summary": "half a sum')

    def test_refuses_text_that_is_not_json(self):
        with self.assertRaises(extract.ExtractionError):
            extract.salvage('I decided not to review this.')


class ExtractTest(unittest.TestCase):
    def test_a_complete_response_is_not_marked_salvaged(self):
        data, salvaged = extract.extract(fenced(json.dumps(REVIEW)))
        self.assertEqual(data, REVIEW)
        self.assertFalse(salvaged)
        self.assertNotIn('caveat', data)

    def test_a_truncated_response_is_salvaged_and_labelled(self):
        text = json.dumps(REVIEW)
        response = ('Reviewing now.\n\n```json\n'
                    + text[:text.index('{"id": 2')])
        data, salvaged = extract.extract(response)
        self.assertTrue(salvaged)
        self.assertEqual([i['id'] for i in data['items']], [1])
        # Without this the partial review reads as a complete one.
        self.assertIn('caveat', data)
        self.assertIn('partial review', data['caveat'])

    def test_an_empty_response_is_an_error(self):
        with self.assertRaises(extract.ExtractionError):
            extract.extract('')

    def test_a_json_array_is_not_a_review(self):
        with self.assertRaises(extract.ExtractionError):
            extract.extract(fenced('[1, 2, 3]'))

    def test_prose_only_is_an_error(self):
        with self.assertRaises(extract.ExtractionError):
            extract.extract('The diff was too large for me to review.')


class SalvagedReviewIsUsableTest(unittest.TestCase):
    """A salvaged review still has to pass the rest of the pipeline."""

    def test_a_salvaged_review_validates_and_renders(self):
        render = load_script(
            'review-pr-with-claude/render-review.py', 'render_review')

        text = json.dumps(REVIEW)
        data, _ = extract.extract('```json\n' + text[:text.index('{"id": 2')])

        valid, error = render.validate_review(data)
        self.assertTrue(valid, error)

        markdown = render.render_markdown(data)
        self.assertIn('Incomplete review', markdown)
        self.assertIn('Adds a thing', markdown)


if __name__ == '__main__':
    unittest.main()
