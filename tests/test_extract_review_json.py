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


def cut_after_last_item(review):
    """The review as a response cut off just after its last item closed.

    Every item arrived complete, but the items array and the review
    around it did not, so this is truncation rather than a whole review.
    """
    text = json.dumps(review)
    return text[:text.rindex(']')]


def fenced(payload):
    return f'Here is the review.\n\n```json\n{payload}\n```\n'


REVIEW = {
    'summary': 'Adds a thing',
    'items': [item(1), item(2, 'Another finding')],
}


class CandidateBlocksTest(unittest.TestCase):
    def test_finds_a_fenced_block(self):
        blocks = extract.candidate_blocks(fenced('{"summary": "s"}'))
        self.assertEqual(blocks, [('{"summary": "s"}', False)])

    def test_ignores_prose_around_the_block(self):
        text = ('I read the diff.\n\n```json\n{"a": 1}\n```\n\n'
                'Hope that helps.\n')
        self.assertEqual(
            extract.candidate_blocks(text), [('{"a": 1}', False)])

    def test_an_unclosed_fence_yields_everything_after_it(self):
        # This is the truncation case: the response stopped before the
        # closing fence was written, so the block is marked as one that
        # could have been cut off mid-write.
        text = 'Review:\n\n```json\n{"summary": "s", "items": [{"id": 1'
        self.assertEqual(
            extract.candidate_blocks(text),
            [('{"summary": "s", "items": [{"id": 1', True)])

    def test_falls_back_to_a_bare_object(self):
        # No fence said where the object was meant to end, so it is
        # offered as one that could have been cut off.
        text = 'No fences today. {"summary": "s", "items": []}'
        self.assertEqual(
            extract.candidate_blocks(text),
            [('{"summary": "s", "items": []}', True)])

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
        self.assertTrue(blocks[0][0].startswith('{"a": 1}'))
        self.assertEqual(blocks[1][0], '{"summary": "s"}')

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
            extract.candidate_blocks(text),
            [('{"b": 2}', False), ('{"a": 1}', False)])

    def test_a_closed_fence_is_not_offered_as_truncatable(self):
        # The whole malformed-versus-unfinished decision rests on this
        # flag, and getting it backwards turns a broken reviewer into a
        # green job blaming the size of the diff.
        blocks = extract.candidate_blocks(fenced('{"summary": "s",'))
        self.assertEqual(blocks, [('{"summary": "s",', False)])


