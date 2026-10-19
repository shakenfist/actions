#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Check that built wheels contain what we intend to publish.

For every *.whl directly under a directory, fail if any member has a
directory component equal to 'tests' or ending in '_tests', or matches one
of the extra fnmatch globs given in CHECK_DIST_FORBID (newline separated).
Only directory components count, so pkg/contests.py and pkg/tests.py pass.
When a test directory was found, a hint follows naming the usual cause.
A wheel must also hold at least one file outside its *.dist-info/ directory,
in a package or under <name>.data/. A zip with no members, or one holding
only dist-info, is not a usable wheel and fails.

Member names are read through zipfile rather than by parsing the output of
"python3 -m zipfile -l", which is a human-readable listing and not a stable
interface: taking its first whitespace-delimited field would truncate a path
containing a space, which is how an unwanted member could slip past.

Usage: check-dist.py [path]

The path defaults to CHECK_DIST_PATH, and then to dist/.
"""

import fnmatch
import glob
import os
import sys
import zipfile


HINT = (
    'include-package-data defaults to true under pyproject.toml, and '
    "setuptools_scm's file finder then offers every git-tracked file as "
    'package data. A packages.find exclude therefore drops the tests '
    'package but the tests come back as data. Setting include-package-data '
    '= false under [tool.setuptools] is the usual fix.')


def annotate(message):
    """Escape text the way the runner unescapes a workflow command's message.

    Without it, a member name containing a newline could start a line of its own and inject a command.
    """
    return message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def is_test_component(component):
    return component == 'tests' or component.endswith('_tests')


def test_directory(member):
    """Return the first test directory component of a wheel member, or None."""
    parts = member.split('/')
    # A trailing slash marks a directory entry, so its last component is a
    # directory too. Otherwise the last component is a file name.
    directories = parts if member.endswith('/') else parts[:-1]
    for component in directories:
        if is_test_component(component):
            return component
    return None


def offending_reason(member, forbid):
    """Return why a wheel member must not ship, or None if it may."""
    if '\\' in member:
        return 'backslash in member name (wheels use /)'
    component = test_directory(member)
    if component is not None:
        return 'test directory %r' % component
    for pattern in forbid:
        if fnmatch.fnmatchcase(member, pattern):
            return 'matches forbid glob %r' % pattern
    return None


def is_payload(member):
    """Return True if a wheel member is a file outside the *.dist-info/ directory.

    Files under <name>.data/ count, because a wheel that only installs scripts holds nothing else.
    """
    if member.endswith('/'):
        return False
    return not member.split('/')[0].endswith('.dist-info')


def check_wheel(wheel, forbid):
    """Return (member_count, [(member, reason)], has_payload) for one wheel.

    Raises zipfile.BadZipFile or OSError if the file cannot be read.
    """
    with zipfile.ZipFile(wheel) as zf:
        members = zf.namelist()
    offenders = []
    for member in members:
        reason = offending_reason(member, forbid)
        if reason:
            offenders.append((member, reason))
    return len(members), offenders, any(is_payload(m) for m in members)


def parse_forbid(text):
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


def run(path, forbid):
    """Check every wheel under path, print the results, return an exit code."""
    wheels = sorted(glob.glob(os.path.join(glob.escape(path), '*.whl')))
    if not wheels:
        print('::error::check-dist: no .whl file found under %s' % annotate(path))
        return 1

    failed = False
    # The hint explains how tests reach a wheel, so it is printed only when
    # some did, not for an unreadable archive or a forbid glob.
    shipped_tests = False
    for wheel in wheels:
        try:
            count, offenders, has_payload = check_wheel(wheel, forbid)
        except (zipfile.BadZipFile, OSError) as e:
            print('::error::check-dist: %s is not a readable zip archive (%s)' % (annotate(wheel), annotate(str(e))))
            failed = True
            continue

        if not has_payload:
            failed = True
            print('::error::check-dist: %s holds no package or data (%d member(s), none outside *.dist-info/)'
                  % (annotate(wheel), count))

        if offenders:
            failed = True
            print('::error::check-dist: %s contains %d member(s) that must not ship:'
                  % (annotate(wheel), len(offenders)))
            for member, reason in offenders:
                print('  %s (%s)' % (annotate(member), reason))
                if test_directory(member) is not None:
                    shipped_tests = True
        elif has_payload:
            print('check-dist: %s OK (%d members)' % (annotate(wheel), count))

    if shipped_tests:
        print('check-dist: hint: ' + HINT)
    return 1 if failed else 0


def main(argv, environ):
    path = argv[1] if len(argv) > 1 else (environ.get('CHECK_DIST_PATH') or 'dist/')
    return run(path, parse_forbid(environ.get('CHECK_DIST_FORBID')))


if __name__ == '__main__':
    sys.exit(main(sys.argv, os.environ))
