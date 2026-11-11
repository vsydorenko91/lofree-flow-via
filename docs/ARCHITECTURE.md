# Architecture

The firmware is a patch over the stock OE915 v2.52 application (CX53710, ARM7TDMI
class core, Thumb code). Nothing is rebuilt from scratch: `tools/build.py` compiles
the C sources in `firmware/`, appends them after the stock image and redirects a
small number of stock call sites into the new code. Every patch is listed with its
before/after bytes in the build manifest.

## Pieces

| Part | Source | Role |
|---|---|---|
| Gate | `firmware/gate.S` | Tiny assembly shims placed in zero padding of the stock image. Every hook goes through a gate. After each USB (re)connect the stock handlers run for a short startup window (32 passes); until the gate is active every hook falls through to the stock routine, so the stock update path always works. |
| Tap-hold engine | `firmware/th.c` | Mod-tap and layer-tap on up to four keys, three decision modes. Wraps the stock matrix scan; see [TAP_HOLD.md](TAP_HOLD.md). |
| VIA protocol | `firmware/protocol.[ch]` | Platform independent VIA command handler (keymap, macros, custom values). Mirrored by `tools/via_proto.py` as a reference model. |
| Adapter | `firmware/adapter.c` | Glue: RAM keymap banks, USB Raw HID transport, save/load, lighting, macro table. |
| Delta store | `firmware/dstore.[ch]` | Power-loss-safe journal of changes; see [PERSISTENCE.md](PERSISTENCE.md). |
| Web flasher | `web/` | WebHID flasher (`flasher-core.js`) with profile backup and restore around an update (`profile-core.js`). |
| Macro compiler | `firmware/macro.[ch]` | Turns VIA macro bytecode into the stock macro player's records; see [MACROS.md](MACROS.md). |

## USB / VIA transport

The stock firmware exposes a keyboard interface, a vendor/consumer/NKRO interface
and a mouse interface that is unused on this hardware. The mouse interface (IF2)
is replaced by an unnumbered 32-byte Raw HID interface (usage page `0xff60`,
usage `0x61`), which is what VIA speaks. The keyboard and vendor descriptors are
untouched, so the stock updater and stock features keep working.

* VIA requests arrive as the stock EP0 SET_REPORT OUT path; a hook after the stock
  FIFO read copies them to a bounded queue that the main loop drains.
* Replies are sent by the stock EP3 FIFO writer. USB epoch tags drop stale
  requests/replies across reconnects.
* **VIA commands are never forwarded to the stock vendor handler.** The stock
  vendor opcode `0x0d` enters firmware-update mode, while VIA uses `0x0d` for
  another command.

## Keymap model

The stock RAM keymap has four banks of 132 four-byte entries: Windows/Mac, each
with a base and an Fn layer. VIA sees them as four layers. QMK keycodes are
translated to stock entries by the protocol core; factory functions that have no
QMK equivalent (Bluetooth slots, lighting, OS switch, the stock macro) appear as ten
custom keycodes `0x7e00..0x7e09` and can only live in their original positions. The
Win/Mac switch is hardware; `MO(1)` is accepted only in the Windows pair and `MO(3)`
only in the Mac pair.

## Image limits

The stock bootloader accepts payloads up to `0x25e00` bytes at flash base `0x2000`
(see [MEMORY.md](MEMORY.md)), so the whole container is at most `0x27e00` bytes.
`tools/oe915.py` enforces this and the build fails if a section exceeds it. Two stock
functions that this patch leaves without callers (the Win/Mac keymap loader at `0x19710`
and the mouse report sender at `0x1f99c`, 540 bytes together) hold our code as well; the
build fails if anything in the stock image can still reach them. RAM:
BSS ends near `0x405e00` and the build keeps a stack reserve below `0x406000`.

## Verification strategy

Behavior is tested by running the **real stock code** plus the built image in a
Unicorn ARM emulator (USB FIFO/CSR model, scan IRQ tick, flash controller model with
power-cut injection). Native C tests cover the delta store and protocol. These tests
do not prove hardware timing; each release was also checked on a real keyboard.
