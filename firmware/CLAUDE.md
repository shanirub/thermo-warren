# firmware/ — ESP32-C3 sensor node

This directory **is** the ESP-IDF project root (`CMakeLists.txt`, `main/`,
`sdkconfig.defaults` live here directly). One project, grown additively across
stages 14 → 15 → 16 → 17. Nothing is thrown away between them.

Read the repo-root `CLAUDE.md` as well; the working-style rules there apply here.

## Toolchain

- **ESP-IDF v5.5.5**, installed at `~/esp/esp-idf-v5.5.5`
- Target **esp32c3**; board is an **ESP32-C3 Super Mini**
- Host is **Fedora Linux**, shell is **zsh**, user is already in `dialout`

**Every shell needs the environment sourced before `idf.py` works:**

```bash
. ~/esp/esp-idf-v5.5.5/export.sh    # aliased to `idf55`
```

It is per-shell and does not persist. If tool calls do not share a shell, chain
it: `. ~/esp/esp-idf-v5.5.5/export.sh && idf.py build`.

**A second checkout exists at `~/esp/esp-idf` on `master` (6.x).** It belongs to
an unrelated older project and must not be used or modified. `echo $IDF_PATH`
confirms which is active. Do not run `idf_tools.py uninstall` — it would remove
the Xtensa toolchain that older project still needs.

## Do not run `idf.py monitor` from a tool call

It never exits on its own (it is interactive, quit is `Ctrl-]`) and will hang.
Build and flash are fine; **ask the user to run monitor and report what they see.**

## Configuration: sdkconfig.defaults is the source of truth

- **`sdkconfig.defaults` is committed.** It holds only deliberate, non-default
  choices — the hardware analogue of `topology_spec.py`.
- **`sdkconfig` is generated and gitignored.**

The trap: `idf.py menuconfig` writes to `sdkconfig` **only** and never touches
`sdkconfig.defaults`. A change made in menuconfig lives in an ignored file and is
lost on a fresh clone. Worse, `sdkconfig.defaults` does **not** override values
already present in `sdkconfig`, so hand-editing it on a machine that already
built does nothing, silently.

**Rule: after any menuconfig change, run `idf.py save-defconfig` and commit the
diff.**

Required setting, with a comment in the file saying why:

```
CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y
```

## The USB situation

There is **no USB-to-UART bridge** on this board. The USB-C connector wires
directly to the chip's internal USB Serial/JTAG controller on GPIO18/19.
Consequences:

- Console must be USB Serial/JTAG, not UART0 — hence the setting above. Left at
  default, the build and flash both succeed and the monitor shows nothing.
- The serial port disappears and re-enumerates on every reset, because the USB
  device *is* the chip. Early boot lines can be lost.
- A crash loop, deep sleep, or reconfiguring GPIO18/19 kills USB and normal
  flashing. Recovery is **manual download mode**: hold BOOT, tap RST, release
  BOOT. The ROM bootloader has its own USB stack and always enumerates.

## GPIO map

| Pins | Status |
|---|---|
| GPIO3 (SDA), GPIO10 (SCL) | **In use** — OLED, software I²C |
| GPIO2, GPIO8, GPIO9 | **Avoid** — strapping pins; GPIO8 also drives the LED, GPIO9 is BOOT |
| GPIO18, GPIO19 | **Unavailable** — USB Serial/JTAG (not broken out anyway) |
| GPIO20, GPIO21 | **Avoid** — UART0 |
| GPIO12–17 | Not broken out — flash |
| GPIO0, 1, 4 | Free, but ADC1 channels — prefer to keep |
| GPIO5 | Free — ADC2 (unreliable with Wi-Fi active) |
| **GPIO6, GPIO7** | **Free, no ADC function — preferred for DHT11 data** |

Final DHT data pin not yet chosen; physical layout decides between 6 and 7.

## DHT11 wiring (stage 1, verified)

Module is a 3-pin breakout. **Grille facing you, pins down, left to right:**

| Pin | Function |
|---|---|
| P1 | **DATA** |
| P2 | **VCC** |
| P3 | **GND** |

Established three ways: R1 = 3.36 kΩ measured between P1–P2 (symmetric under
probe swap, so a resistor not a junction); leg map P2→S1 (VDD), P1→S2 (DATA),
P3→S4 (GND), with S3 open as the NC anchor; and the board silkscreen agreeing.

- **Power from 3V3, never 5V.** The pull-up ties DATA to VCC, and ESP32-C3 GPIOs
  are not 5 V tolerant.
- **No external pull-up** — R1 (3.3 kΩ) is on the module.
- **Disable the internal pull-up** for the same reason.

**Sensor health is UNCONFIRMED.** A reverse-polarity event occurred during stage
1. Resistances were re-measured afterwards and were unchanged, which rules out a
rail-to-rail short but does not prove the die still responds. If stage 15 reads
fail, a dead sensor is a live hypothesis — do not assume a driver bug. A spare is
on order.

## Sensor driver (stage 15)

`esp-idf-lib/dht` is a **registry component**, not a vendored monorepo:

```bash
idf.py add-dependency "esp-idf-lib/dht"
```

CI-verified against IDF v5.5 on esp32c3. Small API: `dht_read_data()`,
`dht_read_float_data()`, `dht_sensor_type_t`.

The driver enters a critical section and disables interrupts while sampling,
because it must distinguish a ~26 µs pulse from a ~70 µs one. **The C3 is
single-core**, and the OLED also uses bit-banged (software) I²C. These two cannot
genuinely overlap. Drive both from one task in sequence, or guard with a mutex —
otherwise expect intermittent, unreproducible read failures that look like bad
wiring. Moving the OLED to hardware I²C is the cleaner fix and is worth
considering at stage 15.

## API verification

Use the **`mcp-api-doc` skill** to verify ESP-IDF and FreeRTOS symbols before
using them — especially the sprawling surfaces (`esp_wifi_*`, `esp_mqtt_*`) at
stages 16–17, where signatures drift between versions. Do not rely on memory or
on tutorials, which are frequently written against older releases.

## The payload contract (stage 17)

Frozen at stage 5. Firmware must satisfy it exactly; it is not open to
renegotiation.

```json
{
  "seq": 1234,
  "device": "sim-01",
  "temp_c": 24.4,
  "humidity_pct": 62.5,
  "ts_ms": 1756400000123
}
```

- `seq` — monotonic int, restarts at 1 each boot; deliberately not globally unique
- `device` — must differ from the simulator's value
- `temp_c` / `humidity_pct` — floats, matching `dht_read_float_data()` so no
  conversion is needed
- `ts_ms` — epoch **milliseconds**, stamped by the publisher (requires SNTP,
  stage 16)
- MQTT **QoS 1**, protocol version **5.0**
- **Client ID must differ from the software publisher's**, or the broker
  disconnects one of the two
