# firmware/ — ESP32-C3 sensor node

ESP-IDF project for the hardware half of thermo-warren. This directory is
the project root (`CMakeLists.txt`, `main/`, `sdkconfig.defaults` live here
directly). See `../CLAUDE.md` for the overall project and `CLAUDE.md` (this
directory) for decisions already settled in a planning session — toolchain,
GPIO map, DHT11 wiring, the payload contract.

Current stage: **17 — MQTT publisher (verified).** `app_main` runs a 1 Hz loop:
it reads the DHT11 every 5th tick, shows the reading and a Wi-Fi link icon on an
SSD1306 OLED over hardware I²C, and publishes the cached reading to RabbitMQ over
MQTT 5 at QoS 1 with an SNTP-stamped timestamp. Wi-Fi and MQTT both reconnect
automatically, and an outage longer than 60 s stops publishing at source with
every dropped reading named in the log. Stages 14 (toolchain), 15 (DHT11),
15b (OLED) and 16 (Wi-Fi) are verified too.

This is the whole hardware half of the plan. Stage 18 needs the software track as
well — see `../CLAUDE.md`.

**Two things a new board needs before it will work — both one-time, and both
easy to mistake for a firmware bug:**

1. **Credentials provisioned into NVS** — Wi-Fi, and from stage 17 the broker
   too — or the board aborts at boot by design. See
   [Provisioning](#provisioning) below.
2. **Its MAC address added to the router's whitelist.** This network filters by
   MAC, so a board swap breaks Wi-Fi until the new MAC is allowed. The symptom
   is a disconnect with `reason=202`, which reads as "authentication failed" and
   looks exactly like a wrong password. The MAC is in the boot log
   (`wifi:mode : sta (xx:xx:...)`).

## Prerequisites

- ESP-IDF v5.5.5, installed at `~/esp/esp-idf-v5.5.5`
- Target **esp32c3**; board is an **ESP32-C3 Super Mini**, rev v0.4, with **4 MB
  embedded flash** (confirmed by `python -m esptool -p /dev/ttyACM0 flash_id`)

Note the build currently declares `CONFIG_ESPTOOLPY_FLASHSIZE_2MB` and uses the
stock single-app partition table, so the app gets 1 MB of that 4 MB and is at 4%
free. See "App partition" in `CLAUDE.md` — changing it must keep `nvs` at
`0x9000`, or provisioned credentials are lost.

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

## Provisioning

**Required once per board.** From stage 16 onward the firmware refuses to
start without them: `sensor_wifi_start()` returns an error and
`ESP_ERROR_CHECK` aborts at boot, deliberately, so an unprovisioned board
fails loudly instead of running offline while looking healthy on the OLED.

Credentials live in the device's **NVS partition**, not in the source tree
and not in the app binary. Nothing secret is ever committed, and a built
`.bin` can be shared without leaking the network password.

Two namespaces, one file, one flash:

| Namespace | Keys | Read by |
|---|---|---|
| `wifi` | `ssid`, `password` | `wifi_station.c` (stage 16) |
| `mqtt` | `host`, `port`, `user`, `password` | the MQTT client (stage 17) |

The broker `host` is the **LAN address of the machine running docker
compose** — not `localhost`, and not a compose service name. `compose.yaml`
publishes 1883 on all interfaces for exactly this reason. `user` and
`password` must match `RABBITMQ_USER` / `RABBITMQ_PASSWORD` in the repo-root
`.env`; MQTT has no anonymous login on this broker.

```bash
cd firmware
cp provisioning.csv.example provisioning.csv
$EDITOR provisioning.csv      # fill in both namespaces
```

`provisioning.csv` is gitignored, as is the `provisioning.bin` generated
from it. Generate the NVS image and flash it to the `nvs` partition at `0x9000`
(its offset and 24 KB size come from `partitions_singleapp.csv`):

```bash
python3 $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate provisioning.csv provisioning.bin 0x6000

python -m esptool -p /dev/ttyACM0 write_flash 0x9000 provisioning.bin
```

This survives ordinary reflashing: `idf.py flash` writes only the
bootloader (`0x0`), partition table (`0x8000`) and app (`0x10000`), so
`0x9000` is left alone. You only need to repeat this after
`idf.py erase-flash`, to change networks, or when the broker host's LAN
address changes.

**NVS is not encrypted.** This keeps the passwords out of git and out of the
binary, which is the point — but anyone with physical access to the board
can read them back out of flash. It is not secure storage.

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

### Monitoring to a log file

`idf.py monitor` has `--timestamps` but **no option to save output** — it only
passes a subset of flags through. Run the monitor module directly for that:

```bash
python -m esp_idf_monitor -p /dev/ttyACM0 --save-log build/sensor-node.elf
```

**`--save-log` is a boolean flag, not a filename.** It names the file itself,
as `log.<elf-basename>.<YYYYMMDDHHMMSS>.txt` in the current directory — so the
command above writes `log.sensor-node.20260917123810.txt` and prints the name on
startup. `log.*.txt` is gitignored.

Passing a filename after `--save-log` does not fail loudly: it is swallowed as
the positional ELF argument instead, so address decoding silently points at a
file that does not exist and panic backtraces stop resolving to function names.
The `.elf` must be the only positional.

Worth defaulting to this for anything that takes more than a few seconds to
reproduce. Stage 17's outage test runs for minutes and the interesting lines —
the gate tripping, the outbox expiring — are separated by long stretches of
routine output that push them out of terminal scrollback. Reconstructing them
from a partial copy-paste afterwards is not possible.

```bash
grep -c "outbox expiry dropped" run.log   # backstop drops
grep -o 'seq=[0-9]*' run.log | uniq       # the seq series, holes and all
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
