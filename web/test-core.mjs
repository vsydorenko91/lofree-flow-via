// Run: node web/test-core.mjs  (needs tools/reports.json from tools/make_web.py --reports)
import { readFileSync } from 'node:fs';
import { buildReports, upload, sha256Hex, STOCK_SHA256 } from './flasher-core.js';
import assert from 'node:assert/strict';

const dir = new URL('./firmware/', import.meta.url);
const stock = new Uint8Array(readFileSync(new URL('stock.bin', dir)));
assert.equal(await sha256Hex(stock), STOCK_SHA256, 'stock hash');
const reports = buildReports(stock);
const all = new Uint8Array(reports.reduce((n, r) => n + r.length, 0));
let o = 0; for (const r of reports) { all.set(r, o); o += r.length; }
// Same packet stream as the vendor updater / tools/oe915.py (checked in tests/test_recovery.py).
assert.equal(await sha256Hex(all), 'e0773c09ef14ad671ec57e332a6fae445b7df7510c0db315cc55995948481d7c', 'report stream');

const state = (n) => Uint8Array.from([6, 0x5b, 0xb5, n, 1, ...new Array(44).fill(0)]);
function fake({ badEcho = false } = {}) {
  const queue = []; let sent = 0; const writes = [];
  return {
    writes,
    async send(r) {
      writes.push(r);
      if (r[1] === 0xb0) queue.push(state(2));
      else {
        sent++;
        queue.push(badEcho ? Uint8Array.from([6, 0xb1, ...new Array(47).fill(0)]) : r);
        if (sent === reports.length - 1 && !badEcho) queue.push(state(5), state(136));
      }
    },
    async receive() { if (!queue.length) throw new Error('timeout'); return queue.shift(); },
  };
}
const t = fake();
let last = 0;
await upload(t, reports, () => {}, (p) => { last = p; });
assert.equal(t.writes.length, reports.length); assert.equal(last, 1);
await assert.rejects(upload(fake({ badEcho: true }), reports), /echo mismatch/);

for (const name of ['flow84-via.bin']) {
  const img = new Uint8Array(readFileSync(new URL(name, dir)));
  const rs = buildReports(img);
  assert.ok(rs.length > 4000, name);
}
const corrupt = Uint8Array.from(stock); corrupt[0x3000] ^= 1;
assert.throws(() => buildReports(corrupt), /invalid/);
console.log('flasher-core: all checks passed');
