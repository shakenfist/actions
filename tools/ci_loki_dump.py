#!/usr/bin/env python3
# Copyright 2019 Michael Still and contributors
#
# Dump a CI cluster's central Loki view to a file, for the log bundle.
#
# This runs ON THE PRIMARY, not on the CI runner. ansible/ci-gather-logs-loki.yml
# ships it there with the script module, because Loki is installed only on the
# primary (build-smoke-cluster/action.yml) and listens on its localhost. The
# first version of the dump ran curl in the gather playbook's localhost play,
# which is the runner, so every bundle carried a zero byte file for a month and
# the step stayed green: shakenfist/actions#16.
#
# Two properties matter more than anything else here:
#
# * An empty dump must never look like a quiet cluster. Any failure -- Loki
#   unreachable, a non-success response, or no entries at all -- writes a file
#   whose "status" says so and exits non-zero, and the playbook turns that exit
#   into a failed task. A cluster which ran the smoke suite has always logged
#   something, so zero entries is an apparatus failure, not a result.
#
# * The dump must reach the end of the run. Loki caps a query_range at
#   max_entries_limit_per_query (5000 by default) and says nothing when it
#   truncates, so a single forward query returns the first 5000 lines of the
#   deploy and never reaches the tests. This pages backward from the end of the
#   window instead, so the lines nearest the failure are the ones collected
#   first, and when --max-entries is reached it is the start of the deploy that
#   is left out. The output records whether that happened.
#
# The output keeps the shape of a Loki query_range response -- status plus
# data.result, a list of {stream, values} -- so anything that read the old dump
# still reads this one, with a "dump" object beside them describing the
# collection. Streams with identical labels are merged, and each stream's
# values are in ascending time order.
#
# Standard library only: the primary has a python3 but no guarantee of anything
# else.

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_URL = 'http://localhost:3100'
DEFAULT_QUERY = '{job="shakenfist"}'


class DumpError(Exception):
    pass


def fetch_page(base_url, query, start_ns, end_ns, limit, timeout=60):
    """Run one backward query_range and return the decoded response.

    Proxies are bypassed explicitly rather than trusted to no_proxy: Loki is
    always on this host, and the gather play exports a squid proxy for pip.
    """
    params = urllib.parse.urlencode({
        'query': query,
        'start': str(start_ns),
        'end': str(end_ns),
        'limit': str(limit),
        'direction': 'backward',
    })
    url = '%s/loki/api/v1/query_range?%s' % (base_url.rstrip('/'), params)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        raise DumpError('Loki returned HTTP %d: %s' % (e.code, e.read()[:500].decode('utf-8', 'replace')))
    except (urllib.error.URLError, OSError) as e:
        raise DumpError('could not reach Loki at %s: %s' % (base_url, e))

    try:
        decoded = json.loads(body)
    except ValueError as e:
        raise DumpError('Loki returned a response which is not JSON: %s' % e)
    if decoded.get('status') != 'success':
        raise DumpError('Loki returned status %r' % decoded.get('status'))
    return decoded


