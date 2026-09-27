#!/usr/bin/env python3

"""Tests for tools/ci_loki_dump.py.

The dump runs on a CI cluster's primary, where nothing here can reach
it, and its predecessor failed silently in every bundle for a month
(shakenfist/actions#16). So the claims worth pinning are the ones a
green step used to hide: that a failure of any kind exits non-zero and
leaves a file saying so, and that the backward paging reaches the whole
window -- including across page boundaries where entries share a
timestamp -- rather than stopping at Loki's per-query limit.
"""

import http.server
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.parse

import yaml

from tests.helpers import REPO_ROOT
from tests.helpers import load_script


dump = load_script('tools/ci_loki_dump.py', 'ci_loki_dump')

PLAYBOOK = os.path.join(REPO_ROOT, 'ansible', 'ci-gather-logs-loki.yml')


class FakeLoki(object):
    """A backward query_range over an in-memory list of (labels, ts, line)."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def __call__(self, query, start_ns, end_ns, limit):
        self.calls.append((start_ns, end_ns, limit))
        selected = [e for e in self.entries if start_ns <= e[1] < end_ns]
        selected.sort(key=lambda e: e[1], reverse=True)
        selected = selected[:limit]
        streams = {}
        for labels, ts, line in selected:
            key = json.dumps(labels, sort_keys=True)
            streams.setdefault(key, {'stream': labels, 'values': []})['values'].append([str(ts), line])
        return {'status': 'success', 'data': {'resultType': 'streams', 'result': list(streams.values())}}


def lines_of(streams):
    return sorted((s['stream']['node'], int(ts), line) for s in streams for ts, line in s['values'])


class CollectTestCase(unittest.TestCase):
    def test_pages_through_more_than_one_query_limit(self):
        entries = [({'node': 'sf-%d' % (i % 3)}, 1000 + i, 'line %d' % i) for i in range(23)]
        fake = FakeLoki(entries)
        streams, stats = dump.collect(fake, 'q', 0, 10000, 5, 1000)

        self.assertEqual(23, stats['entries'])
        self.assertFalse(stats['truncated'])
        self.assertGreater(stats['pages'], 4)
        self.assertEqual(sorted((e[0]['node'], e[1], e[2]) for e in entries), lines_of(streams))

    def test_entries_sharing_a_timestamp_across_a_page_boundary_are_kept_once(self):
        # Three entries at one timestamp straddle the first page boundary.
        entries = [({'node': 'sf-1'}, 50, 'early')]
        entries += [({'node': 'sf-%d' % i}, 100, 'tied %d' % i) for i in range(3)]
        entries += [({'node': 'sf-1'}, 200 + i, 'late %d' % i) for i in range(3)]
        streams, stats = dump.collect(FakeLoki(entries), 'q', 0, 1000, 4, 1000)

        self.assertEqual(len(entries), stats['entries'])
        self.assertEqual(sorted((e[0]['node'], e[1], e[2]) for e in entries), lines_of(streams))

    def test_a_full_page_at_one_timestamp_cannot_loop_forever(self):
        entries = [({'node': 'sf-%d' % i}, 100, 'tied %d' % i) for i in range(10)]
        entries.append(({'node': 'sf-1'}, 50, 'older'))
        streams, stats = dump.collect(FakeLoki(entries), 'q', 0, 1000, 4, 1000)

        # Loki cannot page within one nanosecond, so some tied entries are
        # unreachable; what matters is that it terminates, says so, and
        # still reaches the older entry beyond the tie.
        self.assertGreaterEqual(stats['stuck_boundaries'], 1)
        self.assertIn(('sf-1', 50, 'older'), lines_of(streams))

    def test_max_entries_keeps_the_newest_and_reports_truncation(self):
        entries = [({'node': 'sf-1'}, 1000 + i, 'line %d' % i) for i in range(50)]
        streams, stats = dump.collect(FakeLoki(entries), 'q', 0, 10000, 10, 20)

        self.assertTrue(stats['truncated'])
        self.assertEqual(20, stats['entries'])
        timestamps = [ts for _, ts, _ in lines_of(streams)]
        self.assertEqual(list(range(1030, 1050)), timestamps)

    def test_exactly_reaching_max_entries_with_nothing_older_is_not_truncated(self):
        entries = [({'node': 'sf-1'}, 1000 + i, 'line %d' % i) for i in range(8)]
        _, stats = dump.collect(FakeLoki(entries), 'q', 0, 10000, 10, 8)
        self.assertFalse(stats['truncated'])

    def test_values_are_ascending_within_each_stream(self):
        entries = [({'node': 'sf-1'}, 1000 + i, 'line %d' % i) for i in range(12)]
        streams, _ = dump.collect(FakeLoki(entries), 'q', 0, 10000, 5, 1000)
        self.assertEqual(1, len(streams))
        timestamps = [int(ts) for ts, _ in streams[0]['values']]
        self.assertEqual(sorted(timestamps), timestamps)


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = os.path.join(self.tmp.name, 'loki.json')

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, fetch):
        return dump.main(['--output', self.output, '--window-seconds', '100'], fetch=fetch, now=lambda: 1000)

    def read_output(self):
        with open(self.output) as f:
            return json.load(f)

    def test_success_writes_a_loki_shaped_response(self):
        fake = FakeLoki([({'node': 'sf-1'}, 950 * 1000000000, 'hello')])
        rc = self.run_main(lambda url, *args: fake(*args))

        self.assertEqual(0, rc)
        output = self.read_output()
        self.assertEqual('success', output['status'])
        self.assertEqual('streams', output['data']['resultType'])
        self.assertEqual([['950000000000', 'hello']], output['data']['result'][0]['values'])
        self.assertEqual(1, output['dump']['entries'])
        self.assertEqual(str(900 * 1000000000), output['dump']['start'])

    def test_an_unreachable_loki_fails_and_says_so(self):
        def unreachable(*args):
            raise dump.DumpError('could not reach Loki at http://localhost:3100: refused')

        self.assertEqual(1, self.run_main(unreachable))
        output = self.read_output()
        self.assertEqual('error', output['status'])
        self.assertIn('could not reach Loki', output['dump']['error'])
        self.assertNotIn('data', output)

    def test_an_empty_loki_fails_rather_than_reading_as_a_quiet_cluster(self):
        self.assertEqual(1, self.run_main(lambda url, *args: FakeLoki([])(*args)))
        self.assertEqual('empty', self.read_output()['status'])


# A test fixture bound to loopback that only ever sends the literal Content-Type below, so no outside
# data reaches send_header().
# audit-ok: header-sanitization
class Handler(http.server.BaseHTTPRequestHandler):
    responses = []
    requests = []

    def do_GET(self):
        self.requests.append(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
        code, body = self.responses.pop(0)
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchPageTestCase(unittest.TestCase):
    def setUp(self):
        Handler.responses = []
        Handler.requests = []
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_sends_a_backward_query_range(self):
        Handler.responses.append((200, b'{"status": "success", "data": {"result": []}}'))
        response = dump.fetch_page(self.url, '{job="shakenfist"}', 1, 2, 5000)

        self.assertEqual('success', response['status'])
        params = Handler.requests[0]
        self.assertEqual(['{job="shakenfist"}'], params['query'])
        self.assertEqual(['backward'], params['direction'])
        self.assertEqual(['1'], params['start'])
        self.assertEqual(['2'], params['end'])
        self.assertEqual(['5000'], params['limit'])

    def test_ignores_a_proxy_in_the_environment(self):
        # The gather play exports a squid proxy; Loki is always local.
        Handler.responses.append((200, b'{"status": "success", "data": {"result": []}}'))
        saved = os.environ.get('http_proxy')
        os.environ['http_proxy'] = 'http://192.0.2.1:3128'
        try:
            dump.fetch_page(self.url, 'q', 1, 2, 10, timeout=5)
        finally:
            if saved is None:
                del os.environ['http_proxy']
            else:
                os.environ['http_proxy'] = saved

    def test_http_error_is_a_dump_error(self):
        Handler.responses.append((400, b'max entries limit per query exceeded'))
        with self.assertRaisesRegex(dump.DumpError, 'HTTP 400.*max entries'):
            dump.fetch_page(self.url, 'q', 1, 2, 10)

    def test_non_success_status_is_a_dump_error(self):
        Handler.responses.append((200, b'{"status": "error"}'))
        with self.assertRaisesRegex(dump.DumpError, 'status'):
            dump.fetch_page(self.url, 'q', 1, 2, 10)

    def test_unreachable_is_a_dump_error(self):
        # A port which was just listening and is now closed refuses at once.
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaisesRegex(dump.DumpError, 'could not reach Loki'):
            dump.fetch_page('http://127.0.0.1:%d' % port, 'q', 1, 2, 10, timeout=5)


class PlaybookTestCase(unittest.TestCase):
    """The shape of the bug in #16 was the dump running on the wrong host."""

    def setUp(self):
        with open(PLAYBOOK) as f:
            self.plays = yaml.safe_load(f)

    def test_no_play_on_the_runner_talks_to_loki(self):
        for play in self.plays:
            if play['hosts'] == 'localhost':
                self.assertNotIn('3100', json.dumps(play))

    def test_the_dump_is_the_last_play_and_runs_on_the_primary(self):
        last = self.plays[-1]
        self.assertEqual('primary', last['hosts'])
        scripts = [t for t in last['tasks'] if 'script' in t]
        self.assertEqual(1, len(scripts))
        self.assertIn('tools/ci_loki_dump.py', scripts[0]['script'])
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, 'tools', 'ci_loki_dump.py')))

    def test_a_failed_dump_fails_the_play(self):
        fails = [t for t in self.plays[-1]['tasks'] if 'fail' in t]
        self.assertEqual(1, len(fails))
        self.assertIn('loki_dump is failed', fails[0]['when'])

    def test_the_failure_names_the_dumps_reason(self):
        # Over ssh with a tty the script's stderr arrives in stdout, so a
        # message built from stderr alone reads "non-zero return code".
        fails = [t for t in self.plays[-1]['tasks'] if 'fail' in t]
        self.assertIn('loki_dump.stdout', fails[0]['fail']['msg'])


if __name__ == '__main__':
    unittest.main()
