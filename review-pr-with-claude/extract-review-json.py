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

A response is only treated as truncated when it could actually have
been cut off: a fence left open, or an unfenced object running to the
end of the text. JSON that will not parse inside a fence that closed
is the reviewer emitting something invalid, which is a bug in the
prompt or the schema rather than a large diff, and is reported as such.

Exit codes:
    0 - A review was written to <review.json>
    1 - The response held no review at all, or held JSON that will not
        parse in a block that finished being written, which is the
        reviewer or the prompt being wrong rather than the response
        being large
    2 - A review block was there but stopped before anything usable
        arrived, so the response was cut off too early to salvage
"""

import json
import sys
from pathlib import Path


# How many candidate cut points to try parsing before giving up.
# Finding the cut points is one linear pass, but each candidate costs a
# ``json.loads()`` over the prefix before it, so the cost of trying
# them all is the number tried times the length of the response. A
# truncated review is cut inside its last item and salvages within a
# handful of candidates; this bounds what a pathological response --
# one made mostly of tiny nested objects -- can charge for failing.
MAX_SALVAGE_CANDIDATES = 2000

# How many bare-brace candidates to consider when the response carries
# no fence at all. Every brace that mentions "summary" nearby is tried,
# because a JSON snippet quoted in prose passes that test too, and each
# one costs a parse -- and, on failure, a salvage pass -- over the rest
# of the response. This bounds what a response made of nothing but
# "summary" mentions can charge for failing.
MAX_BRACE_CANDIDATES = 20

SALVAGE_CAVEAT = (
    'The reviewer ran out of output room part way through this review, '
    'so it was salvaged from the complete part of the response. '
    'Findings after the truncation point are missing -- treat this as a '
    'partial review, not a clean bill of health. Asking the bot for '
    'another review gets a fresh pass over the whole diff.')

CLOSERS = {'{': '}', '[': ']'}

# What every recovered item needs before the review is worth keeping.
# These mirror review-schema.json, which render-review.py validates
# against: a salvaged review that fails that validation is discarded
# whole, so the walk-back has to stop somewhere the validator will
# accept rather than merely somewhere the JSON parser will. Drift
# between the two is pinned by a test rather than by a shared import,
# because this script deliberately runs without jsonschema.
ITEM_REQUIRED_FIELDS = {
    'id': int,
    'title': str,
    'category': str,
    'action': str,
}
ITEM_ACTIONS = ('fix', 'document', 'consider', 'none')
ITEM_CATEGORIES = ('security', 'bug', 'performance', 'documentation',
                   'style', 'testing', 'other')


class ExtractionError(Exception):
    """No review could be recovered from the response."""

    status = 'unparseable'
    exit_code = 1


class TruncatedError(ExtractionError):
    """A review block was found, but it stopped before anything usable.

    Separate from its parent because the two mean different things to
    the caller: this one says the response ran out of room, which is a
    fact about the size of the pull request, while a bare
    ExtractionError says the response held no review at all, which is
    the reviewer or the prompt being wrong.
    """

    status = 'truncated_unusable'
    exit_code = 2


def _fenced_blocks(text):
    """Yield (contents, closed) for each ```json fenced block, in order.

    A fence that opens and never closes is the truncation case, so its
    contents run to the end of the response and nothing after it can be
    a block. Which of the two happened is reported rather than
    discarded: a fence that closed says the response finished writing
    what is inside it, so JSON in there that will not parse is
    malformed rather than unfinished.
    """
    lines = text.splitlines()

    index = 0
    while index < len(lines):
        if lines[index].strip().lower() not in ('```json', '``` json'):
            index += 1
            continue

        start = index + 1
        for end in range(start, len(lines)):
            if lines[end].strip() == '```':
                yield '\n'.join(lines[start:end]), True
                index = end + 1
                break
        else:
            # Opening fence with no closing fence: truncated mid-block.
            yield '\n'.join(lines[start:]), False
            return


def looks_like_a_review(data):
    """True when a parsed object could be a review.

    The schema requires a summary and an items array, so an object
    without both cannot become a posted review no matter what else is
    recovered alongside it.
    """
    return (isinstance(data, dict) and 'summary' in data
            and isinstance(data.get('items'), list))


def items_are_usable(items):
    """True when every recovered item will survive schema validation.

    What is checked is the required fields -- present, and of the type
    the schema declares -- and the two enums. An optional field of the
    wrong type still gets through and still fails validation
    downstream, which is deliberate: that is the model writing nonsense
    rather than a response stopping early, and walking back over it
    would drop good findings to hide a reviewer bug that should be
    seen.
    """
    for entry in items:
        if not isinstance(entry, dict):
            return False
        for field, field_type in ITEM_REQUIRED_FIELDS.items():
            if not isinstance(entry.get(field), field_type):
                return False
        if entry['action'] not in ITEM_ACTIONS:
            return False
        if entry['category'] not in ITEM_CATEGORIES:
            return False
    return True


