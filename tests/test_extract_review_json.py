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

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

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


class CandidateBlocksTest(unittest.TestCase):
    def test_finds_a_fenced_block(self):
        blocks = extract.candidate_blocks(fenced('{"summary": "s"}'))
        self.assertEqual(blocks, ['{"summary": "s"}'])

    def test_ignores_prose_around_the_block(self):
        text = ('I read the diff.\n\n```json\n{"a": 1}\n```\n\n'
                'Hope that helps.\n')
        self.assertEqual(extract.candidate_blocks(text), ['{"a": 1}'])

    def test_an_unclosed_fence_yields_everything_after_it(self):
        # This is the truncation case: the response stopped before the
        # closing fence was written.
        text = 'Review:\n\n```json\n{"summary": "s", "items": [{"id": 1'
        self.assertEqual(
            extract.candidate_blocks(text),
            ['{"summary": "s", "items": [{"id": 1'])

    def test_falls_back_to_a_bare_object(self):
        text = 'No fences today. {"summary": "s", "items": []}'
        self.assertEqual(
            extract.candidate_blocks(text),
            ['{"summary": "s", "items": []}'])

    def test_a_brace_that_is_not_the_review_is_not_taken(self):
        # A JSON snippet quoted in prose has no summary near it, so it
        # is passed over rather than offered as the review.
        self.assertEqual(
            extract.candidate_blocks('Consider {"adequate": true} here.'),
            [])

    def test_returns_nothing_when_there_is_no_json_at_all(self):
        self.assertEqual(
            extract.candidate_blocks('I could not review this PR.'), [])

    def test_every_qualifying_brace_is_offered_in_order(self):
        # A snippet quoted in prose ahead of the review satisfies the
        # summary proximity test as well as the review does, so both
        # have to be offered or the review is lost behind the snippet.
        text = 'I saw {"a": 1} and the review is {"summary": "s"}'
        blocks = extract.candidate_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0].startswith('{"a": 1}'))
        self.assertEqual(blocks[1], '{"summary": "s"}')

    def test_the_brace_fallback_is_bounded(self):
        # Each candidate costs a parse and a salvage pass over the rest
        # of the response, so the fallback stops rather than letting a
        # pathological response charge quadratically for failing.
        text = '{"summary": "s"' * (extract.MAX_BRACE_CANDIDATES + 50)
        self.assertEqual(
            len(extract.candidate_blocks(text)),
            extract.MAX_BRACE_CANDIDATES)

    def test_blocks_are_offered_last_first(self):
        # The model's answer is the block it ended on, so that is the
        # one tried first; the earlier ones are the fallback.
        text = ('```json\n{"a": 1}\n```\n\n```json\n{"b": 2}\n```\n')
        self.assertEqual(
            extract.candidate_blocks(text), ['{"b": 2}', '{"a": 1}'])


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

    def test_refuses_a_prefix_that_parses_but_holds_no_items(self):
        # Truncation before the first item closes leaves a prefix that
        # parses -- summary plus a completed sub-object -- with no
        # items at all. Returning that sends a merely truncated
        # response down the schema validation failure path, which is
        # where genuine tooling bugs land, and reports it as one.
        text = ('{"summary": "s", "test_coverage": {"adequate": true}, '
                '"items": [{"id": 1, "title": "t')
        with self.assertRaises(extract.ExtractionError):
            extract.salvage(text)

    def test_refuses_a_prefix_cut_after_an_empty_items_array(self):
        # This one parses and would pass schema validation, so nothing
        # downstream would catch it: the pull request would be recorded
        # as reviewed, with a clean bill of health, off a response that
        # stopped before the first finding was written.
        text = ('{"summary": "s", "items": [], '
                '"positive_feedback": [{"title')
        with self.assertRaises(extract.ExtractionError):
            extract.salvage(text)

    def test_the_walk_back_is_bounded(self):
        # The walk starts at the end of the response and works back, so
        # a response made mostly of tiny objects can bury the
        # recoverable review under more cut points than are tried. That
        # bound is the only thing standing between a pathological
        # response and a parse of the whole text per object in it.
        text = (json.dumps(REVIEW) + ' '
                + '{"a": 1}' * (extract.MAX_SALVAGE_CANDIDATES + 100))
        with self.assertRaises(extract.ExtractionError):
            extract.salvage(text)


