#!/usr/bin/env python3
"""Render structured review JSON to human-readable markdown.

This script takes the JSON output from the automated reviewer and converts
it to a nicely formatted markdown comment suitable for posting on a PR.

Usage:
    render-review.py [--embed-json] <input.json> [output.md]
    render-review.py --validate <input.json>

Options:
    --embed-json    Include the raw JSON in a collapsed <details> section
                    at the end of the markdown, so the review can be
                    recovered from the posted comment by machine.
    --validate      Validate the JSON against the schema without rendering.

If output.md is not specified, writes to stdout.

Everything in the review but its headings is model output shaped by
pull request content, so @mentions in it are broken before it is
posted -- see break_mentions().
"""

import html
import json
import re
import sys
from pathlib import Path

# Try to import jsonschema for validation, but don't require it
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


SCHEMA_PATH = Path(__file__).parent / 'review-schema.json'

# How the fallback validator checks each JSON Schema type. bool is a
# subclass of int, and jsonschema does not count true as an integer, so
# it is excluded by hand; it does count 1.0, so that is admitted.
JSON_TYPES = {
    'object': lambda v: isinstance(v, dict),
    'array': lambda v: isinstance(v, list),
    'string': lambda v: isinstance(v, str),
    'integer': lambda v: ((isinstance(v, int) and not isinstance(v, bool))
                          or (isinstance(v, float) and v.is_integer())),
    'number': lambda v: (isinstance(v, (int, float))
                         and not isinstance(v, bool)),
    'boolean': lambda v: isinstance(v, bool),
    'null': lambda v: v is None,
}

# The keywords the fallback validator enforces. Annotations are listed
# so that they are known to be safe to ignore; anything else in the
# schema is a constraint the fallback would silently skip, which a test
# checks for rather than leaving the two validators to drift apart.
FALLBACK_KEYWORDS = ('type', 'required', 'enum', 'properties',
                     'additionalProperties', 'items')
ANNOTATION_KEYWORDS = ('$schema', '$id', 'title', 'description')

# The bot's mention, and the form render-unparsed-review.py breaks it
# into: a zero-width space after the @, which reads the same and links
# nothing.
BOT_MENTION = '@shakenfist-bot'
BROKEN_MENTION = '@\u200bshakenfist-bot'

# An HTML character reference. GitHub decodes these before it links
# mentions, so &#64;alice, &commat;alice and @&#97;lice all notify
# alice. A numeric one is matched without its semicolon too, as HTML
# decodes &#64alice even though CommonMark should not: breaking too
# much costs nothing. Its digits are taken whole, as a decoder takes
# them, so &#641 is never read as &#64 followed by a 1.
CHARACTER_REFERENCE = (r'&(?:#[0-9]+(?![0-9]);?|#[xX][0-9A-Fa-f]+(?![0-9A-Fa-f]);?'
                       r'|[A-Za-z][A-Za-z0-9]*;)')

# An @ GitHub would turn into a mention: one followed by a letter or
# digit, and not preceded by one, which would make it an email address.
# Either may be spelled as a character reference; which references are
# an @ is left to _break_mention(), and any reference after one is
# taken to be a letter, because breaking too much costs nothing.
MENTION = re.compile(rf'(?<![A-Za-z0-9])(@|{CHARACTER_REFERENCE})'
                     rf'(?=[A-Za-z0-9]|{CHARACTER_REFERENCE})')

SEVERITY_EMOJI = {
    'critical': '🔴',
    'high': '🟠',
    'medium': '🟡',
    'low': '🟢',
}

ACTION_LABEL = {
    'fix': 'FIX',
    'document': 'DOC',
    'consider': 'CONSIDER',
    'none': 'INFO',
}

CATEGORY_EMOJI = {
    'security': '🔒',
    'bug': '🐛',
    'performance': '⚡',
    'documentation': '📝',
    'style': '🎨',
    'testing': '🧪',
    'other': '📋',
}


