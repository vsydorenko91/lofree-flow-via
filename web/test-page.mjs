// Run: node web/test-page.mjs  (after tools/make_web.py)
// Runs the real <script> of index.html against a stub DOM and a fake WebHID keyboard, to check the
// page's flow: back up the profile, enter the bootloader, upload, wait for the restart, restore.
// The protocol logic itself is covered by test-core.mjs and tests/test_web_profile.py.
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const web = fileURLToPath(new URL('.', import.meta.url));
const html = readFileSync(join(web, 'index.html'), 'utf8');
const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
  .replace(/from '\.\/([\w-]+\.js)'/g, (_, f) => `from '${pathToFileURL(join(web, f)).href}'`);
const ids = [...html.matchAll(/\bid="([\w-]+)"/g)].map(m => m[1]);
const tmp = mkdtempSync(join(tmpdir(), 'flow84-page-'));

// ---- fake keyboard -------------------------------------------------------------------------
const NORMAL = { vendorId: 0x05ac, productId: 0x024f }, BOOT = { vendorId: 0x3554, productId: 0xf800 };
function device(ids, productName, collections, onSend) {
  const listeners = new Set();
  const d = {
    ...ids, productName, collections, opened: false, sent: 0,
    async open() { d.opened = true; }, async close() { d.opened = false; },
    addEventListener(_, fn) { listeners.add(fn); }, removeEventListener(_, fn) { listeners.delete(fn); },
    emit(reportId, bytes) { const b = Uint8Array.from(bytes); queueMicrotask(() => listeners.forEach(fn => fn({ reportId, data: new DataView(b.buffer) }))); },
    async sendReport(id, data) { d.sent++; onSend(d, id, new Uint8Array(data.buffer ? data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) : data)); },
  };
  return d;
}
/** Minimal model of the VIA firmware's Raw HID interface. */
function viaFirmware(version, { silent = false } = {}) {
  const st = { keymap: new Uint8Array(1056), macros: new Uint8Array(192), settings: { 6: 2, 10: 0, 11: 0, 14: 0 }, unsaved: 0, saves: 0 };
  for (let i = 0; i < 528; i++) st.keymap[i * 2 + 1] = 4 + i % 100;
  const factory = st.keymap.slice();
  const dev = device(NORMAL, 'Flow84@Lofree', [{ usagePage: 0xff60, usage: 0x61, outputReports: [{ reportId: 0 }] }], (d, id, q) => {
    if (silent) return;
    const r = q.slice(), off = q[1] << 8 | q[2], n = q[3];
    switch (q[0]) {
      case 1: r[1] = 0; r[2] = 13; break;
      case 0x11: r[1] = 4; break;
      case 2: r.set([0, version[0], version[1], version[2]], 2); break;
      case 0x0d: r[1] = 0; r[2] = 192; break;
      case 0x12: r.set(st.keymap.subarray(off, off + n), 4); break;
      case 0x13: st.keymap.set(q.subarray(4, 4 + n), off); st.unsaved = 1; break;
      case 6: if (version[1] < 3) r[0] = 0xff; else { st.keymap.set(factory); st.unsaved = 1; } break;
      case 0x0e: r.fill(0, 4); r.set(st.macros.subarray(off, off + n), 4); break;
      case 0x0f: st.macros.set(q.subarray(4, 4 + Math.min(n, 192 - off)), off); st.unsaved = 1; break;
      case 8: r[3] = q[2] === 13 ? st.unsaved : st.settings[q[2]] ?? 0; break;
      case 7: if (q[2] === 1) { st.unsaved = 0; st.saves++; } else { st.settings[q[2]] = q[3]; st.unsaved = 1; } break;
      default: r[0] = 0xff;
    }
    d.emit(0, r);
  });
  return Object.assign(dev, { st, factory });
}
const updateInterface = (onReset) => device(NORMAL, 'Flow84@Lofree',
  [{ usagePage: 0xff02, usage: 2, outputReports: [{ reportId: 8 }] }], (d, id) => { if (id === 8) onReset(); });
