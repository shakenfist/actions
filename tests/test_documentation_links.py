#!/usr/bin/env python3

"""Tests that documentation only links to files which exist.

The documentation was reshuffled once already -- README.md went from a
manual back to a pitch and its content moved into docs/ -- and every
link pointing into the moved sections had to be rewritten by hand.
Nothing in CI would have caught one of them being missed, which is the
same argument test_workflow_references.py makes about dispatch targets:
a stale link fails at the moment somebody follows it and not before.

Three rules are in force, and they are not a style preference -- each
one is a separate consistency audit in shakenfist/development that
files an issue against this repository when it is broken:

* every relative link must resolve on disk (this file's own rule);
* the top-level README.md must use absolute URLs, because it is
  rendered off the repository landing page -- on PyPI and in README
  mirrors -- where a relative target resolves against the wrong base
  and silently 404s (`readme-absolute-links`);
* a relative link inside docs/ must stay inside docs/, and anything
  pointing above it must be absolute (`docs-external-links`). This is
  the counter-intuitive one: docs/ is synchronised into
  shakenfist/shakenfist under docs/components/<repo>/ and published on
  shakenfist.com, where the tree above docs/ does not exist, so
  `../ARCHITECTURE.md` resolves to docs/components/ARCHITECTURE.md and
  404s -- while rendering perfectly on GitHub, which is why nothing in
  the source repository would otherwise catch it.

The last two pull in opposite directions from "just use relative
links everywhere", which is why they are pinned here rather than left
to be rediscovered: the daily audit reports the breakage a day later
and against the repository rather than the change.
"""

import os
import re
import tempfile
import unittest

from tests.helpers import REPO_ROOT


# [text](target), with the target not starting a scheme or an anchor.
LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
# Both fence syntaxes. A ~~~ block whose sample text contains a link is
# sample text, exactly as a ``` one is, and checking it would fail a
# documentation change that is perfectly correct.
FENCE = re.compile(r'^\s*(```|~~~)')
# A link may carry a title: [text](target "Title"). The target is
# everything up to the whitespace that introduces it.
TITLE = re.compile(r'\s+["\'(]')


def markdown_files():
    skip = {'.git', 'node_modules'}
    for root, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for name in sorted(names):
            if name.endswith('.md'):
                path = os.path.join(root, name)
                yield os.path.relpath(path, REPO_ROOT), path


def links(path):
    """Yield (line number, target) for links outside fenced code blocks."""
    in_fence = False
    with open(path) as f:
        for number, line in enumerate(f, start=1):
            if FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            # Strip inline code spans: a documented command containing
            # [x](y) is sample text, not a rendered link.
            line = re.sub(r'`[^`]*`', '', line)
            for target in LINK.findall(line):
                # Split the optional title off here, so all three
                # tests below inherit the correction.
                yield number, TITLE.split(target.strip(), maxsplit=1)[0]


def is_relative(target):
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', target):   # https:, mailto:
        return False
    if target.startswith(('#', '//')):
        return False
    if '{' in target or '<' in target:                   # a template
        return False
    return bool(target)


SELF = re.compile(
    r'^https://github\.com/shakenfist/actions/blob/(?:main|develop)/(.+)$')


class LinkParserTest(unittest.TestCase):
    """The parser's own sanity check, on a fixture rather than the tree.

    The tree-walking tests below cannot tell "nothing is wrong" from
    "the regex matched nothing", and coupling that check to how many
    links the tree happens to contain makes them fail when the
    documentation is in a perfectly correct state -- the docs-external
    rule pushes authors toward absolute URLs, so docs/ converging on
    zero relative links is a plausible future, not a defect.
    """

    SAMPLE = '''
[plain](a.md)
[titled](b.md "A title")
[anchored](c.md#section)
[absolute](https://example.com/x)

```
[fenced](never.md)
```

~~~
[tilde fenced](never.md)
~~~

Inline `[code](never.md)` is sample text.
'''

    def parse(self):
        with tempfile.NamedTemporaryFile('w', suffix='.md',
                                         delete=False) as f:
            f.write(self.SAMPLE)
            name = f.name
        try:
            return [target for _, target in links(name)]
        finally:
            os.unlink(name)

    def test_the_parser_extracts_exactly_the_real_links(self):
        self.assertEqual(
            self.parse(),
            ['a.md', 'b.md', 'c.md#section', 'https://example.com/x'])

    def test_a_title_is_not_part_of_the_target(self):
        # [text](b.md "A title") yielding 'b.md "A title"' would fail
        # every tree test on valid markdown.
        self.assertIn('b.md', self.parse())

    def test_both_fence_syntaxes_are_skipped(self):
        self.assertNotIn('never.md', self.parse())


