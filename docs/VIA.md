# VIA protocol coverage

Transport: unnumbered 32-byte Raw HID, usage page `0xff60`, usage `0x61`
(see [ARCHITECTURE.md](ARCHITECTURE.md)). `tools/via_proto.py` is a Python reference
model with the same behavior; `tests/test_protocol_native.py` compares it with the C code.

## Standard commands

Protocol version 13, keyboard values (uptime, layout options, firmware version), switch
matrix state query, dynamic keymap get/set for single keycodes and bulk buffers
(4 layers, matrix 6×22), keymap reset (`0x06`, RAM only), macro count/size/buffer (`0x0C..0x10`). Anything else (encoders, QMK lighting, bootloader jump) is answered
with the VIA "unhandled" byte `0xFF`.

Keycodes: basic usages `0x04..0xA4`, mod-tap `0x2000..0x3FFF`, layer-tap `0x4000..`
(Win base → layer 1, Mac base → layer 3), macros `0x7700..0x7707`, factory functions
`0x7E00..0x7E09`. Requests that the stock action engine cannot represent are rejected
atomically (nothing changes).

## Custom values (menu in `definitions/flow84.json`)

Channel 0; command `0x07` sets, `0x08` gets, `0x09` is VIA's automatic "save" which
never writes flash.

| id | Meaning | Values |
|---|---|---|
| 1 | Save to keyboard (set only, value 1) | button |
| 2 | Status string (get) | "Saved", "Unsaved changes", "No saved profile", "Save failed" |
| 3 | Telemetry (get): save counters, stage, matrix pass counter | diagnostics |
| 6 | White key backlight | 0..4 |
| 10 | Side RGB color | 0..7 (red, green, yellow, turquoise, cyan, blue, purple, white) |
| 11 | Side RGB effect | 0 steady, 1 breathing, 2 off |
| 12 | Raw journal record (get) | slot 0..16, 28 bytes |
| 13 | "Unsaved changes" indicator | read-only toggle (clicks are acknowledged and ignored) |
| 14 | Tap-hold timeout preset | 0 normal, 1 short, 2 long, 3 very long |
| 15 | Firmware version (get) | minor version number; the full version is the VIA firmware-version keyboard value (`major<<16 \| minor<<8 \| patch`) |
| 18 | Tap-hold decision | 0 hold when another key is pressed, 1 when another key is pressed and released, 2 only after the timeout |
| 16 | Reset to factory (set only, value 1): factory keymap and empty macros in RAM; lighting and timeout are kept; press Save to make it permanent | button |
