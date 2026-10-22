#!/usr/bin/env python3

"""Tests for tools/start-test-target.py's upload transfer retry.

The engine refuses an upload transfer with a 409 "There is no active
Host in the Data Center" while its cached storage domain report for the
host is older than the storage domain itself, and deletes the disk it
was handed when it does (shakenfist/kerbside#309). _start_upload_transfer
must retry that refusal with a fresh disk, and only that refusal.

The script imports ovirtsdk4 at module scope, which is deliberately not a
test dependency: it runs on the oVirt engine against the engine's own
system-wide SDK. Stub the import, giving the stub a real exception class
so the script's except clauses work.
"""

import sys
import unittest
from unittest import mock

from tests.helpers import load_script


class _SdkError(Exception):
    pass


_sdk = mock.Mock()
_sdk.Error = _SdkError
with mock.patch.dict(sys.modules, {'ovirtsdk4': _sdk, 'ovirtsdk4.types': _sdk.types}):
    stt = load_script('tools/start-test-target.py', 'start_test_target')


NO_HOST = ('Fault reason is "Operation Failed". Fault detail is "[Cannot transfer '
           'Virtual Disk. There is no active Host in the Data Center.]". HTTP '
           'response code is 409.')


class _Disk(object):
    def __init__(self, id):
        self.id = id


class StartUploadTransferTests(unittest.TestCase):
    def setUp(self):
        self.disks = []

        def create_disk(*args):
            disk = _Disk('disk-%d' % len(self.disks))
            self.disks.append(disk)
            return disk, mock.Mock()

        patcher = mock.patch.object(stt, '_create_upload_disk', side_effect=create_disk)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.system_service = mock.Mock()
        self.transfers = self.system_service.image_transfers_service.return_value

    def _start(self):
        with mock.patch.object(stt.time, 'sleep'):
            return stt._start_upload_transfer(self.system_service, 100, 200, 'local-storage', 60)

    def test_first_attempt_succeeds(self):
        self.transfers.add.return_value = _Disk('transfer-0')

        disk, _, transfer_service = self._start()

        self.assertEqual('disk-0', disk.id)
        self.assertEqual(1, len(self.disks))
        self.transfers.image_transfer_service.assert_called_once_with('transfer-0')
        self.assertEqual(self.transfers.image_transfer_service.return_value, transfer_service)

    def test_no_transfer_host_retries_with_a_new_disk(self):
        self.transfers.add.side_effect = [_SdkError(NO_HOST), _SdkError(NO_HOST), _Disk('transfer-2')]

        disk, _, _ = self._start()

        # The engine deleted each refused disk, so every attempt needs its
        # own, and the transfer that started must be for the last one.
        self.assertEqual(['disk-0', 'disk-1', 'disk-2'], [d.id for d in self.disks])
        self.assertEqual('disk-2', disk.id)
        self.transfers.image_transfer_service.assert_called_once_with('transfer-2')

    def test_other_errors_are_not_retried(self):
        self.transfers.add.side_effect = _SdkError('Fault detail is "[Disk is locked]". HTTP response code is 409.')

        with self.assertRaises(_SdkError):
            self._start()
        self.assertEqual(1, len(self.disks))

    def test_no_transfer_host_gives_up_after_the_retry_window(self):
        self.transfers.add.side_effect = _SdkError(NO_HOST)
        clock = iter([0, 10, stt.TRANSFER_HOST_RETRY_SECS + 1])

        with mock.patch.object(stt.time, 'time', side_effect=lambda: next(clock)):
            with self.assertRaises(_SdkError):
                self._start()
        self.assertEqual(2, len(self.disks))


if __name__ == '__main__':
    unittest.main()