const state = (n) => [0x5b, 0xb5, n, 1, ...new Array(44).fill(0)];
const bootloader = (onDone) => device(BOOT, 'boot', [], (d, id, data) => {
  if (data[0] === 0xb0) return d.emit(6, state(2));
  d.emit(6, data);
  if (data[1] === 0xc1) { d.emit(6, state(5)); d.emit(6, state(136)); onDone(); }
});

// ---- one page instance -----------------------------------------------------------------------
let run = 0;
async function page({ before, after, image = 'via', storage = {} }) {
  const els = Object.fromEntries(ids.map(id => [id, {
    hidden: /id="(boot|pick|bar|log|backup)"[^>]*hidden|<div id="backup" hidden/.test(html.match(new RegExp(`<[^>]*id="${id}"[^>]*>`))[0]),
    textContent: '', className: '', checked: id === 'keep', disabled: false, style: {}, value: 0, href: '', scrollTop: 0, scrollHeight: 0, onclick: null,
    handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; },
  }]));
  let devices = before ? [updateInterface(() => { bl.resets++; setTimeout(() => { devices = [bl]; }, 50); }), ...(before === 'stock' ? [] : [before])] : [];
  const bl = Object.assign(bootloader(() => setTimeout(() => { devices = after ? [updateInterface(() => {}), after] : []; }, 50)), { resets: 0 });
  if (!before) devices = [bl];
  const matches = (d, f) => d.vendorId === f.vendorId && d.productId === f.productId &&
    (f.usagePage === undefined || d.collections.some(c => c.usagePage === f.usagePage && c.usage === f.usage));
  const g = globalThis;
  g.document = { getElementById: (id) => els[id], querySelector: () => ({ value: image }) };
  Object.defineProperty(g, 'navigator', { configurable: true, value: { hid: {
    getDevices: async () => devices.filter(d => d.granted),
    requestDevice: async ({ filters }) => { const found = devices.filter(d => filters.some(f => matches(d, f))); found.forEach(d => { d.granted = true; }); return found; },
  } } });
  g.localStorage = { getItem: (k) => storage[k] ?? null, setItem: (k, v) => { storage[k] = v; } };
  g.fetch = async (url) => { const b = readFileSync(join(web, url)); return { json: async () => JSON.parse(b), arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) }; };
  const file = join(tmp, `page${++run}.mjs`);
  writeFileSync(file, script);
  await import(pathToFileURL(file).href);
  await new Promise(r => setTimeout(r, 50));                 // manifest loaded
  const wait = async (what, ms = 60000) => {
    for (const end = Date.now() + ms; Date.now() < end;) { if (what()) return; await new Promise(r => setTimeout(r, 20)); }
    throw new Error(`timeout; status: ${els.status.textContent} | ${els['backup-status'].textContent}\n${els.log.textContent}`);
  };
  const flash = async () => {
    els.ack.checked = true; els.ack.handlers.change();
    assert.equal(els.go.disabled, false);
    els.go.handlers.click();
    // Permissions do not survive re-enumeration: the user picks the bootloader, then the keyboard again.
    const clicked = new Set();
    await wait(() => {
      for (const id of ['boot', 'pick']) if (!els[id].hidden && !clicked.has(id)) { clicked.add(id); els[id].onclick(); }
      return ['ok', 'bad'].includes(els.status.className);
    });
    return clicked;
  };
  return { els, bl, storage, flash, wait, devices: () => devices };
}

const profiled = (version) => {
  const fw = viaFirmware(version);
  fw.st.keymap.set([0x22, 0x04], 132); fw.st.keymap.set([0x77, 0x00], 400);
  fw.st.macros.set(Buffer.from('hello\0world'));
  Object.assign(fw.st.settings, { 6: 4, 10: 5, 11: 1, 14: 3 });
  return fw;
};
const same = (a, b) => assert.deepEqual([Buffer.from(a.st.keymap).toString('hex'), Buffer.from(a.st.macros).toString('hex'), a.st.settings],
  [Buffer.from(b.st.keymap).toString('hex'), Buffer.from(b.st.macros).toString('hex'), b.st.settings]);

