# firmware/ — ESP32-C3 sensor node

This directory **is** the ESP-IDF project root (`CMakeLists.txt`, `main/`,
`sdkconfig.defaults` live here directly). One project, grown additively across
stages 14 → 15 → 16 → 17. Nothing is thrown away between them.

Read the repo-root `CLAUDE.md` too — the working-style rules there apply here,
including the obligation to update this file at the end of every stage.

**Hardware track state: stage 14 done and verified.** Next is stage 15 (DHT11
reads).

See **`docs/dht-api.md`** for the sensor driver's full API, transcribed from its
source. Read it instead of guessing or searching — it also records four
behaviours that are not visible in the function signatures.

## Toolchain

- **ESP-IDF v5.5.5** at `~/esp/esp-idf-v5.5.5`, target **esp32c3**
- Board: **ESP32-C3 Super Mini**, 2.4 GHz Wi-Fi only

**Every shell needs the environment sourced before `idf.py` works:**

```bash
. ~/esp/esp-idf-v5.5.5/export.sh    # aliased to `idf55` in ~/.zshrc
```

It is per-shell and does not persist. If tool calls do not share a shell, chain
it with `&&`.

**Never pipe the sourcing command.** `. export.sh | tail` runs the source in a
subshell, so `IDF_PATH` and the `PATH` additions do not survive into later
commands. Source it as a standalone statement.

**A second checkout exists at `~/esp/esp-idf` on `master` (6.x)**, belonging to an
unrelated older project. Do not use or modify it. `echo $IDF_PATH` confirms which
is active. Do not run `idf_tools.py uninstall` — it would remove the Xtensa
toolchain that older project still needs.

## Flashing and monitoring must be done by the user

The Claude Code bash sandbox **cannot see `/dev/ttyACM*`** even when the board is
genuinely connected and enumerated. Verified at stage 14: `lsusb` from the user's
own shell showed `303a:1001 Espressif USB JTAG/serial debug unit`, while the
sandbox saw nothing.

So: **build here, but ask the user to run `idf.py flash` and `idf.py monitor` and
report back.** Monitor is interactive and never exits anyway (quit is `Ctrl-]`),
so it would hang a tool call regardless.

Expect the user to need an **unplug/replug cycle** before flash finds the port,
because the USB Serial/JTAG port re-enumerates on reset.

## Configuration: sdkconfig.defaults is the source of truth

- **`sdkconfig.defaults` is committed** — only deliberate, non-default choices,
  hand-commented. Currently `CONFIG_IDF_TARGET="esp32c3"` and
  `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`.
- **`sdkconfig` is generated and gitignored**, along with `sdkconfig.old`,
  `build/`, `managed_components/`, `dependencies.lock`.

The trap: `idf.py menuconfig` writes to `sdkconfig` **only** and never touches
`sdkconfig.defaults`. A change made there lives in an ignored file and is lost on
a fresh clone. Worse, `sdkconfig.defaults` does **not** override values already in
`sdkconfig`, so hand-editing it after a build does nothing, silently.

**Rule: after any menuconfig change, run `idf.py save-defconfig` and commit the
diff.**

Two facts verified at stage 14, both non-obvious:

- **`idf.py fullclean` removes `build/` but leaves `sdkconfig` in place.** To
  prove that `sdkconfig.defaults` is genuinely driving the configuration, delete
  `sdkconfig` itself. `fullclean` alone is a weaker test than it looks.
- **`CONFIG_IDF_TARGET` in `sdkconfig.defaults` is enough** for a fresh clone's
  first build to resolve the right target, per `__target_init` in
  `tools/cmake/targets.cmake`. `idf.py set-target esp32c3` is still worth running
  explicitly for clarity, but is not strictly required.

## The USB situation

There is **no USB-to-UART bridge** on this board. The USB-C connector wires
directly to the chip's internal USB Serial/JTAG controller on GPIO18 (D−) and
GPIO19 (D+). Any code reconfiguring either pin breaks USB and produces a cascade
of unrelated-looking errors. Treat both as reserved.

- Console must be USB Serial/JTAG, not UART0 — hence the setting above.
  Confirmed at stage 14 that the post-`set-target` default really is
  `CONFIG_ESP_CONSOLE_UART_DEFAULT=y`, so left alone the build and flash both
  succeed and the monitor shows nothing.
- The port disappears and re-enumerates on every reset, because the USB device
  *is* the chip. Early boot lines can be lost.
- A crash loop, deep sleep, or reconfiguring GPIO18/19 kills USB and normal
  flashing. Recovery is **manual download mode**: hold BOOT, tap RST, release
  BOOT. The ROM bootloader has its own USB stack and always enumerates.

## Stage 14 decisions (settled)

- **Project name `sensor-node`** — describes what it is, not which chip or
  protocol it currently uses, so stages 15–17 need no rename.
- **Counter runs directly in `app_main`**, not a separate task. `app_main`
  already executes as the FreeRTOS main task, so a loop with `vTaskDelay` in it
  *is* a task; a second buys nothing yet. Restructuring into explicit tasks is
  deferred to whichever of stages 15–17 first needs real concurrency.
- **Log tag `sensor_node`, level `ESP_LOGI`.** Tag matches the project name;
  later subsystems get their own tags (`dht11`, `wifi`) for filtering.
