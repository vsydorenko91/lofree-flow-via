# Development

## Requirements

Python 3.10+, `pip install -r requirements.txt` (ziglang for the ARM cross-compiler,
unicorn and capstone for emulation, pyelftools, hidapi for the CLI tools), and Node 18+
for the flasher-core test. Close VIA in the browser before using the HID tools: only one
program can open the Raw HID interface.

## Build

```sh
python tools/build.py            # out/flow84-via.bin + out/manifest.json
python tools/flash.py validate   # image == stock + declared patches; nothing is sent
python tools/make_web.py         # stage web/firmware and web/flow84.json
```

`manifest.json` lists every changed stock address with before/after bytes.
`flash.py` refuses an image that differs from stock plus that list.

## Tests

```sh
PYTHONPATH=tools python -m unittest discover -s tests
node web/test-core.mjs           # after make_web.py
node web/test-page.mjs           # the flasher page against a fake WebHID keyboard
```

* Emulator tests (`test_device_emulated`, `test_persistence_device`, `test_macro_device`,
  `test_oem_*`) run the real stock code plus the built image in Unicorn.
* Native tests compile `dstore.c`, `macro.c`, `protocol.c` with clang/cc and drive them via ctypes.
* `test_recovery` rehearses flash/restore against a fake HID layer.
* `test_web_profile` runs the flasher's backup/restore code (`web/profile-core.js`, via node)
  against the emulated firmware; it is skipped when node is not installed.
* `test_bootloader_bounds` needs `local/bootloader-d386f6ff.bin`, a dump of the keyboard's
  own bootloader that is deliberately not in the repository; it is skipped without it.

## Flashing and diagnostics from the CLI

```sh
python tools/flash.py inspect            # read-only
python tools/flash.py flash              # writes out/flow84-via.bin
python tools/flash.py restore-stock
python tools/device_check.py backup --file local/layout.json   # keymap backup
python tools/device_check.py restore --file local/layout.json
python tools/device_check.py save | status | dump | telemetry | rate
```

`device_check.py` backs up only the keymap; macros and lighting are not included.

## Conventions

* Keep flash and RAM budgets in mind: the build fails over the bootloader limit, and
  struct copies pull in `memcpy`, which does not exist in this freestanding build.
* Every behavior change needs an emulator test; every flash-format change needs a
  power-cut test.

## Publishing the flasher (GitHub Pages)

In the repository settings choose *Pages → Source: GitHub Actions*. The `ci` workflow builds the
image, stages `web/` and deploys it. All page URLs are relative, so it works under
`https://<user>.github.io/<repo>/`. WebHID needs HTTPS, which Pages provides.
