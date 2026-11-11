// Flow84 VIA profile backup / restore over Raw HID (usage page 0xff60, usage 0x61, 32-byte reports).
// Pure logic, no DOM: the page uses it with WebHID, tests drive it against the emulated firmware.
// A profile is what "Save to keyboard" stores: the keymap, the macro buffer and four settings.

export const RAW = { usagePage: 0xff60, usage: 0x61 };
export const FORMAT = 'flow84-via-profile';
const KEYMAP_BYTES = 1056;              // 4 layers x 6 rows x 22 columns x 2 bytes
const CHUNK = 28;
const SETTINGS = { backlight: 6, sideColor: 10, sideEffect: 11, tapTerm: 14 };   // custom value ids, channel 0

export class Rejected extends Error {}

const hex = (bytes) => [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
const unhex = (text, size, what) => {
  if (typeof text !== 'string' || text.length !== size * 2 || /[^0-9a-f]/.test(text)) throw new Error(`backup: bad ${what}`);
  return Uint8Array.from(text.match(/../g) || [], h => parseInt(h, 16));
};
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/** One request / one reply. `transport.send(bytes32)`, `transport.receive(ms)` -> bytes32 or rejects on timeout. */
async function ask(transport, bytes, ms = 2000) {
  const q = new Uint8Array(32); q.set(bytes);
  await transport.send(q);
  const end = Date.now() + ms;
  for (;;) {
    const r = await transport.receive(Math.max(1, end - Date.now()));
    if (r.length !== 32) continue;
    if (r[0] === 0xff && q.subarray(1).every((v, i) => v === r[i + 1])) throw new Rejected('the keyboard rejected a request');
    if (r[0] === q[0]) return r;        // anything else is not the answer to this request
    if (Date.now() >= end) throw new Error('unexpected reply from the keyboard');
  }
}

/** Wait until the VIA firmware answers (it starts a moment after USB is up, once no key is held). */
export async function waitReady(transport, ms = 10000) {
  const end = Date.now() + ms;
  for (;;) {
    try {
      const r = await ask(transport, [1], 500);
      if ((r[1] << 8 | r[2]) !== 13) throw new Error('this is not the Flow84 VIA firmware');
      if ((await ask(transport, [0x11]))[1] !== 4) throw new Error('unexpected layer count');
      return;
    } catch (e) {
      if (/not the Flow84|layer count/.test(e.message) || Date.now() >= end) throw e;
      await sleep(250);
    }
  }
}

async function readBuffer(transport, command, size) {
  const out = new Uint8Array(size);
  for (let off = 0; off < size; off += CHUNK) {
    const n = Math.min(CHUNK, size - off);
    out.set((await ask(transport, [command, off >> 8, off & 255, n])).subarray(4, 4 + n), off);
  }
  return out;
}

const macroSize = async (transport) => { const r = await ask(transport, [0x0d]); return r[1] << 8 | r[2]; };

/** Read the live profile (including unsaved edits). */
export async function readProfile(transport) {
  await waitReady(transport);
  const v = await ask(transport, [2, 4]);
  const settings = {};
  for (const [name, id] of Object.entries(SETTINGS)) settings[name] = (await ask(transport, [8, 0, id]))[3];
  return {
    format: FORMAT, version: 1,
    firmware: `${v[3]}.${v[4]}.${v[5]}`,
    keymap: hex(await readBuffer(transport, 0x12, KEYMAP_BYTES)),
    macros: hex(await readBuffer(transport, 0x0e, await macroSize(transport))),
    settings,
  };
}

/** Throws unless `profile` is a well-formed backup. Returns its decoded parts. */
export function checkProfile(profile) {
  if (!profile || profile.format !== FORMAT || profile.version !== 1) throw new Error('backup: not a Flow84 VIA profile');
  if (typeof profile.macros !== 'string' || profile.macros.length % 2 || profile.macros.length > 2 * 1024) throw new Error('backup: bad macros');
  const settings = {};
  for (const name of Object.keys(SETTINGS)) {
    const value = profile.settings && profile.settings[name];
    if (value === undefined) continue;
    if (!Number.isInteger(value) || value < 0 || value > 255) throw new Error(`backup: bad setting ${name}`);
    settings[name] = value;
  }
  return { keymap: unhex(profile.keymap, KEYMAP_BYTES, 'keymap'), macros: unhex(profile.macros, profile.macros.length / 2, 'macros'), settings };
}

/**
 * Write a backup to the keyboard and press "Save to keyboard".
 * Everything is written to RAM and read back first; flash is written only when the keyboard holds
 * exactly the backup, so a failure leaves the stored profile as it was (replug to drop the RAM edits).
 */
export async function writeProfile(transport, profile, log = () => {}) {
  const want = checkProfile(profile);
  await waitReady(transport);
  const size = await macroSize(transport);
  const macros = new Uint8Array(size);
  if (want.macros.subarray(size).some(b => b)) throw new Error('the backup has more macro data than this firmware holds');
  macros.set(want.macros.subarray(0, size));

  // Start from the factory keymap so tap-hold keys can move without exceeding their limit on the way.
  // Firmware 0.2.0 has no reset: it refuses, and the keymap is then written over the current one.
  try { await ask(transport, [6]); } catch (e) { if (!(e instanceof Rejected)) throw e; }

  let current = await readBuffer(transport, 0x12, KEYMAP_BYTES);
  const differs = (off, n) => want.keymap.subarray(off, off + n).some((v, i) => v !== current[off + i]);
  for (let pass = 0; pass < 3; pass++) {      // a held key makes the keyboard refuse edits: retry
    let refused = 0;
    for (let off = 0; off < KEYMAP_BYTES; off += CHUNK) {
      if (!differs(off, CHUNK)) continue;
      try { await ask(transport, [0x13, off >> 8, off & 255, CHUNK, ...want.keymap.subarray(off, off + CHUNK)]); continue; }
      catch (e) { if (!(e instanceof Rejected)) throw e; }
      for (let i = off; i < off + CHUNK; i += 2) {      // find the keys of this chunk that can be written
        if (!differs(i, 2)) continue;
        const key = i >> 1;
        try { await ask(transport, [5, Math.floor(key / 132), Math.floor(key % 132 / 22), key % 22, want.keymap[i], want.keymap[i + 1]]); }
        catch (e) { if (!(e instanceof Rejected)) throw e; refused++; }
      }
    }
    current = await readBuffer(transport, 0x12, KEYMAP_BYTES);
    if (!refused) break;
    log(`${refused} key(s) were refused, trying again…`);
    await sleep(400);
  }

  for (let off = 0; off < size; off += CHUNK) {
    const n = Math.min(CHUNK, size - off);
    await ask(transport, [0x0f, off >> 8, off & 255, n, ...macros.subarray(off, off + n)]);
  }
  for (const [name, value] of Object.entries(want.settings)) await ask(transport, [7, 0, SETTINGS[name], value]);

  const now = await readProfile(transport);
  const keys = [];
  for (let i = 0; i < KEYMAP_BYTES; i += 2) if (now.keymap.slice(i * 2, i * 2 + 4) !== profile.keymap.slice(i * 2, i * 2 + 4)) keys.push(i >> 1);
  if (keys.length) throw new Error(`${keys.length} key(s) could not be restored (release all keys and try again)`);
  if (now.macros !== hex(macros)) throw new Error('the macros did not read back');
  for (const [name, value] of Object.entries(want.settings)) if (now.settings[name] !== value) throw new Error(`setting ${name} did not read back`);

  log('Saving to the keyboard…');
  try { await ask(transport, [7, 0, 1, 1], 15000); }
  catch (e) { throw e instanceof Rejected ? new Error('the keyboard could not save the profile (it may not fit)') : e; }
  if ((await ask(transport, [8, 0, 13]))[3]) throw new Error('the keyboard still reports unsaved changes');
}