def load_schema() -> dict | None:
    """Load the JSON schema for validation.

    Returns None when `review-schema.json` is not sitting beside this
    script, which `validate_review()` below treats as "valid". That is
    a bypass rather than weaker checking: a relocated script does not
    fail loudly, it silently stops validating, and every review passes
    including ones the schema would reject. Move the two files
    together; tests/test_render_review.py fails if they are apart.
    """
    if not SCHEMA_PATH.exists():
        return None
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def strip_nulls(value):
    """Recursively remove null-valued keys from dicts.

    The reviewer model sometimes emits an explicit null for an optional
    field it has nothing to say about (e.g. '"rationale": null') instead
    of omitting the key. JSON Schema treats those differently: a key
    that is optional may be absent, but if present it must match its
    declared type, so an explicit null fails a '"type": "string"' check.
    Throughout this pipeline null and absent mean the same thing, so
    normalise to absent before validating or rendering.
    """
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def _schema_errors(value, schema: dict, path: str):
    """Yield an error message for each way value fails schema.

    A stdlib reading of the subset of JSON Schema in FALLBACK_KEYWORDS,
    which is every constraint review-schema.json uses, so a review
    without jsonschema installed is held to the same schema as one with
    it -- read from the same file rather than restated here.
    """
    json_type = schema.get('type')
    if json_type is not None:
        types = json_type if isinstance(json_type, list) else [json_type]
        if not any(JSON_TYPES[t](value) for t in types):
            yield f'{path}: {value!r} is not of type {json_type!r}'
            # The other keywords assume the type, and a wrong one says
            # all there is to say.
            return

    if 'enum' in schema and value not in schema['enum']:
        yield f'{path}: {value!r} is not one of {schema["enum"]!r}'

    if isinstance(value, dict):
        for field in schema.get('required', []):
            if field not in value:
                yield f'{path}: {field!r} is a required property'
        for field, subschema in schema.get('properties', {}).items():
            if field in value:
                yield from _schema_errors(
                    value[field], subschema, f'{path}.{field}')
        # Only false is handled: a schema for the extra properties is
        # not something review-schema.json uses.
        if schema.get('additionalProperties') is False:
            for field in value:
                if field not in schema.get('properties', {}):
                    yield (f'{path}: additional property {field!r} '
                           'is not allowed')

    if isinstance(value, list) and 'items' in schema:
        for index, entry in enumerate(value):
            yield from _schema_errors(
                entry, schema['items'], f'{path}[{index}]')


def validate_review(review_data: dict) -> tuple[bool, str]:
    """Validate review data against the schema.

    Returns (is_valid, error_message). Without jsonschema the schema is
    checked by _schema_errors() instead, which enforces every keyword
    the schema uses.
    """
    schema = load_schema()
    if schema is None:
        # Fails open -- see load_schema() for why that is a bypass.
        return True, ''

    if not HAS_JSONSCHEMA:
        for error in _schema_errors(review_data, schema, 'review'):
            return False, error
        return True, ''

    try:
        jsonschema.validate(review_data, schema)
        return True, ''
    except jsonschema.ValidationError as e:
        return False, str(e.message)


def _break_mention(match: re.Match) -> str:
    if html.unescape(match.group(1)) != '@':
        return match.group(0)
    return match.group(1) + '\u200b'


def break_mentions(text: str) -> str:
    """Break the @mentions in text with a zero-width space.

    What the bot posts from a review is model output shaped by the pull
    request, and an @mention in it notifies whoever it names -- or, for
    a bot, can be read as a command. Breaking it keeps the text legible
    and links nobody.

    Every mention is broken, code included. GitHub does not link one
    inside a code span or fence, but telling what GitHub will render as
    code means parsing markdown as GitHub does: list items and block
    quotes end fences, a backslash or an HTML attribute stops a backtick
    opening a span, and an HTML block is no code at all. A reading that
    gets any of that wrong in the direction of code leaves a mention
    live, and the text being read is the model's, so the pull request
    can choose which reading to get wrong. The cost of breaking them all
    is a zero-width space in a decorator quoted in a code sample, which
    survives being copied out of it.

    The bot's own mention is broken first, into BROKEN_MENTION, the
    same form render-unparsed-review.py uses. The trigger workflows
    match their phrase with contains() anywhere in a comment body,
    ignoring case, so it is broken in any capitalisation and even where
    a letter before the @ means GitHub would not link it. The workflows
    see the comment body undecoded, so a character reference there
    fires nothing, and only needs breaking as a mention.
    """
    text = re.sub(re.escape(BOT_MENTION), BROKEN_MENTION, text,
                  flags=re.IGNORECASE)
    return MENTION.sub(_break_mention, text)


