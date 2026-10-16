#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Download the GitHub Actions source archives that CI jobs would otherwise fetch themselves.

Every job on a runner downloads the tarball of each action it uses from
codeload.github.com, and the runners share one outbound address, so a burst
of jobs gets throttled. The actions runner can be pointed at a directory of
pre-fetched archives (ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE), laid out as
DEST/<owner>_<repo>/<sha>.tar.gz. This script fills that directory from a
manifest, so the dependencies disk can carry the archives to every runner.

Usage: cache-action-archives.py MANIFEST DEST [--base-url URL] [--budget SECONDS]

MANIFEST is JSON: either a list of {"repository", "sha"} objects, or an
object with an "archives" key holding that list.

A missed archive only costs what every job pays today, while a failed
nightly image build costs every image's freshness, so nothing here is fatal.
One line of JSON goes to stdout whatever happened:

    {"cached": N, "present": N, "failed": [{"repository", "sha", "reason"}]}

and the exit status is 0 unless the arguments, the manifest or DEST are
unusable. Diagnostics go to stderr.
"""

import argparse
import http.client
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request


DEFAULT_BASE_URL = 'https://codeload.github.com'
DEFAULT_BUDGET = 600
REQUEST_TIMEOUT = 60
RETRIES = 2
DEFAULT_RETRY_WAIT = 15
MAX_RETRY_WAIT = 60
CHUNK_SIZE = 64 * 1024
# The largest archive in use (github/codeql-action) is about 2 MB, so this is generous.
# It stops a wrong or hostile response filling the builder's disk.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024

# GitHub owner names cannot contain '_' or '.', and the cache directory name turns the
# slash into '_', so allowing them would let a_b/c and a/b_c share a directory.
REPOSITORY_RE = re.compile(r'^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')

# Patched by the tests so the retry path does not make the suite slow.
_sleep = time.sleep


class FetchError(Exception):
    """A single archive could not be cached. The message is the reason."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: codeload answers this URL directly, so a redirect is not something to trust."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FetchError('redirected')


def log(message):
    print(message, file=sys.stderr)


def load_manifest(path):
    """Return the list of manifest entries from either supported shape."""
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get('archives')
    if not isinstance(data, list):
        raise ValueError('manifest must be a list or an object with an "archives" list')
    return data


def valid_entry(entry):
    """Check an entry's shape and spelling before it is used to build a path or URL.

    Without this a malformed manifest could write outside DEST.
    """
    return (isinstance(entry, dict)
            and isinstance(entry.get('repository'), str) and REPOSITORY_RE.match(entry['repository'])
            and isinstance(entry.get('sha'), str) and SHA_RE.match(entry['sha'])
            and entry['repository'].split('/')[0] not in ('.', '..')
            and entry['repository'].split('/')[1] not in ('.', '..'))


def archive_path(dest, repository, sha):
    """Where the runner looks: the owner/repo slash becomes an underscore and case is kept."""
    return os.path.join(dest, repository.replace('/', '_'), sha + '.tar.gz')


def verify(path, sha):
    """Raise FetchError unless path is the tarball git archive makes of commit sha.

    That means gzip, one top-level directory whose name ends in -<sha> (the
    repository's name is not matched, as the casing codeload uses for it has
    not been checked for every repository), no absolute or .. names, and only
    files, directories and symlinks. The pax global header's comment is git
    archive's record of the commit the archive was made from, and it must be
    sha, so an archive of some other commit is refused even if its directory
    is named for this one.
    """
    top = None
    count = 0
    try:
        with tarfile.open(path, 'r:gz') as tar:
            for member in tar:
                count += 1
                if not (member.isreg() or member.isdir() or member.issym()):
                    raise FetchError('unexpected member type')
                name = member.name
                if name.startswith('/'):
                    raise FetchError('absolute path in archive')
                parts = name.split('/')
                if '..' in parts:
                    raise FetchError('.. component in archive')
                if top is None:
                    top = parts[0]
                elif parts[0] != top:
                    raise FetchError('more than one top-level directory')
            # Only filled in once the first member has been read, so check it after the loop.
            comment = tar.pax_headers.get('comment')
    except FetchError:
        raise
    except Exception as e:
        # Not gzip, truncated gzip, corrupt tar: all the same to a caller.
        raise FetchError('unreadable archive: %s' % e)
    if count == 0:
        raise FetchError('empty archive')
    if not top or not top.endswith('-' + sha):
        raise FetchError('top-level directory does not end in -<sha>')
    if comment != sha:
        raise FetchError('archive is not for %s' % sha)