class SalvageTest(unittest.TestCase):
    def test_recovers_items_completed_before_the_cut(self):
        text = json.dumps(REVIEW)
        truncated = text[:text.index('"Another finding"')] + '"Anoth'
        data, whole = extract.salvage(truncated)
        self.assertEqual(data['summary'], 'Adds a thing')
        self.assertEqual([i['id'] for i in data['items']], [1])
        # Brackets had to be closed to get here, so this is partial.
        self.assertFalse(whole)

    def test_a_brace_inside_a_string_is_not_a_cut_point(self):
        # A description mentioning a brace must not be mistaken for
        # structure when walking back to a safe cut point.
        review = {
            'summary': 's',
            'items': [dict(item(1), description='look at }] here'),
                      item(2)],
        }
        text = json.dumps(review)
        data, _ = extract.salvage(text[:text.index('{"id": 2')])
        self.assertEqual([i['id'] for i in data['items']], [1])
        self.assertIn('}]', data['items'][0]['description'])

    def test_an_item_cut_before_it_closed_is_dropped_entirely(self):
        # Half an item validates as nothing: the schema wants id,
        # title, category and action on every one of them. Better to
        # lose the unfinished finding than the whole review.
        text = json.dumps(REVIEW)
        data, _ = extract.salvage(text[:text.index('"Another finding"')])
        self.assertEqual([i['id'] for i in data['items']], [1])

    def test_trailing_prose_after_a_complete_object_is_dropped(self):
        # And reported whole: nothing had to be closed, so nothing is
        # missing from what was recovered.
        text = json.dumps(REVIEW) + '\n\nHope that helps!'
        self.assertEqual(extract.salvage(text), (REVIEW, True))

    def test_a_bracket_in_trailing_prose_does_not_hide_the_whole_object(self):
        # A closer in the prose is a later cut point with an empty
        # stack too, but it does not parse, so the walk goes on back to
        # the one that closed the review.
        text = json.dumps(REVIEW) + '\n\nSee [1] and {this}.'
        self.assertEqual(extract.salvage(text), (REVIEW, True))

    def test_a_whole_review_is_not_walked_back_over_an_unusable_item(self):
        # The review closed, so the model finished writing that item.
        # Dropping it and calling the rest partial would blame
        # truncation for a reviewer bug; returning it whole lets
        # validation reject it, as it would in a closed fence.
        unusable = {'id': 2, 'title': 'No action', 'category': 'bug'}
        review = {'summary': 's', 'items': [item(1), unusable]}
        text = json.dumps(review) + '\n\nHope that helps.'
        self.assertEqual(extract.salvage(text), (review, True))

    def test_recovers_when_the_cut_is_at_a_trailing_comma(self):
        text = json.dumps(REVIEW)
        truncated = text[:text.index('{"id": 2')]
        data, _ = extract.salvage(truncated)
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

    def test_a_whole_review_with_no_findings_is_recovered(self):
        # The outer object closed, so nothing was cut off and a clean
        # review is what arrived.
        clean = {'summary': 'LGTM', 'items': []}
        data, whole = extract.salvage(json.dumps(clean) + ' Hope that helps.')
        self.assertEqual(data, clean)
        self.assertTrue(whole)

    def test_an_item_the_schema_will_reject_is_walked_past(self):
        # The trailing item completed as JSON but has no action, so
        # validation downstream would reject the whole review -- and a
        # salvaged review that fails validation is discarded in full,
        # taking the complete finding in front of it with it.
        unusable = {'id': 2, 'title': 'Half a finding', 'category': 'bug'}
        review = {'summary': 's', 'items': [item(1), unusable]}
        data, _ = extract.salvage(cut_after_last_item(review))
        self.assertEqual([i['id'] for i in data['items']], [1])

    def test_an_item_whose_required_field_has_the_wrong_type(self):
        # Present is not the same as usable: the schema wants an
        # integer id, and a string one fails validation exactly as
        # hard as a missing one -- taking the whole salvaged review
        # with it.
        unusable = dict(item(2), id='two')
        review = {'summary': 's', 'items': [item(1), unusable]}
        data, _ = extract.salvage(cut_after_last_item(review))
        self.assertEqual([i['id'] for i in data['items']], [1])

    def test_an_item_with_an_unknown_action_is_walked_past(self):
        # Present but not one of the four the schema allows, which is a
        # different failure from the field being missing: the model
        # invents vocabulary as readily as it truncates.
        unusable = dict(item(2), action='escalate')
        review = {'summary': 's', 'items': [item(1), unusable]}
        data, _ = extract.salvage(cut_after_last_item(review))
        self.assertEqual([i['id'] for i in data['items']], [1])

    def test_an_item_with_an_unknown_category_is_walked_past(self):
        # Same cost, reached through the other enum the model can get
        # wrong: category is checked against the schema's list, not
        # merely for being present.
        unusable = dict(item(2), category='architecture')
        review = {'summary': 's', 'items': [item(1), unusable]}
        data, _ = extract.salvage(cut_after_last_item(review))
        self.assertEqual([i['id'] for i in data['items']], [1])

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
        data, status = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertEqual(status, 'ok')

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
        data, status = extract.extract(text)
        self.assertEqual(status, 'salvaged')
        self.assertEqual(data['summary'], 'Adds a thing')

    def test_a_complete_response_is_not_marked_salvaged(self):
        data, status = extract.extract(fenced(json.dumps(REVIEW)))
        self.assertEqual(data, REVIEW)
        self.assertEqual(status, 'ok')
        self.assertNotIn('caveat', data)

    def test_a_truncated_response_is_salvaged_and_labelled(self):
        text = json.dumps(REVIEW)
        response = ('Reviewing now.\n\n```json\n'
                    + text[:text.index('{"id": 2')])
        data, status = extract.extract(response)
        self.assertEqual(status, 'salvaged')
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
        data, status = extract.extract(fenced(json.dumps(clean)))
        self.assertEqual(data, clean)
        self.assertEqual(status, 'ok')

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
        data, status = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertEqual(status, 'ok')

    def test_a_closed_fence_of_malformed_json_is_not_truncation(self):
        # The fence closed, so nothing was cut off: the reviewer emitted
        # invalid JSON. Salvaging it would post the completed findings
        # under a banner saying output room ran out, which is false, and
        # would tell the reader findings are missing when none are.
        text = json.dumps(REVIEW).replace('}]', '}, ]')
        with self.assertRaises(extract.ExtractionError) as caught:
            extract.extract(fenced(text))
        self.assertNotIsInstance(caught.exception, extract.TruncatedError)
        self.assertEqual(caught.exception.exit_code, 1)

    def test_a_closed_fence_of_garbage_goes_red_rather_than_green(self):
        # Nothing salvageable either, which is the case that used to
        # exit 2: a green job, and a comment on the pull request
        # blaming the diff size for what is a prompt or schema
        # regression -- the opaque failure this handling exists to
        # remove, just relocated.
        with self.assertRaises(extract.ExtractionError) as caught:
            extract.extract(fenced('{"summary": "s", "items": [ <<< ]'))
        self.assertNotIsInstance(caught.exception, extract.TruncatedError)
        self.assertEqual(caught.exception.exit_code, 1)

    def test_a_malformed_closed_fence_does_not_hide_a_later_review(self):
        # Blocks are tried last first, so a malformed final block has
        # to be passed over rather than end the search.
        text = (fenced(json.dumps(REVIEW))
                + '\nOn reflection:\n\n```json\n{"summary": "s", }\n```\n')
        data, status = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertEqual(status, 'ok')

    def assert_complete(self, response, expected=REVIEW, status='ok'):
        data, got = extract.extract(response)
        self.assertEqual(got, status)
        self.assertEqual(data, expected)
        # The caveat is what tells a reader findings are missing, and
        # none are.
        self.assertNotIn('caveat', data)

    def test_an_unfenced_review_followed_by_prose_is_complete(self):
        self.assert_complete(
            'Here it is: ' + json.dumps(REVIEW, indent=2)
            + '\n\nLet me know if you want more detail.')

    def test_a_closing_fence_on_the_brace_line_is_complete(self):
        # The line holding the closing fence is not a bare fence, so
        # the block reads as one that never closed.
        self.assert_complete(
            '```json\n' + json.dumps(REVIEW, indent=2) + '```\n')

    def test_a_tilde_fence_is_complete(self):
        # Not a fence this script looks for, so the review is found by
        # the brace fallback with the closing fence still after it.
        self.assert_complete(
            '~~~json\n' + json.dumps(REVIEW, indent=2) + '\n~~~\n')

    def test_an_unclosed_fence_around_a_complete_object_is_complete(self):
        self.assert_complete(
            '```json\n' + json.dumps(REVIEW, indent=2)
            + '\n\nThat is everything I found.\n')

    def test_a_whole_review_needing_strict_false_is_repaired(self):
        # A raw newline in a string is what a closed fence reports as
        # repaired, so the same object followed by prose says the same.
        text = json.dumps(REVIEW).replace('Adds a thing', 'Adds\na thing')
        expected = dict(REVIEW, summary='Adds\na thing')
        self.assert_complete(text + '\n\nDone.', expected, 'repaired')

    def test_a_whole_review_needing_repair_is_repaired(self):
        # A fence that never closed, around a complete object with a
        # stray quote in it and nothing after it.
        data, status = extract.extract('```json\n' + RepairTest.QUOTED)
        self.assertEqual(status, 'repaired')
        self.assertEqual(len(data['items']), 1)
        self.assertNotIn('caveat', data)

    def test_a_prose_brace_does_not_hide_an_unfenced_review(self):
        # No fence at all, and a JSON snippet quoted ahead of the
        # review. Anchoring on the first brace makes the text
        # unclosable, which reports a review that did arrive as a
        # response cut off before it said anything -- green, with a
        # comment about diff size, for a review sitting right there.
        text = ('I saw {"a": 1} and the review is '
                + json.dumps(REVIEW))
        data, status = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertEqual(status, 'ok')

    CLEAN = {'summary': 'LGTM', 'items': []}

    def test_a_whole_clean_review_followed_by_prose_is_complete(self):
        # Rejecting it for having no findings reported a clean review
        # as one cut off before it said anything.
        self.assert_complete(json.dumps(self.CLEAN) + '\n\nHope that helps.',
                             self.CLEAN)

    def test_a_whole_clean_review_in_an_unclosed_fence_is_complete(self):
        self.assert_complete('```json\n' + json.dumps(self.CLEAN)
                             + '\n\nNothing else.\n', self.CLEAN)

    def test_a_whole_clean_review_needing_strict_false_is_repaired(self):
        text = json.dumps(self.CLEAN).replace('LGTM', 'LG\nTM')
        self.assert_complete(text + '\n\nDone.',
                             dict(self.CLEAN, summary='LG\nTM'), 'repaired')

    def test_a_clean_example_does_not_hide_a_truncated_review(self):
        # Unfenced, the first candidate runs on past the example to the
        # end of the response, and the example is whole inside it. The
        # review after it was cut off, and saying it was clean would
        # record the pull request as reviewed off a response that never
        # got to its first finding.
        text = ('The format is ' + json.dumps(self.CLEAN)
                + ' and here it is: {"summary": "s", "items": '
                '[{"id": 1, "title": "t')
        with self.assertRaises(extract.TruncatedError):
            extract.extract(text)

    def test_a_clean_example_does_not_hide_a_later_review(self):
        text = ('The format is ' + json.dumps(self.CLEAN)
                + ' and here it is: ' + json.dumps(REVIEW))
        data, status = extract.extract(text)
        self.assertEqual(data, REVIEW)
        self.assertEqual(status, 'ok')

    def test_a_whole_invalid_review_is_returned_for_validation_to_reject(self):
        # Not walked back to the usable item in front: the review
        # arrived whole, so the item the schema rejects goes to
        # validation with it, and the job goes red.
        review = {'summary': 's',
                  'items': [item(1), dict(item(2), action='escalate')]}
        data, status = extract.extract(json.dumps(review) + '\n\nDone.')
        self.assertEqual(data, review)
        self.assertEqual(status, 'ok')

    # The prompt's example, whose enums are placeholders: whole, shaped
    # like a review, and holding an item the schema rejects.
    PLACEHOLDER = {'summary': 'Brief summary', 'items': [dict(
        item(1, 'Short title'),
        category='security|bug|performance|documentation|style|testing|other',
        action='fix|document|consider|none')]}

    def test_a_restated_example_does_not_hide_a_later_review(self):
        text = ('The format is ' + json.dumps(self.PLACEHOLDER)
                + ' and here it is: ' + json.dumps(REVIEW))
        self.assertEqual(extract.extract(text), (REVIEW, 'ok'))

    def test_a_restated_example_does_not_hide_a_clean_review(self):
        text = ('The format is ' + json.dumps(self.PLACEHOLDER)
                + ' and here it is: ' + json.dumps(self.CLEAN))
        self.assertEqual(extract.extract(text), (self.CLEAN, 'ok'))

    def test_a_restated_example_does_not_hide_a_truncated_review(self):
        text = ('The format is ' + json.dumps(self.PLACEHOLDER)
                + ' and here it is: {"summary": "s", "items": '
                '[{"id": 1, "title": "t')
        with self.assertRaises(extract.TruncatedError):
            extract.extract(text)