- **`firmware/README.md` is separate** from the root README — the toolchain,
  sourcing step and recovery procedure are irrelevant to the Python half.

## GPIO map

| Pins | Status |
|---|---|
| GPIO3 (SDA), GPIO10 (SCL) | **In use** — OLED, software I²C |
| GPIO2, GPIO8, GPIO9 | **Avoid** — strapping; GPIO8 drives the LED, GPIO9 is BOOT |
| GPIO18, GPIO19 | **Unavailable** — USB Serial/JTAG (not broken out anyway) |
| GPIO20, GPIO21 | **Avoid** — UART0 |
| GPIO12–17 | Not broken out — flash |
| GPIO0, 1, 4 | Free, but ADC1 channels — prefer to keep |
| GPIO5 | Free — ADC2 (unreliable with Wi-Fi active) |
| **GPIO6, GPIO7** | **Free, no ADC function — preferred for DHT11 data** |

DHT data pin not yet chosen; physical layout decides between 6 and 7.

## DHT11 wiring (stage 1, verified)

3-pin breakout. **Grille facing you, pins down, left to right:**

| Pin | Function |
|---|---|
| P1 | **DATA** |
| P2 | **VCC** |
| P3 | **GND** |

Established three ways: R1 = 3.36 kΩ between P1–P2, symmetric under probe swap
(so a resistor, not a junction); leg map P2→S1 (VDD), P1→S2 (DATA), P3→S4 (GND),
with S3 open as the NC anchor; and the board silkscreen agreeing.

- **Power from 3V3, never 5V.** The pull-up ties DATA to VCC, and ESP32-C3 GPIOs
  are not 5 V tolerant.
- **No external pull-up needed** — R1 (3.3 kΩ) is on the module.
- **The driver never configures an internal pull-up** and has no option to. It
  uses open-drain output and relies entirely on R1. Do not add one in
  application code. See `docs/dht-api.md`.

## Sensor health

A **reverse-polarity event** occurred during stage 1, and VCC/GND were
reconnected again during stage 14's flash test without polarity being checked.

**Re-measured after stage 14: P1–P2 = 3.36 kΩ, P1–P3 = OL, P2–P3 = OL** —
matching stage 1 exactly. Polarity is correct and there is no rail-to-rail
short.

**Whether the die still responds is unknown until a read succeeds.** Resistance
cannot show that. If stage 15's first reads fail, a dead sensor remains a live
hypothesis alongside a driver or wiring mistake — do not assume a driver bug. A
spare is on order.

## Sensor driver (stage 15)

`esp-idf-lib/dht`, a **registry component** rather than a vendored monorepo:

```bash
idf.py add-dependency "esp-idf-lib/dht"
```

CI-verified against IDF v5.2–v6.0 on esp32c3. **Full API and its non-obvious
behaviours are in `docs/dht-api.md`** — read that before writing sensor code.
The three that shape the design:

- **Each read holds a critical section for roughly 25 ms** (the 20 ms start
  pulse plus bit decoding, all inside `PORT_ENTER_CRITICAL`). A read is not a
  cheap call.
- **The OLED cannot run concurrently.** It uses software (bit-banged) I²C, the C3
  is single-core, and neither protocol tolerates preemption. Sequence them in one
  task or guard both with a mutex, or expect intermittent unreproducible read
  failures that look like bad wiring. Moving the OLED to hardware I²C is the
  cleaner fix, worth considering at stage 15.
- **DHT11 readings are whole numbers** — the driver discards the fractional byte
  for this sensor type. Expect `24.0`, never `24.4`.

Respect the DHT11's ~2 s minimum sampling interval; the driver does not enforce
it. The 1 Hz publish loop should read a cached value, and a tick without a fresh
sample is expected behaviour, not a bug.

## API verification

The **`mcp-api-doc` skill is not available** in the Claude Code environment as of
stage 14 — not in the skill list, not findable via tool search. The working
substitute is grepping the installed source directly under `$IDF_PATH` or
`managed_components/`, which is what the project's conventions ask for anyway.

This matters at **stages 16–17**, where `esp_wifi_*` and `esp_mqtt_*` are large
and version-sensitive.

**Parked, unverified:** whether the MQTT 5 PUBACK **reason code** reaches the
ESP-MQTT event handler. `MQTT_EVENT_PUBLISHED` is documented as carrying only the
message id, but `mqtt_client.h` has a general `reason_code` field on the event —
suggestive, not conclusive. Settle it by reading the installed header.

**Also unverified, and worth watching:** whether the DHT driver's ~25 ms
critical section disrupts Wi-Fi, which depends on timely interrupt service. Do
not assume either way. If Wi-Fi misbehaves once reads are running, this is the
first suspect.

## The payload contract (stage 17)

Frozen at stage 5. Firmware must satisfy it exactly; not open to renegotiation.
Rationale for each field is in `src/telemetry/CLAUDE.md`.

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
  conversion is needed. Note the driver's argument order is **humidity first**,
  the opposite of this field order.
- `ts_ms` — epoch **milliseconds**, publisher-stamped (requires SNTP, stage 16)
- MQTT topic `sensors/esp32c3/telemetry`, **QoS 1**, protocol version **5.0**
- **Client ID must differ from the software publisher's**, or the broker
  disconnects one of the two
- Port 1883 is bound to 127.0.0.1 only until stage 14's compose change widens it
  for the ESP32 — check this before debugging a connection failure
