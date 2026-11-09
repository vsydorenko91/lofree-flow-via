# Persistence ("Save to keyboard")

VIA edits take effect in RAM immediately. **Flow84 tab → Save → Save to keyboard**
writes only the differences from the factory profile, the lighting settings, the
tap-hold timeout and the macro buffer to flash. The automatic VIA command `0x09` never
writes flash. A saved profile is loaded at boot, at the first moment no key is held.
The *Unsaved changes* indicator is on when keys or macros were edited, or when the backlight, side
RGB or tap-hold setting differs from the saved state.

## Where it is stored

The application cannot write its own code area (selector 0) on this board, and there
is no EEPROM chip. The controller at `0x803000` exposes a second flash area, selector
1, used by the stock firmware as a small settings journal. It has two 512-byte sectors:

* **Sector A** (words `0x8000..0x807f`) — used here: bytes `0..475` hold our journal.
  Bytes `0x1F0..0x1F7` carry the bootloader update marker and are never touched.
* **Sector B** (word `0x8080`) — the stock journal, untouched.

The stock code uses sector A as a *backup buffer* for rotating sector B and erases
it at boot. Left alone this wiped the saved profile on every power cycle even though
Save reported success. The build therefore patches those uses out (the boot recovery
branch, the erase function, four erase/program calls in the rotation). The only loss
is the stock journal's protection against a power cut during its own rotation.
Entering update mode, a factory reset and a firmware update still erase sector A.
`tests/test_oem_sector_a.py` covers these patches.

The stock wrappers are used for flash access: erase `0x19661`, program with verify
`0x1b9a5` (≤32 bytes), read `0x1a76d`. The selector-1 read window repeats every
`0x400` words, so the stock code addresses sector B through aliases.

## Format (`firmware/dstore.[ch]`)

17 records of 28 bytes, appended in order (a log). Each record has a tag, sequence,
item count, flags and a CRC16.

* `0xC7` key record: backlight level, a "light" byte, and up to 5 `{position, VIA keycode}` pairs.
* `0xC9` blob record: 16 bytes of the macro buffer at an offset.
* Flags: snapshot, more-records-follow, first-record-of-save, blob-reset.

The light byte is `0x80 | tap_term<<5 | mode<<3 | color`.

A save is applied only if all of its records are valid and the last one has no
"more" flag, so a power cut leaves the old or the new state, never a mix (the native
test cuts power at every byte). A blank slot ends the log. When the log is full the
store compacts: erase and write a snapshot; if the snapshot would not fit it refuses
**before** erasing. An unchanged profile writes nothing. A profile that fails
validation at boot (protected slots, mod-tap limits) is discarded as a whole.

Capacity: 85 changed keys, a 192-byte macro buffer; both together may not fit, in
which case Save reports failure before writing.

## Findings from hardware experiments

* Writing selector 0 from the application: erase "succeeds", programming returns an
  error or leaves memory unchanged although the command sequence matches the
  bootloader; code running from RAM triggers a reset. Cause unknown.
* I²C EEPROM: stock code exists, but its enable flag is zero and forced reads return
  zeros within 3 ms (no chip on the bus).