def render_markdown(review_data: dict, embed_json: bool = False) -> str:
    """Render review data to markdown format.

    Args:
        review_data: The review data dictionary.
        embed_json: If True, append the raw JSON in a collapsed details section
                    so the review can be parsed back out of the comment.
    """
    lines = []

    # Header
    lines.append('## PR Review')
    lines.append('')

    # Caveat, when the review is known to be incomplete. This goes
    # above the summary deliberately: a partial review read as a
    # complete one is worse than no review at all.
    caveat = review_data.get('caveat')
    if caveat:
        lines.append(f'> :warning: **Incomplete review.** {caveat}')
        lines.append('')

    # Summary
    lines.append('### Summary')
    lines.append('')
    lines.append(review_data.get('summary', 'No summary provided.'))
    lines.append('')

    # Separate items by action type
    fix_items = []
    doc_items = []
    consider_items = []
    info_items = []

    for item in review_data.get('items', []):
        action = item.get('action', 'none')
        if action == 'fix':
            fix_items.append(item)
        elif action == 'document':
            doc_items.append(item)
        elif action == 'consider':
            consider_items.append(item)
        else:
            info_items.append(item)

    # Action Items (things that must be done)
    if fix_items or doc_items:
        lines.append('---')
        lines.append('')
        lines.append('### Action Items')
        lines.append('')

        for item in fix_items + doc_items:
            lines.extend(render_item(item))
            lines.append('')

    # Suggestions (optional improvements)
    if consider_items:
        lines.append('---')
        lines.append('')
        lines.append('### Suggestions')
        lines.append('')
        lines.append('*These are optional improvements to consider:*')
        lines.append('')

        for item in consider_items:
            lines.extend(render_item(item))
            lines.append('')

    # Observations (informational only)
    if info_items:
        lines.append('---')
        lines.append('')
        lines.append('### Observations')
        lines.append('')
        lines.append('*These are informational and do not require action:*')
        lines.append('')

        for item in info_items:
            lines.extend(render_item(item))
            lines.append('')

    # Positive feedback
    positive = review_data.get('positive_feedback', [])
    if positive:
        lines.append('---')
        lines.append('')
        lines.append('### What\'s Good')
        lines.append('')

        for item in positive:
            lines.append(f"✅ **{item['title']}**")
            lines.append('')
            lines.append(item.get('description', ''))
            lines.append('')

    # Test coverage
    test_coverage = review_data.get('test_coverage')
    if test_coverage:
        lines.append('---')
        lines.append('')
        lines.append('### Test Coverage')
        lines.append('')

        if test_coverage.get('adequate'):
            lines.append('✅ Test coverage appears adequate.')
        else:
            lines.append('⚠️ Test coverage may need improvement.')

        missing = test_coverage.get('missing', [])
        if missing:
            lines.append('')
            lines.append('**Missing test scenarios:**')
            for scenario in missing:
                lines.append(f'- {scenario}')
        lines.append('')

    # Footer
    lines.append('---')
    lines.append('')
    lines.append('*🤖 This review was generated by the automated reviewer.*')

    # Optionally embed the JSON for machine parsing
    if embed_json:
        lines.append('')
        lines.append('<details>')
        lines.append('<summary>Machine-readable review data (for automation)'
                     '</summary>')
        lines.append('')
        lines.append('```json')
        lines.append(json.dumps(review_data, indent=2))
        lines.append('```')
        lines.append('')
        lines.append('</details>')

    # Over the whole comment rather than field by field, so that what
    # is code is judged in context: the location sits in a code span
    # only once it is rendered, and the embedded JSON inside a fence.
    return break_mentions('\n'.join(lines))


def render_item(item: dict) -> list[str]:
    """Render a single review item to markdown lines."""
    lines = []

    # Build title line with emoji and labels
    action = item.get('action', 'none')
    category = item.get('category', 'other')
    severity = item.get('severity')

    action_label = ACTION_LABEL.get(action, 'INFO')
    category_emoji = CATEGORY_EMOJI.get(category, '📋')

    title_parts = [f"**{item['id']}. [{action_label}]**"]

    if severity:
        severity_emoji = SEVERITY_EMOJI.get(severity, '')
        title_parts.append(f"{severity_emoji}")

    title_parts.append(f"{category_emoji} {item['title']}")

    lines.append(' '.join(title_parts))
    lines.append('')

    # Description
    if item.get('description'):
        lines.append(item['description'])
        lines.append('')

    # Location
    if item.get('location'):
        lines.append(f"📍 Location: `{item['location']}`")
        lines.append('')

    # Suggestion
    if item.get('suggestion'):
        lines.append(f"💡 **Suggestion:** {item['suggestion']}")
        lines.append('')

    # Rationale (for none/consider actions)
    if item.get('rationale'):
        lines.append(f"ℹ️ *{item['rationale']}*")
        lines.append('')

    return lines


def load_review(input_path: Path) -> dict:
    """Load review JSON, reporting a parse failure as an error.

    The input is a model's output rather than a file this repository
    wrote, and a truncated response reaches this script as JSON that
    stops mid-item. That is a known failure mode, so it exits with a
    message the CI log can be read from rather than a traceback.
    """
    try:
        with open(input_path) as f:
            return strip_nulls(json.load(f))
    except json.JSONDecodeError as e:
        print(f'Error: {input_path} is not valid JSON: {e}',
              file=sys.stderr)
        sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    # Parse arguments
    args = sys.argv[1:]
    embed_json = False

    if '--embed-json' in args:
        embed_json = True
        args.remove('--embed-json')

    # Handle --validate flag
    if args and args[0] == '--validate':
        if len(args) < 2:
            print('Error: --validate requires an input file')
            sys.exit(1)
        input_path = Path(args[1])
        data = load_review(input_path)
        is_valid, error = validate_review(data)
        if is_valid:
            print('Valid')
            sys.exit(0)
        else:
            print(f'Invalid: {error}')
            sys.exit(1)

    if not args:
        print(__doc__)
        sys.exit(1)

    input_path = Path(args[0])
    output_path = Path(args[1]) if len(args) > 1 else None

    # Load and validate
    data = load_review(input_path)

    is_valid, error = validate_review(data)
    if not is_valid:
        print(f'Error: Invalid review JSON: {error}', file=sys.stderr)
        sys.exit(1)

    # Render
    markdown = render_markdown(data, embed_json=embed_json)

    # Output
    if output_path:
        with open(output_path, 'w') as f:
            f.write(markdown)
    else:
        print(markdown)


if __name__ == '__main__':
    main()