def candidate_blocks(text):
    """Return the JSON candidates in a response, best guess first.

    Each candidate is a (block, truncatable) pair. ``truncatable`` says
    whether this block could have stopped mid-write: an unclosed fence
    could, and so could a brace-fallback candidate, where there is no
    fence to say where the object was meant to end. A closed fence
    could not, and that distinction decides whether unparseable
    contents are a large diff or a broken reviewer.

    Prefers fenced ```json blocks, last one first: the prompt hands the
    model a fenced example with the shape of a real review, so a model
    that restates the format before answering leaves the example first
    and its actual review last.

    Failing a fence, falls back to the braces that open an object going
    on to mention a summary. Every such brace is a candidate rather
    than only the first, because a JSON snippet quoted in prose ahead
    of the review passes the same test, and stopping there would report
    a review that did arrive as a response cut off before it said
    anything.

    Ordering here is positional, not "the first one that parses". A
    block cut off mid-write is the model's answer rather than a
    restated example, so it has to be offered before the earlier
    complete blocks; deciding between them is `extract()`'s job.
    """
    blocks = [(block, not closed)
              for block, closed in _fenced_blocks(text) if block.strip()]
    if blocks:
        return list(reversed(blocks))

    candidates = []
    for index, char in enumerate(text):
        if char != '{':
            continue
        if '"summary"' in text[index:index + 2000]:
            candidates.append((text[index:], True))
            if len(candidates) == MAX_BRACE_CANDIDATES:
                break

    return candidates


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
    """Recover a review from a truncated response.

    Returns the parsed object, or raises ExtractionError when no prefix
    of the text closes into something shaped like a review.

    Parsing is not enough on its own. Truncation before the first item
    closed leaves a prefix that parses -- ``summary`` and a completed
    ``test_coverage``, say -- with no ``items`` at all, and returning
    that would send a response that was merely cut off down the schema
    validation failure path, which is where genuine tooling bugs land.
    So keep walking back until the recovered object could actually be
    posted.

    That includes having found something. A cut after ``"items": []``
    recovers an object the schema accepts, and posting it would record
    the pull request as reviewed with a clean bill of health on the
    strength of a response that stopped before the first finding was
    written. A genuinely clean review arrives complete and never comes
    through here.

    It also includes every recovered item being one the schema will
    accept. Stopping the walk one item too early loses that finding;
    stopping it one item too late fails validation downstream, and a
    salvaged review that fails validation is thrown away in full -- so
    a single unusable trailing item costs every complete finding in
    front of it.
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
        if (looks_like_a_review(data) and data['items']
                and items_are_usable(data['items'])):
            return data

    raise ExtractionError('no complete JSON object could be recovered')


def extract(text):
    """Return (review_data, salvaged) for a model response.

    salvaged is True when the response was truncated and the review was
    recovered from the part that arrived intact.

    Candidates are tried in order and the first one that yields a
    review wins, so a truncated last block still beats a complete
    earlier one. A candidate that yields nothing is passed over rather
    than ending the search, so a stray trailing fence, or a JSON
    snippet quoted in prose, cannot discard a review that did arrive.

    Only a candidate that could have been cut off mid-write is salvaged
    and reported as truncation. A closed fence holding JSON that will
    not parse is the reviewer emitting something invalid, which is a
    prompt or schema problem rather than a size one, so it falls
    through to the unparseable path and the job goes red.
    """
    candidates = candidate_blocks(text)
    if not candidates:
        raise ExtractionError('no JSON block found in the response')

    truncated = False
    findingless = None

    for block, truncatable in candidates:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            if not truncatable:
                # The fence closed, so the response finished writing
                # this block; it is malformed, not unfinished. Calling
                # that truncation would blame the diff size for a
                # tooling bug, and hide it behind a green job.
                continue
            # This block was still being written when the response ran
            # out, so it is the answer if anything survives it.
            truncated = True
            try:
                data = salvage(block)
            except ExtractionError:
                continue
            data['caveat'] = SALVAGE_CAVEAT
            return data, True

        if not looks_like_a_review(data):
            continue
        if data['items']:
            return data, False
        # A review with no findings is a legitimate clean bill of
        # health, but it is also the shape of a restated example, so
        # keep looking before settling for it.
        if findingless is None:
            findingless = data

    if findingless is not None:
        return findingless, False

    if truncated:
        # A block was there and it did not parse, so the response was
        # cut off. It just stopped too early for anything to survive,
        # which is still a size problem rather than a broken reviewer.
        raise TruncatedError('no complete JSON object could be recovered')

    raise ExtractionError('no JSON block held anything shaped like a review')


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
        print(f'status={e.status} reason={e}')
        sys.exit(e.exit_code)

    output_path.write_text(json.dumps(data, indent=2))
    print('status=salvaged' if salvaged else 'status=ok')


if __name__ == '__main__':
    main()
