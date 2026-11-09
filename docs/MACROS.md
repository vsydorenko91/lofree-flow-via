# Macros

VIA's macro tab (`M0..M7`, keycodes `0x7700..0x7707`) drives the **stock macro
player**; only the bytecode compiler is new.

## Stock player (reverse engineered)

* A macro is a list of 12-byte records: `u16 delay`, three slots `{type, u16 value}`,
  padding. A record is a report state held for `delay`; type 1 = modifier mask,
  type 2 = key usage. A record with `delay = 0` ends the macro (everything released).
* Macros are numbered from 1 and stored back to back; the scanner at `0x18e30`
  builds the length array at `0x401C08` at boot from the ROM table (5 factory
  macros, 19 records). A key action "type 9, value n" starts macro n.
* The player runs in every matrix pass; the delay counter is decremented in the scan
  interrupt (`0x1b378`). The first record's delay is halved.
* **A first record with delay < 2 hangs the player.** An empty macro is therefore
  compiled as `{2, empty}` + terminator.

## Implementation

* `macro_table` is a RAM table: the 19 factory records are copied from ROM by the
  gate loader (also in bypass mode), followed by 96 user records. Three stock
  references (`0x1D304`, `0x1EE3C`, `0x1A024`) are redirected to it. User lengths go
  to `0x401C0D..`; VIA `M0..M7` = type 9, values 6..13. Factory macros 1..5 are not
  editable.
* VIA commands `0x0C..0x10` (count, size = 192, read, write, reset). The compiler
  (`firmware/macro.c`) accepts printable ASCII (US layout), `0x01 0x01/2/3 kc`
  (tap/down/up), `0x01 0x04 digits |` (delay in ms) and the 16-bit forms
  `0x01 0x05/6/7`. At most three simultaneous slots. A dry run validates first so a
  bad buffer cannot corrupt the live table; edits made while a macro plays are
  applied when the player is free.
* The macro buffer is saved with the profile ([PERSISTENCE.md](PERSISTENCE.md)).

Delays are interrupt ticks (assumed 1 ms). Media keys and non-US characters are not
supported in macros.