try {
  // 1. Upgrade from the VIA firmware 0.2.0: backup, flash, automatic restore and save.
  {
    const old = profiled([0, 2, 0]), fresh = viaFirmware([0, 3, 0]);
    const p = await page({ before: old, after: fresh });
    assert.match(p.els['hash-via'].textContent, /^Version \d+\.\d+\.\d+ · SHA-256 /);
    assert.equal(p.els.backup.hidden, true);
    assert.deepEqual([...await p.flash()], ['boot', 'pick']);
    assert.equal(p.els.status.className, 'ok', p.els.status.textContent + p.els.log.textContent);
    assert.match(p.els.status.textContent, /profile restored and saved/);
    same(fresh, old); assert.equal(fresh.st.saves, 1); assert.equal(fresh.st.unsaved, 0);
    assert.equal(p.bl.resets, 1); assert.ok(p.bl.sent > 4000);
    assert.equal(p.els.backup.hidden, false);
    const file = JSON.parse(decodeURIComponent(p.els['backup-file'].href.split(',')[1]));
    assert.equal(file.firmware, '0.2.0'); assert.equal(file.settings.tapTerm, 3);
    assert.ok(p.storage['flow84-via-profile']);

    // 4. Later visit: the stored backup can be restored by hand onto another fresh keyboard.
    const again = viaFirmware([0, 3, 0]);
    const q = await page({ before: again, storage: p.storage });
    assert.equal(q.els.backup.hidden, false);
    q.els['backup-restore'].handlers.click();
    await q.wait(() => q.els['backup-status'].className);
    assert.equal(q.els['backup-status'].className, 'ok', q.els['backup-status'].textContent);
    same(again, old); assert.equal(again.st.saves, 1);
  }
  // 2. First install on a stock keyboard: nothing to back up, plain success.
  {
    const fresh = viaFirmware([0, 3, 0]);
    const p = await page({ before: 'stock', after: fresh });
    await p.flash();
    assert.equal(p.els.status.className, 'ok'); assert.match(p.els.status.textContent, /restarts with VIA support/);
    assert.equal(fresh.st.saves, 0); same(fresh, viaFirmware([0, 3, 0]));
    assert.equal(p.els.backup.hidden, true);
  }
  // 3. The backup fails: nothing is flashed and the keyboard is never put into update mode.
  {
    const p = await page({ before: viaFirmware([0, 2, 0], { silent: true }), after: viaFirmware([0, 3, 0]) });
    await p.flash();
    assert.equal(p.els.status.className, 'bad');
    assert.match(p.els.status.textContent, /could not be backed up/); assert.doesNotMatch(p.els.status.textContent, /bootloader/);
    assert.equal(p.bl.resets, 0); assert.equal(p.bl.sent, 0);
  }
  // 5. Restoring stock: the profile is backed up, never written back.
  {
    const old = profiled([0, 3, 0]);
    const p = await page({ before: old, after: null, image: 'stock' });
    await p.flash();
    assert.equal(p.els.status.className, 'ok'); assert.match(p.els.status.textContent, /Stock firmware restored/);
    assert.equal(p.els.backup.hidden, false);
  }
  // 6. Keyboard already in the bootloader and a stored backup: flashed, then restored.
  {
    const old = profiled([0, 2, 0]), fresh = viaFirmware([0, 3, 0]);
    const first = await page({ before: old, after: null, image: 'stock' }); await first.flash();
    const p = await page({ before: null, after: fresh, storage: first.storage });
    await p.flash();
    assert.equal(p.els.status.className, 'ok', p.els.status.textContent); same(fresh, old);
  }
  console.log('flasher page: all checks passed');
} finally { rmSync(tmp, { recursive: true, force: true }); }
process.exit(0);
