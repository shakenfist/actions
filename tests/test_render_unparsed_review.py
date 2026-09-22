#!/usr/bin/env python3

"""Tests for review-pr-with-claude/render-unparsed-review.py.

This is what reaches the pull request when no review could be recovered
from a completed response, so what is pinned is that the response
arrives intact, cannot escape its fence, cannot fire a bot trigger, and
fits in a comment.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
import unittest.mock

from tests.helpers import load_script


unparsed = load_script(
    'review-pr-with-claude/render-unparsed-review.py',
    'render_unparsed_review')


class FenceTest(unittest.TestCase):
    def test_plain_text_gets_three_backticks(self):
        self.assertEqual(unparsed.fence_for('no ticks here'), '```')

    def test_the_fence_outruns_any_run_in_the_text(self):
        # A response holding its own ```json fence must not close ours.
        self.assertEqual(unparsed.fence_for('a ```json b ````'), '`````')


class RenderTest(unittest.TestCase):
    RESPONSE = 'Findings:\n\n```json\n{"summary": "s", "items": [\n```\n'

    def test_the_response_is_posted_whole(self):
        body = unparsed.render(self.RESPONSE, 'status=unparseable')
        self.assertIn(self.RESPONSE.rstrip('\n'), body)

    def test_it_is_marked_as_unparsed(self):
        body = unparsed.render(self.RESPONSE, 'status=unparseable')
        self.assertIn('could not be parsed', body)
        self.assertIn('status=unparseable', body)
        self.assertIn(unparsed.MARKER, body)

    def test_the_response_sits_inside_a_longer_fence(self):
        body = unparsed.render(self.RESPONSE, 'r')
        self.assertIn('\n````text\n', body)
        self.assertIn('\n````\n', body)

    def test_bot_mentions_are_broken(self):
        # A review of a change to a trigger workflow is the response
        # most likely to quote the trigger phrase.
        body = unparsed.render(
            'the phrase is ' + unparsed.BOT_MENTION + ' please retest', 'r')
        self.assertNotIn(unparsed.BOT_MENTION, body)
        self.assertIn(unparsed.BROKEN_MENTION, body)

    def test_the_explanation_carries_no_bot_mention(self):
        self.assertNotIn(unparsed.BOT_MENTION, unparsed.render('x', 'r'))

    def test_a_short_response_is_not_marked_cut(self):
        self.assertNotIn('was cut', unparsed.render('x', 'r'))

    def test_a_long_response_is_cut_to_fit_a_comment(self):
        body = unparsed.render('x' * 100000, 'r')
        self.assertLess(len(body), 65536)
        self.assertIn('was cut', body)
        self.assertIn('x' * unparsed.MAX_RESPONSE_CHARS, body)


class MainTest(unittest.TestCase):
    def test_the_comment_is_written(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        response_path = os.path.join(directory.name, 'response.txt')
        output_path = os.path.join(directory.name, 'comment.md')
        with open(response_path, 'w') as f:
            f.write('a finding')

        argv = ['render-unparsed-review.py', response_path, output_path,
                'status=unparseable']
        with unittest.mock.patch.object(sys, 'argv', argv):
            unparsed.main()

        with open(output_path) as f:
            self.assertIn('a finding', f.read())

    def test_wrong_argument_count_exits_one(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with unittest.mock.patch.object(
                    sys, 'argv', ['render-unparsed-review.py']):
                with self.assertRaises(SystemExit) as caught:
                    unparsed.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('Usage:', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
