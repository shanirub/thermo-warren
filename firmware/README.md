# firmware/ — ESP32-C3 sensor node

ESP-IDF project for the hardware half of thermo-warren. This directory is
the project root (`CMakeLists.txt`, `main/`, `sdkconfig.defaults` live here
directly). See `../CLAUDE.md` for the overall project and `CLAUDE.md` (this
directory) for decisions already settled in a planning session — toolchain,
GPIO map, DHT11 wiring, the payload contract.

Current stage: **14 — toolchain and known-good flash (verified).** A
minimal `app_main` logs an incrementing counter roughly once a second, over
USB Serial/JTAG, to prove the build/flash/monitor loop before any sensor,
Wi-Fi or MQTT code is added. No DHT11 driver, no GPIO reference, no network
stack yet — that's stages 15-17.

## Prerequisites

- ESP-IDF v5.5.5, installed at `~/esp/esp-idf-v5.5.5`
- Target **esp32c3**; board is an **ESP32-C3 Super Mini**

Every shell needs the environment sourced before `idf.py` works — it does
not persist across shells:

```bash
. ~/esp/esp-idf-v5.5.5/export.sh
```

Confirm the right checkout is active — a second, unrelated ESP-IDF checkout
exists at `~/esp/esp-idf` (6.x master) and must not be used here:

```bash
echo $IDF_PATH   # must print .../esp-idf-v5.5.5
```

## Build

```bash
cd firmware
idf.py set-target esp32c3   # only needed once per fresh clone; sdkconfig.defaults
                             # also records the target, so a plain `idf.py build`
                             # on a clean checkout works without this step too
idf.py build
```

## Flash

No USB-to-UART bridge exists on this board — the USB-C connector wires
directly to the chip's internal USB Serial/JTAG controller. Console output
requires `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, which is committed in
`sdkconfig.defaults` (see the comment there for why).

```bash
idf.py flash                     # auto-detects the port
idf.py -p /dev/ttyACM0 flash     # if auto-detect fails
```

**Do not run `idf.py monitor` from an automated/non-interactive context.**
It never exits on its own (quit is `Ctrl-]`).

```bash
idf.py -p /dev/ttyACM0 monitor
```

## Stage 14 verification

Verification actually performed (2026-09-12), against real hardware — not
just a passing build:

1. `idf.py build` — succeeded for target `esp32c3`.
2. `idf.py -p /dev/ttyACM0 flash` — succeeded. The board needed an
   unplug/replug before the port enumerated; expected, since the USB
   Serial/JTAG device re-enumerates on every reset (the USB device *is* the
   chip, not a separate bridge).
3. `idf.py -p /dev/ttyACM0 monitor` — showed `sensor_node: counter=N`
   incrementing once per second (timestamps `71`, `1071`, `2071`, … ms in
   the log), over USB Serial/JTAG.
4. **Fresh-clone proof**: `sdkconfig` and `sdkconfig.old` were deleted
   entirely (not just `idf.py fullclean`, which only removes `build/`), then
   `idf.py build` was run again with no target or console setting present
   anywhere except `sdkconfig.defaults`. The build regenerated `sdkconfig`
   with `CONFIG_IDF_TARGET="esp32c3"` and
   `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` present, both read from
   `sdkconfig.defaults` — confirming the setting survives a fresh clone
   rather than only living in a machine's already-generated `sdkconfig`.

## Manual download mode (recovery procedure)

Because the console *is* the same USB connection used for programming, a
crash loop, deep sleep, or reconfiguring GPIO18/19 (the internal USB
Serial/JTAG pins) can knock out USB entirely and make the board un-flashable
over the normal path. Recovery:

1. Hold the **BOOT** button.
2. Tap **RST** (reset) while still holding BOOT.
3. Release **BOOT**.

This drops the chip into the ROM bootloader, which has its own independent
USB stack and always enumerates regardless of what the flashed application
did. From there, `idf.py flash` (or `idf.py erase-flash` if a clean slate is
needed) works normally again.

## Configuration

`sdkconfig.defaults` is the committed source of truth — it holds only
deliberate, non-default choices. `sdkconfig` is generated and gitignored.

**The trap**: `idf.py menuconfig` writes to `sdkconfig` only, never to
`sdkconfig.defaults`. A change made there lives in a gitignored file and is
lost on a fresh clone. Worse, `sdkconfig.defaults` does not override values
already present in `sdkconfig`, so hand-editing it on a machine that already
built silently does nothing.

**Rule**: after any menuconfig change, run `idf.py save-defconfig` and
commit the diff.
