// Test helper for tests/test_web_profile.py: runs profile-core.js against a keyboard that lives in
// the parent process (the emulated firmware). Protocol on stdio, one line each:
//   -> "Q <hex32>"   request      <- "<hex32>" reply, or "-" for no reply
//   -> "OK <json>" / "ERR <message>" when done.
// Usage: node test-profile-bridge.mjs backup | restore <profile.json>
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { readProfile, writeProfile } from './profile-core.js';

const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const hex = (b) => Buffer.from(b).toString('hex');
let pending = null;
const transport = {
  async send(q) { process.stdout.write(`Q ${hex(q)}\n`); pending = lines.next(); },
  async receive() {
    if (!pending) throw new Error('timeout');
    const { value, done } = await pending; pending = null;
    if (done || value === '-') throw new Error('timeout');
    return Uint8Array.from(Buffer.from(value, 'hex'));
  },
};

try {
  const [mode, file] = process.argv.slice(2);
  const log = [];
  const result = mode === 'backup' ? await readProfile(transport)
    : (await writeProfile(transport, JSON.parse(readFileSync(file, 'utf8')), (m) => log.push(m)), { log });
  process.stdout.write(`OK ${JSON.stringify(result)}\n`);
} catch (e) {
  process.stdout.write(`ERR ${e.message}\n`);
}
process.exit(0);
