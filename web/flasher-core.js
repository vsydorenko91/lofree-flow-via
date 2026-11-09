// Flow84 (OE915 / CX53710) firmware container + bootloader protocol.
// Pure logic, no DOM: the page uses it with WebHID, tests use it with a fake device.
// Mirrors tools/oe915.py (the Python implementation is covered by the test-suite).

export const HEAD = 0x2000;
export const PREPARE = 0x1d7;
export const DOWNLOAD = 0x217;
export const FLASH_BASE = 0x2000;
export const MAX_IMAGE_SIZE = 0x27e00;
export const STOCK_SHA256 = '7c061d9ce66c0b45e7d9f02e5d467af78cd537a88529b90fccb5dbf6dbccc7de';
export const NORMAL = { vendorId: 0x05ac, productId: 0x024f, usagePage: 0xff02, usage: 2, product: 'Flow84@Lofree' };
export const BOOT = { vendorId: 0x3554, productId: 0xf800 };
// Stock "enter update mode" output report (id 8, 16 data bytes; the last byte completes the 0x55 sum).
export const RESET_REPORT_ID = 8;
export const RESET_DATA = Uint8Array.from([0x0d, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x40]);

const u32le = (d, o) => (d[o] | d[o + 1] << 8 | d[o + 2] << 16 | d[o + 3] << 24) >>> 0;
const u32be = (d, o) => (d[o] << 24 | d[o + 1] << 16 | d[o + 2] << 8 | d[o + 3]) >>> 0;

let crcTable;
export function crc32(bytes) {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[i] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = crcTable[(c ^ bytes[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

export function headerChecksum(head) {
  const n = u32le(head, 4);
  if (n >= HEAD) return 0;
  let sum = 0;
  for (let i = 8; i < n; i++) sum = (sum + head[i]) >>> 0;
  return (0x55555555 - sum) >>> 0;
}

const ascii = (d, o, n) => String.fromCharCode(...d.subarray(o, o + n)).split('\0')[0];

/** Validate a container and return its upload reports (49 bytes each: id 6 + 48 data). */
export function buildReports(data) {
  if (data.length > MAX_IMAGE_SIZE) throw new Error('image exceeds the bootloader size limit');
  if (data.length <= HEAD) throw new Error('invalid firmware container');
  const head = data.subarray(0, HEAD), payload = data.subarray(HEAD);
  const crc = (crc32(payload) ^ 0xffffffff) >>> 0;   // the bootloader expects the complement
  if (ascii(head, 0x17, 17) !== 'ComUsbUpgradeFile' || ascii(head, 0x57, 7) !== 'CX53710' ||
      u32le(head, 12) !== 0 || u32le(head, 0) !== headerChecksum(head) ||
      u32le(head, 8) !== payload.length || u32be(head, PREPARE + 7) !== payload.length ||
      u32be(head, PREPARE + 11) !== crc) throw new Error('invalid or unsupported OE915 firmware container');
  for (const [off, op] of [[PREPARE, 0xb0], [DOWNLOAD, 0xb1]]) {
    if (head[off] !== 49 || head[off + 1] !== 0 || head[off + 2] !== 6 || head[off + 3] !== op)
      throw new Error('unsupported HID command template');
  }
  if (u32be(head, DOWNLOAD + 7) !== FLASH_BASE) throw new Error('unexpected flash start address');
  const reports = [Uint8Array.from(head.subarray(PREPARE + 2, PREPARE + 51))];
  for (let off = 0; off < payload.length; off += 32) {
    const n = Math.min(32, payload.length - off);
    const r = new Uint8Array(49).fill(0xff, 17);
    r.set([6, 0xb1, off + n === payload.length ? 0xc1 : 0xc0, n, 0]);
    r[5] = (FLASH_BASE + off) >>> 24; r[6] = (FLASH_BASE + off) >>> 16 & 255;
    r[7] = (FLASH_BASE + off) >>> 8 & 255; r[8] = (FLASH_BASE + off) & 255;
    r.fill(0, 9, 17);
    r.set(payload.subarray(off, off + n), 17);
    reports.push(r);
  }
  return reports;
}

/** Interpret one bootloader input report (id + data as one array, like the HID API). */
export function decodeBootResponse(r) {
  if (r.length === 3 && r[0] === 0xb0) return { kind: 'other' };
  if (r.length !== 49 || r[0] !== 6) throw new Error('unexpected bootloader report');
  if (r[1] === 0xb1) return { kind: 'echo', report: r };
  if (r[1] === 0xb0) return { kind: 'prepare' };
  if (r[1] === 0x5a) throw new Error('bootloader reported an error');
  if (r[1] === 0x5b && r[2] === 0xb5 && [1, 2, 5, 136].includes(r[3])) return { kind: 'state', state: r[3], detail: r[4] };
  throw new Error('unknown bootloader response');
}

const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

/**
 * Drive the bootloader. `transport.send(report49)` writes, `transport.receive(ms)` resolves with
 * the next input report as one array (id first) or rejects on timeout. State numbers: 1/2 erase,
 * 5 image check, 136 success.
 */
export async function upload(transport, reports, log = () => {}, progress = () => {}) {
  const next = async () => {
    const e = decodeBootResponse(await transport.receive(10000));
    if (e.kind === 'state') log(`bootloader state ${e.state} (${e.detail})`);
    return e;
  };
  await transport.send(reports[0]);
  log('Erasing…');
  for (;;) {
    const e = await next();
    if (e.kind !== 'state') continue;
    if ((e.state === 1 || e.state === 2) && e.detail === 1) break;
    if (e.state !== 1 && e.state !== 2) throw new Error('unexpected bootloader state before upload');
  }
  const count = reports.length - 1;
  for (let i = 1; i <= count; i++) {
    const packet = reports[i];
    await transport.send(packet);
    let bad = 0;
    for (;;) {
      const e = await next();
      if (e.kind === 'echo') {
        if (same(e.report, packet)) break;
        if (++bad > 8) throw new Error(`data echo mismatch at packet ${i}`);
        await transport.send(packet);
      } else if (e.kind === 'state' && e.state === 136) {
        if (i !== count) throw new Error('bootloader reported success before the whole image was sent');
        progress(1);
        return;
      } else if (e.kind === 'state' && e.state === 5 && i !== count) {
        throw new Error('bootloader checked the image before the upload finished');
      }
    }
    if (i % 64 === 0 || i === count) progress(i / count);
  }
  for (;;) {
    const e = await next();
    if (e.kind === 'state' && e.state === 136) return;
  }
}