class RelativeLinkTest(unittest.TestCase):
    def test_every_relative_link_resolves_on_disk(self):
        for relative, path in markdown_files():
            for number, target in links(path):
                if not is_relative(target):
                    continue
                # Drop any in-page anchor: docs/ci.md#section is a link
                # to a file plus a heading within it.
                clean = target.split('#', 1)[0]
                if not clean:
                    continue
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(path), clean))
                with self.subTest(file=relative, line=number, link=target):
                    self.assertTrue(
                        os.path.exists(resolved),
                        '%s:%d links to %s, which does not exist'
                        % (relative, number, target))


class SelfReferencingAbsoluteLinkTest(unittest.TestCase):
    """An absolute link back into this repository still has to resolve.

    The README rule turns every link out of README.md into
    https://github.com/shakenfist/actions/blob/main/<path>, and the
    docs-external rule does the same to anything leaving docs/. That
    makes self-referencing absolute URLs the largest class of link
    here -- and the class a relative-link checker cannot see. Renaming
    a file under docs/ would leave every one of them pointing at a
    404 with all the other tests still green.

    Checked entirely offline: the URL names a path in this repository,
    so it is verified against the working tree, not fetched.
    """

    def test_every_self_referencing_url_names_a_file_that_exists(self):
        found = False
        for relative, path in markdown_files():
            for number, target in links(path):
                match = SELF.match(target)
                if not match:
                    continue
                found = True
                clean = match.group(1).split('#', 1)[0]
                with self.subTest(file=relative, line=number, link=target):
                    self.assertTrue(
                        os.path.exists(os.path.join(REPO_ROOT, clean)),
                        '%s:%d links to %s, but %s does not exist in this '
                        'repository' % (relative, number, target, clean))
        self.assertTrue(
            found,
            'no self-referencing absolute links found -- README.md must '
            'link into this repository absolutely, so finding none means '
            'the pattern has stopped matching')


class DocsExternalLinkTest(unittest.TestCase):
    """A relative link inside docs/ must resolve inside docs/.

    Mirrors shakenfist/development's docs-external-links audit. docs/
    is published on shakenfist.com with the tree above it absent, so a
    link that escapes upwards renders correctly on GitHub and 404s
    there. Absolute URLs survive both renderings; relative links that
    stay within docs/ move with the tree and work in both.
    """

    DOCS = os.path.join(REPO_ROOT, 'docs')

    def test_no_relative_link_in_docs_escapes_docs(self):
        for relative, path in markdown_files():
            if not path.startswith(self.DOCS + os.sep):
                continue
            for number, target in links(path):
                if not is_relative(target):
                    continue
                clean = target.split('#', 1)[0]
                if not clean or clean.startswith('/'):
                    # Site-root-absolute targets are the mkdocs
                    # convention and resolve on the published site.
                    continue
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(path), clean))
                with self.subTest(file=relative, line=number, link=target):
                    self.assertTrue(
                        resolved.startswith(self.DOCS + os.sep),
                        '%s:%d links to %s, which resolves above docs/. '
                        'docs/ is published on shakenfist.com without the '
                        'tree above it, so this renders on GitHub and 404s '
                        'there -- use an absolute URL'
                        % (relative, number, target))


class ReadmeAbsoluteLinkTest(unittest.TestCase):
    """The top-level README's links must be absolute.

    shakenfist/development's readme-absolute-links audit checks this
    across the fleet, and it exists because divergulent's first PyPI
    release rendered with every relative link broken -- they resolved
    against the PyPI project page rather than the GitHub landing page.
    The audit runs daily and files an issue; this fails in the pull
    request that would have caused it.

    Only the top-level README is in scope. READMEs further down the
    tree are only ever read on the GitHub file listing, where relative
    links resolve correctly.
    """

    def test_the_top_level_readme_has_no_relative_links(self):
        path = os.path.join(REPO_ROOT, 'README.md')
        offenders = [(number, target) for number, target in links(path)
                     if is_relative(target)]
        self.assertEqual(
            offenders, [],
            'README.md is rendered off the repository landing page, so '
            'these relative links would resolve against the wrong base: %s'
            % ', '.join('line %d: %s' % o for o in offenders))


if __name__ == '__main__':
    unittest.main()
