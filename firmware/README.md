# firmware/ — ESP32-C3 sensor node

ESP-IDF project for the hardware half of thermo-warren. This directory is
the project root (`CMakeLists.txt`, `main/`, `sdkconfig.defaults` live here
directly). See `../CLAUDE.md` for the overall project and `CLAUDE.md` (this
directory) for decisions already settled in a planning session — toolchain,
GPIO map, DHT11 wiring, the payload contract.

Current stage: **16 — Wi-Fi station mode (verified).** `app_main` reads the
DHT11 every 5 s, shows the reading on an SSD1306 OLED over hardware I²C, and
joins Wi-Fi in station mode with automatic reconnect. Stages 14 (toolchain),
15 (DHT11) and 15b (OLED) are verified too; MQTT and SNTP are stage 17.

**Two things a new board needs before it will work — both one-time, and both
easy to mistake for a firmware bug:**

1. **Wi-Fi credentials provisioned into NVS**, or the board aborts at boot by
   design — see [Wi-Fi credentials](#wi-fi-credentials) below.
2. **Its MAC address added to the router's whitelist.** This network filters by
   MAC, so a board swap breaks Wi-Fi until the new MAC is allowed. The symptom
   is a disconnect with `reason=202`, which reads as "authentication failed" and
   looks exactly like a wrong password. The MAC is in the boot log
   (`wifi:mode : sta (xx:xx:...)`).

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

## Wi-Fi credentials

**Required once per board.** From stage 16 onward the firmware refuses to
start without them: `sensor_wifi_start()` returns an error and
`ESP_ERROR_CHECK` aborts at boot, deliberately, so an unprovisioned board
fails loudly instead of running offline while looking healthy on the OLED.

Credentials live in the device's **NVS partition**, not in the source tree
and not in the app binary. Nothing secret is ever committed, and a built
`.bin` can be shared without leaking the network password.

```bash
cd firmware
cp wifi_creds.csv.example wifi_creds.csv
$EDITOR wifi_creds.csv        # fill in your SSID and password
```

`wifi_creds.csv` is gitignored, as is the `wifi_creds.bin` generated from
it. Generate the NVS image and flash it to the `nvs` partition at `0x9000`
(its offset and 24 KB size come from `partitions_singleapp.csv`):

```bash
python3 $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate wifi_creds.csv wifi_creds.bin 0x6000

python -m esptool -p /dev/ttyACM0 write_flash 0x9000 wifi_creds.bin
```

This survives ordinary reflashing: `idf.py flash` writes only the
bootloader (`0x0`), partition table (`0x8000`) and app (`0x10000`), so
`0x9000` is left alone. You only need to repeat this after
`idf.py erase-flash`, or to change networks.

**NVS is not encrypted.** This keeps the password out of git and out of the
binary, which is the point — but anyone with physical access to the board
can read it back out of flash. It is not secure storage.

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
