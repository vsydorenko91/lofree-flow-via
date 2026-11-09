"""Flash and restore rehearsals over a fake HID layer; no real USB device is opened."""
from pathlib import Path
import hashlib, unittest
from unittest.mock import patch
import flash as fl
import oe915
from oe915 import FlashError, NORMAL_IDS, BOOT_IDS, BOOT_DESCRIPTOR

REPORTS_SHA = 'e0773c09ef14ad671ec57e332a6fae445b7df7510c0db315cc55995948481d7c'


def state(n):
    return bytes([6, 0x5b, 0xb5, n, 1]) + bytes(44)


class FakeHID:
    def __init__(self, start_boot=False, bad_echo=False):
        self.stock = oe915.load_stock()
        self.reports = list(oe915.upgrade_reports(self.stock))
        self.normal_descriptor = self.stock[slice(*oe915.STOCK_NORMAL_DESCRIPTOR)]
        self.boot = start_boot; self.bad_echo = bad_echo
        self.writes = []; self.queue = []; self.sent = 0

    def enumerate(self, *ids):
        if ids != (BOOT_IDS if self.boot else NORMAL_IDS):
            return []
        return [dict(path=b'flow', usage_page=0xff02, usage=2, product_string='Flow84@Lofree')]

    def device(self):
        owner = self

        class Device:
            def open_path(self, path): pass
            def close(self): pass
            def get_report_descriptor(self):
                return BOOT_DESCRIPTOR if owner.boot else owner.normal_descriptor
            def write(self, report):
                report = bytes(report); owner.writes.append(report)
                if not owner.boot:
                    owner.boot = True
                elif report[1] == 0xb0:
                    owner.queue.append(state(2))
                else:
                    owner.sent += 1
                    owner.queue.append(bytes([6, 0xb1]) + bytes(47) if owner.bad_echo else report)
                    if owner.sent == len(owner.reports) - 1 and not owner.bad_echo:
                        owner.queue.extend([state(5), state(136)])
                return len(report)
            def read(self, n, timeout):
                if not owner.queue:
                    return []
                reply = owner.queue.pop(0)
                if reply == state(136):
                    owner.boot = False
                return reply
        return Device()


class Recovery(unittest.TestCase):
    def test_stock_packet_stream_matches_the_vendor_updater(self):
        reports = list(oe915.upgrade_reports(oe915.load_stock()))
        self.assertEqual(hashlib.sha256(b''.join(reports)).hexdigest(), REPORTS_SHA)

    def test_restore_from_working_app_or_already_reachable_bootloader(self):
        for boot in (False, True):
            with self.subTest(start_boot=boot):
                hid = FakeHID(start_boot=boot)
                with patch('builtins.print'):
                    fl.run(hid, 'restore-stock')
                packets = hid.writes if boot else hid.writes[1:]
                self.assertEqual(packets, hid.reports)
                if not boot:
                    self.assertEqual(hid.writes[0][:2], b'\x08\x0d')
                    self.assertEqual(sum(hid.writes[0]) & 255, 0x55)
                self.assertFalse(hid.boot)

    def test_bad_echo_stops_upload_without_reporting_success(self):
        hid = FakeHID(start_boot=True, bad_echo=True)
        with patch('builtins.print'), self.assertRaisesRegex(FlashError, 'echo mismatch'):
            fl.run(hid, 'restore-stock')
        self.assertTrue(hid.boot)
        self.assertLess(len(hid.writes), len(hid.reports))


if __name__ == '__main__':
    unittest.main()
