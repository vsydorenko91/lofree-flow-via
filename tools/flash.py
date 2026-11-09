#!/usr/bin/env python3
"""USB flasher (hidapi): install a built image or restore the stock firmware.

`inspect` and `validate` never send anything. `flash` and `restore-stock` write
the keyboard's application flash; read docs/RECOVERY.md first.
"""
import argparse, hashlib, json, math, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oe915
from oe915 import (FlashError, NORMAL_IDS, BOOT_IDS, BOOT_DESCRIPTOR, STOCK_RESET_COMMAND,
                   STOCK_NORMAL_DESCRIPTOR)

ROOT = Path(__file__).resolve().parents[1]


def validate(image, manifest):
    """Image must equal the authentic stock plus exactly the manifest's changes."""
    data = Path(image).read_bytes()
    m = json.loads(Path(manifest).read_text())
    digest = hashlib.sha256(data).hexdigest()
    if digest != m['sha256'] or m['stock_sha256'] != oe915.STOCK_SHA:
        raise FlashError('image does not match its manifest')
    stock = oe915.load_stock()
    if not len(stock) < len(data) <= oe915.MAX_IMAGE_SIZE or len(data) != m['new_size']:
        raise FlashError('unexpected image length')
    rebuilt = bytearray(stock)
    for c in m['changes']:
        at, before, after = int(c['address'], 16), bytes.fromhex(c['before']), bytes.fromhex(c['after'])
        if at < 0x2000 or at + len(before) > len(stock) or len(before) != len(after) \
                or rebuilt[at:at + len(before)] != before:
            raise FlashError(f'invalid declared change at {at:#x}')
        rebuilt[at:at + len(before)] = after
    rebuilt.extend(data[len(stock):])
    if oe915.seal(rebuilt) != data:
        raise FlashError('image differs outside the declared changes')
    for at, n in ((0x227cc, 77), (0x22819, 106)):        # vendor keyboard descriptors must stay intact
        if data[at:at + n] != stock[at:at + n]:
            raise FlashError('keyboard descriptor changed')
    list(oe915.upgrade_reports(data))
    return data


def find(hid, ids):
    found = {}
    for d in hid.enumerate(*ids):
        if d.get('usage_page') == 0xff02 and d.get('usage') == 2:
            if ids == NORMAL_IDS and d.get('product_string') != 'Flow84@Lofree':
                continue
            found[d['path']] = d
    if len(found) > 1:
        raise FlashError('multiple matching interfaces; unplug other keyboards')
    return next(iter(found.values()), None)


class Transport:
    def __init__(self, hid, info, ids, descriptor):
        self.ids = ids
        self.device = hid.device()
        try:
            self.device.open_path(info['path'])
            if bytes(self.device.get_report_descriptor()) != descriptor:
                raise FlashError('report descriptor differs from the known one; no writes allowed')
        except BaseException:
            self.device.close()
            raise

    def close(self):
        self.device.close()

    def send(self, report):
        if self.ids == NORMAL_IDS:
            if len(report) != 17 or report[:2] != b'\x08\x0d' or sum(report) & 255 != 0x55:
                raise FlashError('only the known enter-update command may be sent in normal mode')
        elif len(report) != 49 or report[0] != 6 or report[1] not in (0xb0, 0xb1):
            raise FlashError('unsupported bootloader packet')
        if self.device.write(report) != len(report):
            raise FlashError('short HID write')

    def receive(self, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise FlashError('bootloader response timeout')
        report = bytes(self.device.read(4096, max(1, math.ceil(left * 1000))))
        if not report:
            raise FlashError('bootloader response timeout')
        return report


def wait_for(hid, ids, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        info = find(hid, ids)
        if info:
            return info
        time.sleep(.1)
    raise FlashError(f'USB {ids[0]:04x}:{ids[1]:04x} did not appear')


def run(hid, command, data=None):
    stock = oe915.load_stock()
    normal_descriptor = stock[slice(*STOCK_NORMAL_DESCRIPTOR)]
    if command == 'inspect':
        for ids, desc in ((NORMAL_IDS, normal_descriptor), (BOOT_IDS, BOOT_DESCRIPTOR)):
            info = find(hid, ids)
            if info:
                Transport(hid, info, ids, desc).close()
                print(f'Verified {ids[0]:04x}:{ids[1]:04x}; descriptor matches; nothing sent.')
        return
    data = stock if command == 'restore-stock' else data
    if data is None:
        raise FlashError('no image selected')
    reports = list(oe915.upgrade_reports(data))
    print('Image SHA-256:', hashlib.sha256(data).hexdigest(), flush=True)
    boot = find(hid, BOOT_IDS)
    if not boot:
        normal = find(hid, NORMAL_IDS)
        if not normal:
            raise FlashError('Flow84 not found; nothing was sent')
        t = Transport(hid, normal, NORMAL_IDS, normal_descriptor)
        try:
            reset = bytearray(stock[slice(*STOCK_RESET_COMMAND)])
            reset[-1] = (0x55 - sum(reset[:-1])) & 255
            print('Entering update mode.', flush=True)
            t.send(reset)
        finally:
            t.close()
        boot = wait_for(hid, BOOT_IDS, 15)
    t = Transport(hid, boot, BOOT_IDS, BOOT_DESCRIPTOR)
    try:
        oe915.upload(t, reports, emit=lambda s: print(s, flush=True))
    finally:
        t.close()
    Transport(hid, wait_for(hid, NORMAL_IDS, 20), NORMAL_IDS, normal_descriptor).close()
    print('Keyboard is back on its normal USB identity.', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='command', required=True)
    s.add_parser('inspect')
    s.add_parser('restore-stock')
    for name in ('validate', 'flash'):
        q = s.add_parser(name)
        q.add_argument('--image', type=Path, default=ROOT / 'out/flow84-via.bin')
        q.add_argument('--manifest', type=Path, default=ROOT / 'out/manifest.json')
    a = p.parse_args()
    try:
        data = validate(a.image, a.manifest) if a.command in ('validate', 'flash') else None
        if a.command == 'validate':
            print('Image is stock plus its declared changes; container checks pass. Nothing was sent.')
            return 0
        import hid
        run(hid, a.command, data)
    except (FlashError, ValueError, OSError, KeyError) as e:
        print('STOPPED:', e, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
