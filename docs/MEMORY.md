# Memory and bootloader facts

## Flash

* The stock update container is a `0x2000`-byte header plus the payload; the payload
  is linked and flashed at `0x2000`. The first 8 KiB of flash is the bootloader (not
  part of any update file).
* The bootloader checks the payload length against `0x25e00` and always erases the
  fixed interval `[0x2000, 0x27e00)` (selector 0, 512-byte sectors). The maximum
  whole image is therefore `0x27e00` = 163,328 bytes; a full-size image was flashed
  and ran correctly on hardware.
* Stock application size is 140,144 bytes of payload (ends at `0x24370`); our image
  is larger (see `out/manifest.json`).
* The chip family of the FCC sample is a Beken BK3635 (160 KB flash, 32 KB RAM);
  this is consistent with the numbers above but not proven for every hardware revision.
* The controller at `0x803000` has two selectors: 0 = application area (the stock
  writer rejects writes below word `0x800`; writes from the application do not work
  here), 1 = settings area with sectors A and B (see [PERSISTENCE.md](PERSISTENCE.md)).

## RAM

* Stock RAM starts at `0x400000`; our BSS begins near `0x404dc4` and the build keeps
  its end below `0x406000`; the observed stack minimum is above `0x406c80`.
* Key stock locations: keymap RAM `0x401c34` (via pointer `0x400408`), report buffers
  around `0x401a78`, matrix `0x401aec`, macro length array `0x401c08`.
* USB FIFO/CSR registers are around `0x804000`.

## Key stock functions

Matrix scan `0x1beb4`, key event `0x19898`, keymap tables `0x22cc0/0x22ed0/0x230e0/0x232f0`
(Win, Win+Fn, Mac, Mac+Fn), settings journal `0x22038`, brightness `0x1a904`, color
`0x1ad38`, lighting mode `0x1ada8`, OS switch loader `0x1af9c`.

## Factory function labels

Custom keycodes in the VIA definition map to stock actions:

| Code | Action | Notes |
|---|---|---|
| `0x7e00..0x7e02` | Bluetooth 1..3 | short press selects, hold pairs |
| `0x7e03` | Lighting mode (stock) | three-state cycle |
| `0x7e04` | Color index (stock) | cycles 0..7 |
| `0x7e05/06` | Backlight up/down | index 0..4 |
| `0x7e07` | Windows mode | switches the OS bank |
| `0x7e08` | Mac mode | switches the OS bank |
| `0x7e09` | Macro 4 (Win+Tab) | fixed stock macro |

Type `04` entries exist in the handler (`0x19f78`) but are unidentified; they are not
offered. The stock lighting helpers write the stock settings journal; VIA lighting
controls therefore set RAM state only and persist through our own save.

## Lighting hardware

Two separate modules: white key backlight (levels 0..4) and side RGB panels. Side RGB
color index 0..7: red, green, yellow, turquoise, cyan, blue, purple, white; modes 0 and
1 look identical, mode 2 is off. VIA exposes them as On/Off.