def collect(fetch, query, start_ns, end_ns, page_limit, max_entries):
    """Page backward through [start_ns, end_ns) and return (streams, stats).

    fetch has fetch_page's signature minus base_url, so the paging can be
    tested without a Loki.

    Loki treats end as exclusive. Each following page ends one nanosecond
    after the oldest timestamp already seen, so entries sharing that
    timestamp are fetched again rather than skipped, and are dropped here as
    duplicates. A page whose entries all share one timestamp would never
    advance that way, so the boundary is then stepped past it instead. That
    is the only case in which entries can be lost, and it is counted in the
    stats as stuck_boundaries.
    """
    streams = {}
    seen = set()
    entries = 0
    pages = 0
    truncated = False
    stuck_boundaries = 0
    page_end = end_ns

    while True:
        response = fetch(query, start_ns, page_end, page_limit)
        pages += 1
        result = response.get('data', {}).get('result', [])

        page = []
        for stream in result:
            labels = stream.get('stream', {})
            key = json.dumps(labels, sort_keys=True)
            for ts, line in stream.get('values', []):
                page.append((int(ts), key, labels, ts, line))
        returned = len(page)
        if not page:
            break
        oldest = min(entry[0] for entry in page)

        # Newest first, so that when the cap lands mid page it is the older
        # entries which are left out.
        page.sort(key=lambda entry: entry[0], reverse=True)
        new = 0
        for _, key, labels, ts, line in page:
            identity = (key, ts, line)
            if identity in seen:
                continue
            if entries >= max_entries:
                truncated = True
                break
            seen.add(identity)
            streams.setdefault(key, {'stream': labels, 'values': []})['values'].append([ts, line])
            new += 1
            entries += 1

        if truncated:
            break
        if entries >= max_entries:
            # Full at a page boundary. A short page means Loki had nothing
            # older to give; a full one means it may have.
            truncated = returned >= page_limit
            break
        if returned < page_limit:
            break
        if new == 0 or oldest + 1 >= page_end:
            stuck_boundaries += 1
            page_end = oldest
        else:
            page_end = oldest + 1
        if page_end <= start_ns:
            break

    merged = []
    for key in sorted(streams):
        stream = streams[key]
        stream['values'].sort(key=lambda value: int(value[0]))
        merged.append(stream)

    stats = {
        'pages': pages,
        'entries': entries,
        'truncated': truncated,
        'stuck_boundaries': stuck_boundaries,
    }
    return merged, stats


def main(argv=None, fetch=fetch_page, now=time.time):
    parser = argparse.ArgumentParser(description='Dump the central Loki view for a CI log bundle.')
    parser.add_argument('--url', default=DEFAULT_URL, help='Loki base URL.')
    parser.add_argument('--query', default=DEFAULT_QUERY, help='LogQL stream selector to dump.')
    parser.add_argument('--window-seconds', type=int, default=21600,
                        help='How far back from now to dump. The CI Loki is fresh per run, so a wide '
                             'window only ever holds this run.')
    parser.add_argument('--page-limit', type=int, default=5000,
                        help='Entries per query_range. Loki refuses more than max_entries_limit_per_query.')
    parser.add_argument('--max-entries', type=int, default=200000,
                        help='Collect at most this many entries, keeping the newest.')
    parser.add_argument('--output', required=True, help='Where to write the dump.')
    args = parser.parse_args(argv)

    end_ns = int(now()) * 1000000000
    start_ns = end_ns - args.window_seconds * 1000000000
    dump = {
        'query': args.query,
        'start': str(start_ns),
        'end': str(end_ns),
        'page_limit': args.page_limit,
        'max_entries': args.max_entries,
    }

    def page(query, start, end, limit):
        return fetch(args.url, query, start, end, limit)

    try:
        streams, stats = collect(page, args.query, start_ns, end_ns, args.page_limit, args.max_entries)
    except DumpError as e:
        dump['error'] = str(e)
        output = {'status': 'error', 'dump': dump}
        rc = 1
        message = 'Loki dump FAILED: %s' % e
    else:
        dump.update(stats)
        output = {
            'status': 'success',
            'data': {'resultType': 'streams', 'result': streams},
            'dump': dump,
        }
        if stats['entries'] == 0:
            output['status'] = 'empty'
            rc = 1
            message = ('Loki dump FAILED: Loki answered but held no entries for %s in the last %d seconds, '
                       'so nothing shipped logs to it' % (args.query, args.window_seconds))
        else:
            rc = 0
            message = 'Loki dump collected %d entries in %d pages%s' % (
                stats['entries'], stats['pages'],
                ', TRUNCATED at --max-entries: the oldest entries are missing' if stats['truncated'] else '')

    with open(args.output, 'w') as f:
        json.dump(output, f)
    print(message, file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == '__main__':
    sys.exit(main())