class ExtractTest(unittest.TestCase):
    def test_the_last_review_block_wins_over_an_earlier_one(self):
        # The prompt hands the model a fenced example with the shape of
        # a real review. A model that restates the format before
        # answering leaves two blocks, and the review is the second
        # one; taking the first would post the example's placeholder
        # text as a review, silently.
        example = {'summary': 'Brief summary', 'items': [item(1, 'Short title')]}
        text = ('Understood, the format is:\n\n```json\n'
                + json.dumps(example) + '\n```\n\nHere is the review.\n\n'
                '```json\n' + json.dumps(REVIEW) + '\n```\n')
        data, salvaged = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertFalse(salvaged)

    def test_a_truncated_last_block_wins_over_a_complete_earlier_one(self):
        # Cut off mid-block means the model was still writing its
        # answer, so the unfinished block is the answer and the earlier
        # complete one is the example. Salvaging half a review beats
        # posting a whole example.
        example = {'summary': 'Brief summary', 'items': [item(1, 'Short title')]}
        review_text = json.dumps(REVIEW)
        text = ('The format is:\n\n```json\n' + json.dumps(example)
                + '\n```\n\nReviewing now.\n\n```json\n'
                + review_text[:review_text.index('{"id": 2')])
        data, salvaged = extract.extract(text)
        self.assertTrue(salvaged)
        self.assertEqual(data['summary'], 'Adds a thing')

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

    def test_prose_only_is_not_reported_as_truncation(self):
        # No JSON block was opened, so nothing was cut off: the
        # reviewer or the prompt is wrong, and the shell turns that
        # into a red job rather than a comment about diff size.
        with self.assertRaises(extract.ExtractionError) as caught:
            extract.extract('The diff was too large for me to review.')
        self.assertNotIsInstance(caught.exception, extract.TruncatedError)
        self.assertEqual(caught.exception.exit_code, 1)

    def test_a_response_cut_before_any_item_completed_is_truncation(self):
        response = ('Reviewing now.\n\n```json\n{"summary": "s", '
                    '"test_coverage": {"adequate": true}, '
                    '"items": [{"id": 1, "title": "t')
        with self.assertRaises(extract.TruncatedError) as caught:
            extract.extract(response)
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(caught.exception.status, 'truncated_unusable')

    def test_a_review_with_no_findings_is_still_a_review(self):
        # A clean bill of health arrives complete and has to survive
        # the preference for a review that actually found something.
        clean = {'summary': 'Nothing to report', 'items': []}
        data, salvaged = extract.extract(fenced(json.dumps(clean)))
        self.assertEqual(data, clean)
        self.assertFalse(salvaged)

    def test_a_cut_after_empty_items_is_reported_as_truncation(self):
        # Salvaging this would post a clean bill of health and satisfy
        # the already-reviewed gate, so the pull request would end up
        # recorded as reviewed by a response that said nothing.
        response = ('```json\n{"summary": "s", "items": [], '
                    '"positive_feedback": [{"title')
        with self.assertRaises(extract.TruncatedError):
            extract.extract(response)

    def test_a_trailing_cut_off_fence_does_not_discard_a_review(self):
        # The model finished the review and then started writing
        # something else. The unfinished block is tried first, as
        # always, but yielding nothing has to fall through to the
        # complete review rather than end the search.
        text = (fenced(json.dumps(REVIEW))
                + '\nOne more thing.\n\n```json\n{"note": "abc')
        data, salvaged = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertFalse(salvaged)

    def test_a_prose_brace_does_not_hide_an_unfenced_review(self):
        # No fence at all, and a JSON snippet quoted ahead of the
        # review. Anchoring on the first brace makes the text
        # unclosable, which reports a review that did arrive as a
        # response cut off before it said anything -- green, with a
        # comment about diff size, for a review sitting right there.
        text = ('I saw {"a": 1} and the review is '
                + json.dumps(REVIEW))
        data, salvaged = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertFalse(salvaged)


class MainTest(unittest.TestCase):
    """The shell branches on main()'s exit code and its status line."""

    def run_main(self, response):
        """Run main() over a response, returning (exit code, stdout, path).

        The exit code is 0 when main() returned without calling
        sys.exit(), which is what the shell sees for a review it can
        go on to validate.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        response_path = os.path.join(directory.name, 'response.txt')
        output_path = os.path.join(directory.name, 'review.json')
        with open(response_path, 'w') as f:
            f.write(response)

        stdout = io.StringIO()
        argv = ['extract-review-json.py', response_path, output_path]
        code = 0
        with contextlib.redirect_stdout(stdout):
            with unittest.mock.patch.object(sys, 'argv', argv):
                try:
                    extract.main()
                except SystemExit as e:
                    code = e.code or 0

        return code, stdout.getvalue().strip(), output_path

    def test_a_complete_review_is_written_and_reported_ok(self):
        code, output, path = self.run_main(fenced(json.dumps(REVIEW)))
        self.assertEqual(code, 0)
        self.assertEqual(output, 'status=ok')
        with open(path) as f:
            self.assertEqual(json.load(f), REVIEW)

    def test_a_salvaged_review_is_written_with_its_caveat(self):
        text = json.dumps(REVIEW)
        code, output, path = self.run_main(
            'Reviewing.\n\n```json\n' + text[:text.index('{"id": 2')])
        self.assertEqual(code, 0)
        self.assertEqual(output, 'status=salvaged')
        with open(path) as f:
            written = json.load(f)
        self.assertEqual([i['id'] for i in written['items']], [1])
        self.assertIn('partial review', written['caveat'])

    def test_prose_only_exits_one_and_says_unparseable(self):
        code, output, path = self.run_main('I could not review this.')
        self.assertEqual(code, 1)
        self.assertTrue(output.startswith('status=unparseable '), output)
        self.assertFalse(os.path.exists(path))

    def test_an_early_cut_exits_two_and_says_truncated(self):
        code, output, path = self.run_main(
            '```json\n{"summary": "s", "items": [{"id": 1, "title": "t')
        self.assertEqual(code, 2)
        self.assertTrue(
            output.startswith('status=truncated_unusable '), output)
        self.assertFalse(os.path.exists(path))

    def test_wrong_argument_count_exits_one(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with unittest.mock.patch.object(
                    sys, 'argv', ['extract-review-json.py']):
                with self.assertRaises(SystemExit) as caught:
                    extract.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('Usage:', stdout.getvalue())


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
