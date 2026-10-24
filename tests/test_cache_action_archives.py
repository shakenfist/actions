# Copyright 2026 Michael Still and contributors

"""Tests for ansible/files/cache-action-archives.py.

The script is run against a real HTTP server on 127.0.0.1 serving tarballs
built in memory, because what matters is how it treats a response: a short
body, a throttle, a tarball of the wrong shape.
"""

import contextlib
import http.client
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

from tests.helpers import REPO_ROOT, load_script


caa = load_script('ansible/files/cache-action-archives.py', 'cache_action_archives')

SHA = '3d3c42e5aac5ba805825da76410c181273ba90b1'
SHA2 = '6f2ac81fb2b0a1b6d1d3e8f1a5c0e9d7c4b3a291'
REPO = 'actions/checkout'


def make_tarball(members, gz=True, comment=SHA):
    """Build a tarball like codeload's, from (name, content) pairs.

    A content of None makes a directory, bytes make a file, and a (type, linkname) tuple makes
    a member of that tarfile type. The pax global header git archive writes, whose comment is
    the commit, is included unless comment is None.
    """
    buf = io.BytesIO()
    pax_headers = {'comment': comment} if comment else None
    with tarfile.open(fileobj=buf, mode='w:gz' if gz else 'w', format=tarfile.PAX_FORMAT,
                      pax_headers=pax_headers) as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(content, tuple):
                info.type, info.linkname = content
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def good(sha=SHA, repo='checkout'):
    return make_tarball([('%s-%s/' % (repo, sha), None), ('%s-%s/action.yml' % (repo, sha), b'name: x\n')],
                        comment=sha)


# A test fixture bound to loopback that only sends the headers the tests themselves put in
# server.routes, so no outside data reaches send_header().
# audit-ok: header-sanitization
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.requests.append(self.path)
        behaviour = server.routes.get(self.path)
        if isinstance(behaviour, list):
            behaviour = behaviour.pop(0) if len(behaviour) > 1 else behaviour[0]
        if behaviour is None:
            behaviour = (404, {}, b'')
        status, headers, body = behaviour
        self.send_response(status)
        for key, value in headers.items():
            if key != 'X-No-Length':
                self.send_header(key, value)
        # The server speaks HTTP/1.0, so without a length the end of the body is the close.
        if 'Content-Length' not in headers and 'X-No-Length' not in headers:
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if callable(body):
            body(self)
        else:
            self.wfile.write(body)
        self.close_connection = True