def retry_wait(error):
    """Seconds to wait before retrying a throttled request."""
    value = error.headers.get('Retry-After') if error.headers else None
    try:
        wait = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_WAIT
    return max(0, min(wait, MAX_RETRY_WAIT))


def download(url, tmp, deadline):
    """Stream url to tmp, retrying 429 and 5xx. Raises FetchError."""
    attempt = 0
    opener = urllib.request.build_opener(NoRedirect)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError('budget')
        try:
            with opener.open(url, timeout=min(REQUEST_TIMEOUT, remaining)) as response:
                expected = response.headers.get('Content-Length')
                if expected is not None and int(expected) > MAX_ARCHIVE_BYTES:
                    raise FetchError('too large')
                received = 0
                with open(tmp, 'wb') as f:
                    while True:
                        if time.monotonic() >= deadline:
                            raise FetchError('budget')
                        # read1, not read: read(n) loops until n bytes or EOF, and every recv
                        # resets the socket timeout, so a server trickling bytes could hold one
                        # call open for ever and this deadline check would never run.
                        chunk = response.read1(CHUNK_SIZE)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > MAX_ARCHIVE_BYTES:
                            raise FetchError('too large')
                        f.write(chunk)
                # read1 does not raise when the server closes early,
                # only an unbounded read() does, so check the length by hand.
                if expected is not None and received != int(expected):
                    raise http.client.IncompleteRead(b'', int(expected) - received)
            return
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < RETRIES:
                attempt += 1
                wait = min(retry_wait(e), max(0, deadline - time.monotonic()))
                log('HTTP %d from %s, retry %d in %ds' % (e.code, url, attempt, wait))
                _sleep(wait)
                continue
            raise FetchError('HTTP %d' % e.code)
        except http.client.IncompleteRead:
            raise FetchError('short read')
        except ValueError:
            raise FetchError('bad Content-Length')
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise FetchError('download error: %s' % e)


def remove_stale_temporaries(directory, sha):
    """Remove partial downloads of sha left behind by an earlier run that was killed."""
    for name in os.listdir(directory):
        if name.startswith('.tmp-%s-' % sha):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError as e:
                log('could not remove stale %s: %s' % (name, e))


def fetch(entry, dest, base_url, deadline):
    """Cache one archive. Returns 'present' or 'cached', or raises FetchError."""
    repository = entry['repository']
    sha = entry['sha']
    final = archive_path(dest, repository, sha)

    if time.monotonic() >= deadline:
        raise FetchError('budget')

    if os.path.exists(final):
        try:
            verify(final, sha)
            return 'present'
        except FetchError as e:
            log('%s %s present but bad (%s), fetching again' % (repository, sha, e))

    directory = os.path.dirname(final)
    tmp = os.path.join(directory, '.tmp-%s-%d' % (sha, os.getpid()))
    try:
        os.makedirs(directory, exist_ok=True)
        remove_stale_temporaries(directory, sha)
        download('%s/%s/tar.gz/%s' % (base_url.rstrip('/'), repository, sha), tmp, deadline)
        verify(tmp, sha)
        os.rename(tmp, final)
    except OSError as e:
        raise FetchError('filesystem error: %s' % e)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError as e:
            log('could not remove %s: %s' % (tmp, e))
    return 'cached'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cache GitHub Actions source archives.')
    parser.add_argument('manifest')
    parser.add_argument('dest')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--budget', type=float, default=DEFAULT_BUDGET,
                        help='seconds allowed for the whole run')
    args = parser.parse_args(argv)

    try:
        entries = load_manifest(args.manifest)
    except (OSError, ValueError) as e:
        log('cannot read manifest %s: %s' % (args.manifest, e))
        return 2
    try:
        os.makedirs(args.dest, exist_ok=True)
    except OSError as e:
        log('cannot use %s: %s' % (args.dest, e))
        return 2

    deadline = time.monotonic() + args.budget
    summary = {'cached': 0, 'present': 0, 'failed': []}
    for entry in entries:
        if not valid_entry(entry):
            repository = entry.get('repository') if isinstance(entry, dict) else None
            sha = entry.get('sha') if isinstance(entry, dict) else None
            summary['failed'].append({'repository': repository, 'sha': sha, 'reason': 'invalid'})
            continue
        try:
            summary[fetch(entry, args.dest, args.base_url, deadline)] += 1
        except FetchError as e:
            log('%s %s: %s' % (entry['repository'], entry['sha'], e))
            summary['failed'].append({'repository': entry['repository'], 'sha': entry['sha'], 'reason': str(e)})

    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
