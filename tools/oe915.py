"""Flow84 (OE915 / CX53710) firmware container and bootloader protocol.

Container (little endian): a fixed 0x2000-byte header followed by the payload.
  0x00 u32 header checksum = 0x55555555 - sum(header[8:header_length])
  0x04 u32 header_length   0x08 u32 payload length   0x0c u32 next file (0)
  0x17 "ComUsbUpgradeFile"  0x57 "CX53710"
  0x1d7 prepare command template: [7:11] payload length (big endian),
        [11:15] ~CRC32(payload) (big endian)
  0x217 data command template:    [7:11] flash start address (big endian)
Upload uses 49-byte HID reports (id 6): one prepare/erase report (0xB0), then
32-byte data chunks (0xB1, 0xC0 = more, 0xC1 = last) at flash address 0x2000+.
No function here touches hardware.
"""
import hashlib, struct, time, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOCK_PATH = ROOT / 'stock' / 'CompxUpgradeBinFile.bin'
STOCK_SHA = '7c061d9ce66c0b45e7d9f02e5d467af78cd537a88529b90fccb5dbf6dbccc7de'
PAYLOAD_SHA = '783b524191d4a165371600b66b528d9348e3152bb12ce94d156f2c9e35e20b7f'
HEAD = 0x2000
PREPARE, DOWNLOAD = 0x1d7, 0x217
FLASH_BASE = 0x2000
# Whole image (header + payload) accepted by the bootloader: erase window ends at 0x27e00.
MAX_IMAGE_SIZE = 0x27e00
NORMAL_IDS = (0x05ac, 0x024f)
BOOT_IDS = (0x3554, 0xf800)
BOOT_DESCRIPTOR = bytes.fromhex(
    '0602ff0902a1018506150026ff00750895300902810009029100c0'
    '050c0901a10185b01500263c0219002a3c02751095018100c0')
STOCK_RESET_COMMAND = (0x199, 0x1aa)        # enter-update command inside the stock header
STOCK_NORMAL_DESCRIPTOR = (0x22819, 0x22883)   # vendor interface descriptor in the stock payload


class FlashError(Exception):
    pass


def checksum(head):
    n = struct.unpack_from('<I', head, 4)[0]
    return 0 if n >= HEAD else (0x55555555 - sum(head[8:n])) & 0xffffffff


def image_crc(payload):
    return zlib.crc32(payload) ^ 0xffffffff


def seal(data):
    """Recompute length, payload CRC and header checksum of a modified image."""
    data = bytearray(data)
    n = len(data) - HEAD
    struct.pack_into('<I', data, 8, n)
    struct.pack_into('>II', data, PREPARE + 7, n, image_crc(data[HEAD:]))
    struct.pack_into('<I', data, 0, checksum(data[:HEAD]))
    return bytes(data)


def validate_image_size(data):
    if len(data) > MAX_IMAGE_SIZE:
        raise ValueError(f'image exceeds the {MAX_IMAGE_SIZE:#x}-byte bootloader limit')


def load_stock():
    data = STOCK_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != STOCK_SHA:
        raise ValueError('stock firmware SHA-256 mismatch')
    return data


def upgrade_reports(data):
    """Validate a container and yield its upload reports (prepare, then data)."""
    validate_image_size(data)
    head, payload = data[:HEAD], data[HEAD:]
    if len(head) < HEAD or not payload:
        raise ValueError('invalid firmware container')
    length, crc = struct.unpack_from('>II', head, PREPARE + 7)
    if (head[0x17:0x28].rstrip(b'\0') != b'ComUsbUpgradeFile' or head[0x57:0x5e] != b'CX53710'
            or struct.unpack_from('<I', head, 12)[0] != 0
            or struct.unpack_from('<I', head, 0)[0] != checksum(head)
            or struct.unpack_from('<I', head, 8)[0] != len(payload)
            or length != len(payload) or crc != image_crc(payload)):
        raise ValueError('invalid or unsupported OE915 firmware container')
    for off, opcode in ((PREPARE, 0xb0), (DOWNLOAD, 0xb1)):
        if head[off:off + 4] != bytes([49, 0, 6, opcode]):
            raise ValueError('unsupported HID command template')
    base = struct.unpack_from('>I', head, DOWNLOAD + 7)[0]
    if base != FLASH_BASE:
        raise ValueError('unexpected flash start address')
    yield bytes(head[PREPARE + 2:PREPARE + 51])
    for off in range(0, len(payload), 32):
        n = min(32, len(payload) - off)
        report = bytearray(49)
        report[:4] = bytes([6, 0xb1, 0xc1 if off + n == len(payload) else 0xc0, n])
        struct.pack_into('>I', report, 5, base + off)
        report[17:49] = b'\xff' * 32
        report[17:17 + n] = payload[off:off + n]
        yield bytes(report)


def decode_boot_response(report):
    if len(report) == 3 and report[0] == 0xb0:
        return ('other',)
    if len(report) != 49 or report[0] != 6:
        raise FlashError(f'unexpected bootloader report: {report.hex(" ")}')
    op = report[1]
    if op == 0xb1:
        return ('echo', report)
    if op == 0xb0:
        return ('prepare',)
    if op == 0x5a:
        raise FlashError(f'bootloader error: {report.hex(" ")}')
    if op == 0x5b and report[2] == 0xb5 and report[3] in (1, 2, 5, 136):
        return ('state', report[3], report[4])
    raise FlashError(f'unknown bootloader response: {report.hex(" ")}')


def upload(transport, reports, emit=print):
    """Drive the bootloader: erase, then send every chunk and wait for its echo.

    `transport` provides send(report) and receive(deadline) -> report bytes.
    The state numbers (1/2 erase, 5 check, 136 success) come from the bootloader.
    """
    def event(deadline):
        e = decode_boot_response(transport.receive(deadline))
        if e[0] == 'state':
            emit(f'Bootloader state={e[1]} detail={e[2]}')
        return e

    transport.send(reports[0])
    emit('Erasing...')
    deadline = time.monotonic() + 10
    while True:
        e = event(deadline)
        if e[0] == 'state':
            if e[1] in (1, 2) and e[2] == 1:
                break
            if e[1] not in (1, 2):
                raise FlashError('unexpected bootloader state before data upload')
    count = len(reports) - 1
    shown = 10
    for index, packet in enumerate(reports[1:], 1):
        transport.send(packet)
        deadline = time.monotonic() + 10
        bad = 0
        while True:
            e = event(deadline)
            if e[0] == 'echo':
                if e[1] == packet:
                    break
                bad += 1
                if bad > 8:
                    raise FlashError(f'data echo mismatch at packet {index}')
                transport.send(packet)
                deadline = time.monotonic() + 10
            elif e[0] == 'state' and e[1] == 136:
                if index != count:
                    raise FlashError('bootloader reported success before the whole image was sent')
                emit('Bootloader reported success.')
                return
            elif e[0] == 'state' and e[1] == 5 and index != count:
                raise FlashError('bootloader checked the image before the upload finished')
        if index * 100 // count >= shown:
            emit(f'Upload {index * 100 // count}%')
            shown += 10
    deadline = time.monotonic() + 10
    while True:
        e = event(deadline)
        if e[0] == 'state' and e[1] == 136:
            emit('Bootloader reported success.')
            return