class RepairTest(unittest.TestCase):
    """A complete review with a stray quote in it is still a review.

    This is the case in issue #40: a description discussing
    ``packages = ["shakenfist"]`` wrote the inner quotes unescaped, and
    ten findings were thrown away over two characters.
    """

    QUOTED = ('{"summary": "s", "items": [{"id": 1, "title": "t", '
              '"category": "bug", "action": "fix", "severity": "low", '
              '"description": "`packages = ["shakenfist"]` lists only the '
              'top-level package"}]}')

    def test_quotes_followed_by_prose_are_escaped(self):
        data = json.loads(extract.repair(self.QUOTED))
        self.assertEqual(
            data['items'][0]['description'],
            '`packages = ["shakenfist"]` lists only the top-level package')

    def test_valid_json_is_left_alone(self):
        text = json.dumps(REVIEW)
        self.assertEqual(extract.repair(text), text)

    def test_escaped_quotes_are_left_alone(self):
        text = json.dumps({'summary': 'say "hi"', 'items': []})
        self.assertEqual(extract.repair(text), text)

    def test_a_quote_before_a_comma_and_prose_is_escaped(self):
        # A comma alone is not enough to end a string: what comes after
        # it has to be able to start the next value.
        text = '{"summary": "the "a", then the b", "items": []}'
        self.assertEqual(
            json.loads(extract.repair(text))['summary'],
            'the "a", then the b')

    def test_a_closed_fence_with_a_stray_quote_is_repaired(self):
        data, status = extract.extract(fenced(self.QUOTED))
        self.assertEqual(status, 'repaired')
        self.assertEqual(len(data['items']), 1)
        # Nothing is missing from a repaired review, so it must not be
        # labelled partial.
        self.assertNotIn('caveat', data)

    def test_a_raw_newline_inside_a_string_is_repaired(self):
        # Substituted into the dumped text, so the newline is a literal
        # one inside the JSON string rather than an escaped one.
        text = json.dumps(REVIEW).replace('Adds a thing', 'Adds\na thing')
        data, status = extract.extract(fenced(text))
        self.assertEqual(status, 'repaired')
        self.assertEqual(data['summary'], 'Adds\na thing')

    def test_a_repair_that_finds_no_items_is_refused(self):
        # A repaired empty review would record the pull request as
        # reviewed and clean on the strength of a guess.
        text = '{"summary": "the "a" thing", "items": []}'
        with self.assertRaises(extract.ExtractionError):
            extract.extract(fenced(text))

    def test_a_repaired_review_validates(self):
        render = load_script(
            'review-pr-with-claude/render-review.py', 'render_review')
        data, _ = extract.extract(fenced(self.QUOTED))
        valid, message = render.validate_review(data)
        self.assertTrue(valid, message)

    def test_a_multi_element_literal_is_refused_rather_than_mangled(self):
        # The quote after "a" is followed by a comma and another string,
        # so it reads as a string ending. The guess is wrong, and what
        # it produces must fail to parse rather than parse into a
        # review with its strings cut in the wrong places.
        text = self.QUOTED.replace('["shakenfist"]', '["a", "b"]')
        with self.assertRaises(extract.ExtractionError):
            extract.extract(fenced(text))

    def _truncated_and_quoted(self):
        items = [dict(item(1), description='uses QxQ here'),
                 dict(item(2), description='also QyQ'),
                 dict(item(3), description='a third, cut off')]
        text = json.dumps({'summary': 's', 'items': items}).replace('Q', '"')
        return '```json\n' + text[:text.index('cut off')]

    def test_a_truncated_block_with_a_stray_quote_is_salvaged(self):
        # An unescaped quote throws salvage's string tracking out of
        # step, so without the repair this cuts in the wrong places,
        # finds nothing, and reports two complete findings as a
        # response cut off before it said anything.
        data, status = extract.extract(self._truncated_and_quoted())
        self.assertEqual(status, 'salvaged')
        self.assertEqual(
            [i['description'] for i in data['items']],
            ['uses "x" here', 'also "y"'])
        self.assertEqual(data['caveat'], extract.SALVAGE_CAVEAT)

    def test_a_salvaged_repair_validates(self):
        render = load_script(
            'review-pr-with-claude/render-review.py', 'render_review')
        data, _ = extract.extract(self._truncated_and_quoted())
        valid, message = render.validate_review(data)
        self.assertTrue(valid, message)

    def test_a_truncated_block_with_a_raw_newline_is_salvaged(self):
        text = json.dumps(REVIEW).replace('Adds a thing', 'Adds\na thing')
        data, status = extract.extract(
            '```json\n' + text[:text.index('Another finding')])
        self.assertEqual(status, 'salvaged')
        self.assertEqual(data['summary'], 'Adds\na thing')
        self.assertEqual(len(data['items']), 1)


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

    def test_a_repaired_review_is_written_and_reported_repaired(self):
        code, output, path = self.run_main(fenced(RepairTest.QUOTED))
        self.assertEqual(code, 0)
        self.assertEqual(output, 'status=repaired')
        with open(path) as f:
            self.assertEqual(len(json.load(f)['items']), 1)

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


