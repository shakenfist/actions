#!/usr/bin/env python3
"""Render a reviewer response that could not be parsed as a PR comment.

Usage:
    render-unparsed-review.py <response.txt> <comment.md> <reason>

When no review can be recovered from the model's response, the job goes
red, and it should: the reviewer or its prompt is broken. But the model
call has usually completed normally, and its findings are sitting in
the response -- issue #81 lost four correct ones, two of which changed
the code, and they survived only because somebody read the raw job log
of a red job. So the response is posted to the pull request as it came,
clearly marked as unparsed, rather than left in a log nobody opens.

The response is model output shaped by pull request content, so three
things are done to it before it is posted:

* It goes inside a code fence one backtick longer than any run of
  backticks in it, so nothing in it can close the fence and render as
  markdown of its own.
* Mentions of the bot are broken with a zero-width space, in any
  capitalisation, because the trigger workflows match their phrase with
  contains(), which ignores case, anywhere in a comment body. A review
  of a change to one of them is exactly the response that would quote
  the phrase. A comment posted with github.token cannot trigger a
  workflow, but a caller using a personal token can. The
  sf-reviewer-unavailable markers are broken the same way: they are
  found by an exact grep over the bot's comments, so a quoted one would
  stop a later run posting the explanation it stands for.
* It is cut to fit GitHub's 65,536 character limit on a comment body,
  saying so, since the full text is still in the step log.

The comment ends with MARKER so it can be found, by a human or later
tooling. Nothing deduplicates on it, deliberately: each forced
re-review that fails to parse is a new response with its own findings,
so it gets a comment of its own rather than replacing the last one.
"""

import re
import sys
from pathlib import Path


# GitHub rejects a comment body over 65,536 characters. The response is
# held well under that, leaving room for the explanation around it.
MAX_RESPONSE_CHARS = 60000

MARKER = '<!-- sf-reviewer-unparsed -->'

BOT_MENTION = '@shakenfist-bot'
BROKEN_MENTION = '@​shakenfist-bot'

MARKER_PREFIX = '<!-- sf-reviewer'
BROKEN_MARKER_PREFIX = '<!-- sf​-reviewer'


def fence_for(text):
    """Return a backtick fence longer than any backtick run in text."""
    runs = [len(run) for run in re.findall(r'`+', text)]
    return '`' * max(3, max(runs, default=0) + 1)


def render(response, reason):
    """Return the comment body for an unparsed response."""
    response = re.sub(re.escape(BOT_MENTION), BROKEN_MENTION, response,
                      flags=re.IGNORECASE)
    response = response.replace(MARKER_PREFIX, BROKEN_MARKER_PREFIX)

    truncated = len(response) > MAX_RESPONSE_CHARS
    if truncated:
        response = response[:MAX_RESPONSE_CHARS]

    fence = fence_for(response)
    lines = [
        '## :robot: Automated review could not be parsed',
        '',
        'The reviewer finished, but no structured review could be '
        f'recovered from what it wrote ({reason}). Its output is '
        'posted below as it came, so that any findings in it reach the '
        'pull request rather than only the job log.',
        '',
        'Nothing below has been checked against the review schema, no '
        'issues were filed for it, and it does not count as the bot '
        'having reviewed this pull request. The job is red because the '
        'reviewer or its prompt needs fixing, not because of anything '
        'in this pull request.',
        '',
    ]
    if truncated:
        lines += [
            f'> :warning: The output was cut to its first '
            f'{MAX_RESPONSE_CHARS:,} characters to fit in a comment. The '
            'full text is in the log of the job that posted this.',
            '',
        ]
    lines += [
        f'{fence}text',
        response.rstrip('\n'),
        fence,
        '',
        MARKER,
    ]
    return '\n'.join(lines) + '\n'


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    response_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    reason = sys.argv[3]

    output_path.write_text(render(response_path.read_text(), reason))


if __name__ == '__main__':
    main()