class ArchiveCacheTestCase(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.requests = []
        self.server.routes = {}
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, 'cache')
        self.manifest = os.path.join(self.tmp.name, 'manifest.json')
        # A developer's proxy must not stand between the tests and 127.0.0.1.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for name in ('http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY'):
            os.environ.pop(name, None)
        sleeper = mock.patch.object(caa, '_sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def route(self, repo, sha, *behaviour):
        self.server.routes['/%s/tar.gz/%s' % (repo, sha)] = list(behaviour) if len(behaviour) > 1 else behaviour[0]

    def run_script(self, manifest, *extra):
        with open(self.manifest, 'w') as f:
            json.dump(manifest, f)
        out = io.StringIO()
        err = io.StringIO()
        base = self.base_url()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = caa.main([self.manifest, self.dest, '--base-url', base] + list(extra))
        return rc, out.getvalue(), json.loads(out.getvalue().splitlines()[0]) if out.getvalue() else None

    def entry(self, repo=REPO, sha=SHA):
        return {'repository': repo, 'sha': sha}

    def leftovers(self):
        found = []
        for root, _, files in os.walk(self.dest):
            found.extend(f for f in files if f.startswith('.tmp-'))
        return found

    def assert_fails(self, body, reason_contains=None, headers=None):
        self.route(REPO, SHA, (200, headers or {}, body))
        rc, _, summary = self.run_script([self.entry()])
        self.assertEqual(0, rc)
        self.assertEqual(0, summary['cached'])
        self.assertEqual(1, len(summary['failed']))
        if reason_contains:
            self.assertIn(reason_contains, summary['failed'][0]['reason'])
        self.assertFalse(os.path.exists(caa.archive_path(self.dest, REPO, SHA)))
        self.assertEqual([], self.leftovers())

    def test_good_archive_is_cached(self):
        self.route(REPO, SHA, (200, {}, good()))
        rc, _, summary = self.run_script([self.entry()])
        self.assertEqual(0, rc)
        self.assertEqual({'cached': 1, 'present': 0, 'failed': []}, summary)
        path = os.path.join(self.dest, 'actions_checkout', SHA + '.tar.gz')
        self.assertTrue(os.path.exists(path))
        self.assertEqual([], self.leftovers())

    def base_url(self):
        return 'http://127.0.0.1:%d' % self.server.server_address[1]

    def test_pax_global_header_is_accepted(self):
        # git archive, which codeload uses, writes a pax global header holding the commit id.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': SHA}) as tar:
            top = tarfile.TarInfo('checkout-%s/' % SHA)
            top.type = tarfile.DIRTYPE
            tar.addfile(top)
            info = tarfile.TarInfo('checkout-%s/action.yml' % SHA)
            info.size = 2
            tar.addfile(info, io.BytesIO(b'x\n'))
        path = os.path.join(self.tmp.name, 'pax.tar.gz')
        with open(path, 'wb') as f:
            f.write(buf.getvalue())
        caa.verify(path, SHA)
        self.route(REPO, SHA, (200, {}, buf.getvalue()))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual({'cached': 1, 'present': 0, 'failed': []}, summary)

    def test_second_run_is_present_and_makes_no_request(self):
        self.route(REPO, SHA, (200, {}, good()))
        self.run_script([self.entry()])
        self.server.requests.clear()
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual({'cached': 0, 'present': 1, 'failed': []}, summary)
        self.assertEqual([], self.server.requests)

    def test_manifest_shapes(self):
        self.route(REPO, SHA, (200, {}, good()))
        self.route('github/codeql-action', SHA2, (200, {}, good(SHA2, 'codeql-action')))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual(1, summary['cached'])
        _, _, summary = self.run_script({'archives': [self.entry(), self.entry('github/codeql-action', SHA2)]})
        self.assertEqual({'cached': 1, 'present': 1, 'failed': []}, summary)

    def test_empty_manifest(self):
        rc, _, summary = self.run_script([])
        self.assertEqual(0, rc)
        self.assertEqual({'cached': 0, 'present': 0, 'failed': []}, summary)

    def test_two_top_level_directories(self):
        self.assert_fails(make_tarball([('a-%s/f' % SHA, b'x'), ('b-%s/f' % SHA, b'x')]), 'top-level')

    def test_directory_without_sha_suffix(self):
        self.assert_fails(make_tarball([('checkout-main/f', b'x')]), 'top-level')

    def test_absolute_member(self):
        self.assert_fails(make_tarball([('/etc/passwd', b'x')]), 'absolute')

    def test_dotdot_member(self):
        self.assert_fails(make_tarball([('checkout-%s/../../x' % SHA, b'x')]), '..')

    def members(self):
        return [('checkout-%s/' % SHA, None), ('checkout-%s/action.yml' % SHA, b'x')]

    def test_missing_pax_comment(self):
        self.assert_fails(make_tarball(self.members(), comment=None), 'archive is not for %s' % SHA)

    def test_pax_comment_for_another_sha(self):
        self.assert_fails(make_tarball(self.members(), comment=SHA2), 'archive is not for %s' % SHA)

    def test_hardlink_member(self):
        members = self.members() + [('checkout-%s/link' % SHA, (tarfile.LNKTYPE, 'checkout-%s/action.yml' % SHA))]
        self.assert_fails(make_tarball(members), 'unexpected member type')

    def test_special_file_members(self):
        for kind in (tarfile.FIFOTYPE, tarfile.CHRTYPE):
            members = self.members() + [('checkout-%s/special' % SHA, (kind, ''))]
            self.assert_fails(make_tarball(members), 'unexpected member type')

    def test_symlink_member_is_accepted(self):
        members = self.members() + [('checkout-%s/link' % SHA, (tarfile.SYMTYPE, 'action.yml'))]
        self.route(REPO, SHA, (200, {}, make_tarball(members)))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual({'cached': 1, 'present': 0, 'failed': []}, summary)

    def test_empty_archive(self):
        self.assert_fails(make_tarball([], comment=None), 'empty')

    def test_not_gzip(self):
        self.assert_fails(b'<html>an error page</html>', 'unreadable')

    def test_truncated_gzip(self):
        self.assert_fails(good()[:40], 'unreadable')

    def test_short_body_is_a_failure(self):
        body = good()
        self.assert_fails(body[:len(body) // 2], 'short read', headers={'Content-Length': str(len(body))})

    def test_content_length_over_the_cap(self):
        body = good()
        with mock.patch.object(caa, 'MAX_ARCHIVE_BYTES', len(body) - 1):
            self.assert_fails(body, 'too large')

    def test_body_over_the_cap_without_content_length(self):
        body = good()
        with mock.patch.object(caa, 'MAX_ARCHIVE_BYTES', len(body) - 1):
            self.assert_fails(body, 'too large', headers={'X-No-Length': '1'})

    def test_body_at_the_cap_is_accepted(self):
        body = good()
        self.route(REPO, SHA, (200, {'X-No-Length': '1'}, body))
        with mock.patch.object(caa, 'MAX_ARCHIVE_BYTES', len(body)):
            _, _, summary = self.run_script([self.entry()])
        self.assertEqual(1, summary['cached'])

    def test_redirect_is_refused(self):
        self.route(REPO, SHA, (302, {'Location': '/elsewhere'}, b''))
        self.server.routes['/elsewhere'] = (200, {}, good())
        rc, _, summary = self.run_script([self.entry()])
        self.assertEqual(0, rc)
        self.assertEqual('redirected', summary['failed'][0]['reason'])
        self.assertEqual(['/%s/tar.gz/%s' % (REPO, SHA)], self.server.requests)
        self.assertEqual([], self.leftovers())

    def test_budget_runs_out_mid_transfer(self):
        self.route(REPO, SHA, (200, {}, good()))
        now = [0]
        real_read1 = http.client.HTTPResponse.read1

        def read1(response, *args):
            # The transfer, not a count of clock reads, is what uses up the budget: it is gone as
            # soon as the first chunk has been delivered.
            chunk = real_read1(response, *args)
            now[0] = 1000
            return chunk

        with mock.patch.object(caa, 'time', mock.Mock(monotonic=lambda: now[0])), \
                mock.patch.object(http.client.HTTPResponse, 'read1', read1), \
                mock.patch.object(caa, 'CHUNK_SIZE', 16):
            with self.assertRaises(caa.FetchError) as raised:
                caa.fetch(self.entry(), self.dest, self.base_url(), 100)
        self.assertEqual('budget', str(raised.exception))
        self.assertEqual([], self.leftovers())
        self.assertFalse(os.path.exists(caa.archive_path(self.dest, REPO, SHA)))

    def test_budget_is_enforced_against_a_server_that_trickles(self):
        body = good()
        release = threading.Event()
        now = [0]
        state = {'timed_out': False}

        def trickle(handler):
            # Pause so the client is waiting in its first read, then spend the budget and send a few
            # bytes of a body that will never finish. A read that waits for a full chunk would
            # block here until the wait below gives up.
            release.wait(0.2)
            now[0] = 1000
            try:
                handler.wfile.write(body[:8])
                if not release.wait(3):
                    state['timed_out'] = True
            except OSError:
                pass

        self.route(REPO, SHA, (200, {'Content-Length': str(len(body))}, trickle))
        with mock.patch.object(caa, 'time', mock.Mock(monotonic=lambda: now[0])):
            with self.assertRaises(caa.FetchError) as raised:
                caa.fetch(self.entry(), self.dest, self.base_url(), 100)
        release.set()
        self.assertEqual('budget', str(raised.exception))
        self.assertFalse(state['timed_out'], 'The deadline was only noticed once the server gave up.')
        self.assertEqual([], self.leftovers())
        self.assertFalse(os.path.exists(caa.archive_path(self.dest, REPO, SHA)))

    def test_corrupt_cached_file_is_replaced(self):
        final = caa.archive_path(self.dest, REPO, SHA)
        os.makedirs(os.path.dirname(final))
        with open(final, 'wb') as f:
            f.write(b'not a tarball')
        self.route(REPO, SHA, (200, {}, good()))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual({'cached': 1, 'present': 0, 'failed': []}, summary)
        caa.verify(final, SHA)

    def test_stale_temporary_from_another_process_is_removed(self):
        directory = os.path.dirname(caa.archive_path(self.dest, REPO, SHA))
        os.makedirs(directory)
        stale = os.path.join(directory, '.tmp-%s-1' % SHA)
        other = os.path.join(directory, '.tmp-%s-1' % SHA2)
        for path in (stale, other):
            with open(path, 'wb') as f:
                f.write(b'partial')
        self.route(REPO, SHA, (200, {}, good()))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual(1, summary['cached'])
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(other), 'only temporaries for the archive being fetched are ours to remove')

    def test_owner_with_underscore_is_invalid(self):
        rc, _, summary = self.run_script([self.entry('a_b/c', SHA)])
        self.assertEqual(0, rc)
        self.assertEqual(['invalid'], [f['reason'] for f in summary['failed']])
        self.assertEqual([], self.server.requests)

    def test_script_runs_as_ansible_runs_it(self):
        # The script module copies the file and runs it as a program, so main() is not enough.
        with open(self.manifest, 'w') as f:
            f.write('[]')
        script = os.path.join(REPO_ROOT, 'ansible', 'files', 'cache-action-archives.py')
        result = subprocess.run([sys.executable, script, self.manifest, self.dest], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('{"cached": 0, "present": 0, "failed": []}\n', result.stdout)

    def test_throttle_then_success(self):
        self.route(REPO, SHA, (429, {'Retry-After': '0'}, b''), (200, {}, good()))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual(1, summary['cached'])
        self.assertEqual(2, len(self.server.requests))
        self.sleep.assert_called_once_with(0)

    def test_retry_after_is_capped_and_default_applies(self):
        self.route(REPO, SHA, (503, {'Retry-After': '9999'}, b''), (500, {}, b''), (200, {}, good()))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual(1, summary['cached'])
        self.assertEqual([mock.call(60), mock.call(15)], self.sleep.call_args_list)

    def test_throttle_gives_up_after_two_retries(self):
        self.route(REPO, SHA, (429, {'Retry-After': '0'}, b''))
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual('HTTP 429', summary['failed'][0]['reason'])
        self.assertEqual(3, len(self.server.requests))

    def test_not_found(self):
        _, _, summary = self.run_script([self.entry()])
        self.assertEqual('HTTP 404', summary['failed'][0]['reason'])
        self.assertEqual(1, len(self.server.requests))
        self.assertEqual([], self.leftovers())

    def test_invalid_entries_are_rejected_without_a_request(self):
        entries = [self.entry('../x', SHA), self.entry('actions/checkout', 'abc'), self.entry('a/b/c', SHA),
                   self.entry('../..', SHA), 'nonsense']
        rc, _, summary = self.run_script(entries)
        self.assertEqual(0, rc)
        self.assertEqual(5, len(summary['failed']))
        self.assertEqual({'invalid'}, {f['reason'] for f in summary['failed']})
        self.assertEqual([], self.server.requests)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'x')))

    def test_zero_budget_fails_everything(self):
        self.route(REPO, SHA, (200, {}, good()))
        rc, _, summary = self.run_script([self.entry()], '--budget', '0')
        self.assertEqual(0, rc)
        self.assertEqual([{'repository': REPO, 'sha': SHA, 'reason': 'budget'}], summary['failed'])
        self.assertEqual([], self.server.requests)

    def test_summary_is_one_json_line(self):
        self.route(REPO, SHA, (200, {}, good()))
        _, out, _ = self.run_script([self.entry(), self.entry('x/y', SHA2)])
        self.assertEqual(1, len(out.splitlines()))
        json.loads(out)

    def test_unusable_arguments_exit_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, caa.main([os.path.join(self.tmp.name, 'missing.json'), self.dest]))
            blocker = os.path.join(self.tmp.name, 'file')
            open(blocker, 'w').close()
            with open(self.manifest, 'w') as f:
                f.write('[]')
            self.assertEqual(2, caa.main([self.manifest, os.path.join(blocker, 'sub')]))

    def test_verification_exception_removes_temporary_file(self):
        self.route(REPO, SHA, (200, {}, good()))
        with mock.patch.object(caa, 'verify', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                caa.fetch(self.entry(), self.dest, self.base_url(), caa.time.monotonic() + 60)
        self.assertEqual([], self.leftovers())


if __name__ == '__main__':
    unittest.main()