class ItemAcceptanceMatchesTheSchemaTest(unittest.TestCase):
    """The salvage predicate has to agree with what validation wants.

    salvage() stops walking back at the first cut whose items would
    survive render-review.py's validation. If this script's idea of
    that drifts from review-schema.json, the walk stops either too
    early -- losing findings that would have posted -- or too late,
    which discards the whole salvaged review. So the rules are read
    from the schema rather than copied from it, and what is pinned here
    is that reading.
    """

    def schema(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'review-pr-with-claude', 'review-schema.json')
        with open(path) as f:
            return json.load(f)

    def test_the_rules_are_read_from_the_shipped_schema(self):
        self.assertEqual(extract.ITEM_RULES, extract.item_rules(self.schema()))
        self.assertEqual(list(extract.REVIEW_REQUIRED_FIELDS),
                         self.schema()['required'])

    def test_types_are_judged_as_the_fallback_validator_judges_them(self):
        # Both scripts carry a JSON_TYPES table; if they part ways, the
        # walk-back stops somewhere the validator will not accept.
        render = load_script(
            'review-pr-with-claude/render-review.py', 'render_review')
        self.assertEqual(extract.JSON_TYPES.keys(), render.JSON_TYPES.keys())
        for value in (None, True, False, 0, 1, 1.5, '', 'x', [], {}):
            for name, check in extract.JSON_TYPES.items():
                self.assertEqual(check(value), render.JSON_TYPES[name](value),
                                 (name, value))

    def test_the_schema_sits_beside_the_script(self):
        # It is read when the script loads, so without it every review
        # fails as unparseable -- red, with a traceback naming the file
        # rather than anything about the response.
        self.assertTrue(extract.SCHEMA_PATH.exists(), extract.SCHEMA_PATH)

    def test_the_rules_follow_whatever_the_schema_says(self):
        schema = {'properties': {'items': {'items': {
            'required': ['name', 'kind', 'free'],
            'properties': {
                'name': {'type': 'string'},
                'kind': {'type': 'string', 'enum': ['a', 'b']},
                'other': {'type': 'integer'},
            },
        }}}}
        self.assertEqual(extract.item_rules(schema), {
            'name': ('string', None),
            'kind': ('string', ['a', 'b']),
            'free': (None, None),
        })

    def test_items_are_judged_by_the_rules_not_a_copy(self):
        # Widen the schema's category enum, and an item using the new
        # value has to become usable without anything else changing.
        schema = self.schema()
        item_schema = schema['properties']['items']['items']
        item_schema['properties']['category']['enum'].append('architecture')
        candidate = dict(item(1), category='architecture')
        self.assertFalse(extract.items_are_usable([candidate]))
        with unittest.mock.patch.object(
                extract, 'ITEM_RULES', extract.item_rules(schema)):
            self.assertTrue(extract.items_are_usable([candidate]))

    def test_usable_items_are_exactly_the_ones_that_validate(self):
        # Every way an item can differ in what it requires, judged by
        # both this script and the validator downstream. Optional
        # fields are left alone: items_are_usable() lets a bad one
        # through on purpose.
        render = load_script(
            'review-pr-with-claude/render-review.py', 'render_review')
        candidates = [item(1), 'not an object', dict(item(1), id='one'),
                      dict(item(1), id=True), dict(item(1), id=1.5),
                      dict(item(1), title=7), dict(item(1), action='escalate'),
                      dict(item(1), category='architecture')]
        for field in item(1):
            candidates.append(
                {k: v for k, v in item(1).items() if k != field})
        for candidate in candidates:
            valid, error = render.validate_review(
                {'summary': 's', 'items': [candidate]})
            self.assertEqual(
                extract.items_are_usable([candidate]), valid,
                f'{candidate!r}: {error}')


if __name__ == '__main__':
    unittest.main()
