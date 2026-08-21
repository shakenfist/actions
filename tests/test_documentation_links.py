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
import unittest

from tests.helpers import REPO_ROOT


# [text](target), with the target not starting a scheme or an anchor.
LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
FENCE = re.compile(r'^\s*```')


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
                yield number, target.strip()


def is_relative(target):
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', target):   # https:, mailto:
        return False
    if target.startswith(('#', '//')):
        return False
    if '{' in target or '<' in target:                   # a template
        return False
    return bool(target)


class RelativeLinkTest(unittest.TestCase):
    def test_every_relative_link_resolves_on_disk(self):
        found = False
        for relative, path in markdown_files():
            for number, target in links(path):
                if not is_relative(target):
                    continue
                found = True
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
        self.assertTrue(found, 'no relative documentation links found')


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
        found = False
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
                found = True
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
        self.assertTrue(found, 'no relative links inside docs/ found')


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
