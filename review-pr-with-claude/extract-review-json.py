#!/usr/bin/env python3
"""Extract the review JSON from the reviewer model's response.

Usage:
    extract-review-json.py <response.txt> <review.json>

The reviewer is asked for a fenced ```json block, and on a small diff
that is exactly what comes back. On a large one two things go wrong
often enough to be worth handling here rather than in the shell:

* The response is cut off mid-JSON, so the closing fence never
  arrives and ``json.loads()`` fails part way through an item.
* The fence markers are missing or reworded, so a naive
  fence-to-fence extraction finds nothing at all.

Both are recoverable. A JSON object that is truncated inside its
``items`` array still holds every finding before the cut, so this
script walks back to the last structurally complete point, closes the
open brackets, and keeps what survives. A salvaged review gets a
``caveat`` field describing what happened, which `render-review.py`
renders at the top of the posted comment so nobody mistakes a partial
review for a complete one.

Exit codes:
    0 - A review was written to <review.json>
    1 - Nothing usable could be extracted
"""

import json
import sys
from pathlib import Path


# How far back to walk looking for a structurally complete cut point.
# A truncated review is cut inside its last item, so the salvageable
# point is a few hundred characters back at most; the bound stops a
# pathological response from turning into a quadratic scan.
MAX_SALVAGE_CANDIDATES = 2000

SALVAGE_CAVEAT = (
    'The reviewer ran out of output room part way through this review, '
    'so it was salvaged from the complete part of the response. '
    'Findings after the truncation point are missing -- treat this as a '
    'partial review, not a clean bill of health.')

CLOSERS = {'{': '}', '[': ']'}


class ExtractionError(Exception):
    """No review could be recovered from the response."""


def find_json_block(text):
    """Return the most likely JSON text in a model response.

    Prefers a fenced ```json block. A fence that opens and never closes
    is the truncation case, so everything after the opening fence is
    returned for the salvage pass to deal with. Failing that, falls back
    to the first brace that starts an object mentioning "summary".
    """
    lines = text.splitlines()

    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() in ('```json', '``` json'):
            start = i + 1
            break

    if start is not None:
        for j in range(start, len(lines)):
            if lines[j].strip() == '```':
                return '\n'.join(lines[start:j])
        # Opening fence with no closing fence: truncated mid-block.
        return '\n'.join(lines[start:])

    # No fence at all. Take the first brace that opens an object which
    # goes on to mention a summary, which is enough to tell the review
    # apart from a JSON snippet quoted in prose.
    for index, char in enumerate(text):
        if char != '{':
            continue
        if '"summary"' in text[index:index + 2000]:
            return text[index:]

    return None


def _cut_points(text):
    """Yield (cut_index, open_stack) for each structurally safe cut.

    A safe cut is immediately after an object or array closed, outside
    any string; the stack at that point says which brackets still need
    closing. Cutting anywhere else -- after a comma, say -- would keep
    a half-written item, and an item missing its required fields fails
    schema validation and costs the whole review rather than the one
    finding that did not finish.
    """
    stack = []
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in CLOSERS:
            stack.append(char)
        elif char in ('}', ']'):
            if stack:
                stack.pop()
            yield index + 1, tuple(stack)


def salvage(text):
    """Recover a JSON object from a truncated response.

    Returns the parsed object, or raises ExtractionError when no prefix
    of the text closes into valid JSON.
    """
    candidates = list(_cut_points(text))[-MAX_SALVAGE_CANDIDATES:]

    for cut, stack in reversed(candidates):
        # An empty stack means this cut closed the outermost object,
        # so the only thing wrong was whatever followed it.
        closing = ''.join(CLOSERS[opener] for opener in reversed(stack))
        try:
            data = json.loads(text[:cut] + closing)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    raise ExtractionError('no complete JSON object could be recovered')


def extract(text):
    """Return (review_data, salvaged) for a model response.

    salvaged is True when the response was truncated and the review was
    recovered from the part that arrived intact.
    """
    block = find_json_block(text)
    if block is None or not block.strip():
        raise ExtractionError('no JSON block found in the response')

    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        data = salvage(block)
        data['caveat'] = SALVAGE_CAVEAT
        return data, True

    if not isinstance(data, dict):
        raise ExtractionError(
            f'JSON block is a {type(data).__name__}, not an object')

    return data, False


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    response_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])

    text = response_path.read_text()

    try:
        data, salvaged = extract(text)
    except ExtractionError as e:
        print(f'status=unparseable reason={e}')
        sys.exit(1)

    output_path.write_text(json.dumps(data, indent=2))
    print('status=salvaged' if salvaged else 'status=ok')


if __name__ == '__main__':
    main()
