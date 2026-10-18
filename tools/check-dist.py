#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Check that built wheels contain what we intend to publish.

For every *.whl directly under a directory, fail if any member has a
directory component equal to 'tests' or ending in '_tests', or matches one
of the extra fnmatch globs given in CHECK_DIST_FORBID (newline separated).
Only directory components count, so pkg/contests.py and pkg/tests.py pass.

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


def is_test_component(component):
    return component == 'tests' or component.endswith('_tests')


def offending_reason(member, forbid):
    """Return why a wheel member must not ship, or None if it may."""
    parts = member.split('/')
    # A trailing slash marks a directory entry, so its last component is a
    # directory too. Otherwise the last component is a file name.
    directories = parts if member.endswith('/') else parts[:-1]
    for component in directories:
        if is_test_component(component):
            return 'test directory %r' % component
    for pattern in forbid:
        if fnmatch.fnmatchcase(member, pattern):
            return 'matches forbid glob %r' % pattern
    return None


def check_wheel(wheel, forbid):
    """Return (member_count, [(member, reason)]) for one wheel.

    Raises zipfile.BadZipFile or OSError if the file cannot be read.
    """
    with zipfile.ZipFile(wheel) as zf:
        members = zf.namelist()
    offenders = []
    for member in members:
        reason = offending_reason(member, forbid)
        if reason:
            offenders.append((member, reason))
    return len(members), offenders


def parse_forbid(text):
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


def run(path, forbid):
    """Check every wheel under path, print the results, return an exit code."""
    wheels = sorted(glob.glob(os.path.join(glob.escape(path), '*.whl')))
    if not wheels:
        print('::error::check-dist: no .whl file found under %s' % path)
        return 1

    failed = False
    for wheel in wheels:
        try:
            count, offenders = check_wheel(wheel, forbid)
        except (zipfile.BadZipFile, OSError) as e:
            print('::error::check-dist: %s is not a readable zip archive (%s)' % (wheel, e))
            failed = True
            continue

        if offenders:
            failed = True
            print('::error::check-dist: %s contains %d member(s) that must not ship:' % (wheel, len(offenders)))
            for member, reason in offenders:
                print('  %s (%s)' % (member, reason))
        else:
            print('check-dist: %s OK (%d members)' % (wheel, count))

    if failed:
        print('check-dist: hint: ' + HINT)
        return 1
    return 0


def main(argv, environ):
    path = argv[1] if len(argv) > 1 else (environ.get('CHECK_DIST_PATH') or 'dist/')
    return run(path, parse_forbid(environ.get('CHECK_DIST_FORBID')))


if __name__ == '__main__':
    sys.exit(main(sys.argv, os.environ))
