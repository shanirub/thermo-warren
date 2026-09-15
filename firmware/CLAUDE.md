# firmware/ — ESP32-C3 sensor node

This directory **is** the ESP-IDF project root (`CMakeLists.txt`, `main/`,
`sdkconfig.defaults` live here directly). One project, grown additively across
stages 14 → 15 → 16 → 17. Nothing is thrown away between them.

Read the repo-root `CLAUDE.md` too — the working-style rules there apply here,
including the obligation to update this file at the end of every stage.

**Hardware track state: stage 16 done and verified.** DHT11 reads, SSD1306
OLED display and Wi-Fi station mode with automatic reconnect, all confirmed
on hardware running together. Next is stage 17 (MQTT, and the SNTP that its
`ts_ms` field needs).

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
| GPIO3 (SDA), GPIO10 (SCL) | **In use** — OLED, hardware I²C (I2C0) |
| GPIO2, GPIO8, GPIO9 | **Avoid** — strapping; GPIO8 drives the LED, GPIO9 is BOOT |
| GPIO18, GPIO19 | **Unavailable** — USB Serial/JTAG (not broken out anyway) |
| GPIO20, GPIO21 | **Avoid** — UART0 |
| GPIO12–17 | Not broken out — flash |
| GPIO0, 1, 4 | Free, but ADC1 channels — prefer to keep |
| GPIO5 | Free — ADC2 (unreliable with Wi-Fi active) |
| GPIO7 | **In use** — DHT11 data |
| GPIO6 | Free, no ADC function |

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

**Resolved at stage 15: the die responds, and the reading is genuinely live.**
Reads succeed, return plausible values, and the plan's breathe test (humidity
climbs then decays under breath) was run and passed — ruling out a stuck
buffer or silently-accepted checksum failure. The spare that was on order is
now a spare, not a required fallback.

## Sensor driver (stage 15, done)

**Verified on hardware:** GPIO7, `dht_read_float_data(DHT_TYPE_DHT11, ...)`,
5 s poll interval, plausible temp/humidity values logged on the sampling
cadence over USB Serial/JTAG. The plan's specified check — breathe on the
sensor, confirm humidity climbs then decays — was also run and passed,
ruling out a stuck buffer or silently-accepted checksum failure. Both the
"sensor health" open question (does the die respond) and the "proven live
vs. plausible-looking constant" distinction are closed.

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
- **The OLED must not run concurrently with a DHT read**, regardless of I2C
  flavor — the C3 is single-core and the DHT driver's critical section blocks
  everything. Moved to hardware I2C at stage 15b (see "OLED display" below);
  the two are still sequenced in one task, never overlapped, and verified
  together on hardware with no dropped reads.
- **DHT11 readings are whole numbers** — the driver discards the fractional byte
  for this sensor type. Expect `24.0`, never `24.4`.

Respect the DHT11's ~2 s minimum sampling interval; the driver does not enforce
it. The 1 Hz publish loop should read a cached value, and a tick without a fresh
sample is expected behaviour, not a bug.

## OLED display (stage 15b, done)

**Verified on hardware:** SSD1306 128x64, I2C0 (GPIO3 SDA / GPIO10 SCL,
400kHz), updates once per DHT read (5s cadence) showing "Temp: X.X°C" /
"Humidity: Y.Y%". Confirmed updating in real time against a changing
humidity reading (breath test), with no dropped DHT reads while the display
was active.

**I2C peripheral: I2C0.** Nothing else in this project claims a hardware I2C
bus, so there was no conflict to design around; I2C1 would have been an
arbitrary choice.

**Display driver: ESP-IDF's native `esp_lcd_panel_ssd1306`** (in the `esp_lcd`
component, ships with the toolchain — no external dependency). This was not
the original plan, and the actual path there is worth recording since it
overturned two assumptions made from memory rather than from the installed
source:

- **`esp-idf-lib/ssd1306` does not exist.** The `esp-idf-lib` registry
  namespace has no SSD1306 component at all — checked directly against the
  registry, not assumed.
- **`espressif/ssd1306`** (what `idf.py add-dependency "ssd1306"` resolves
  to) **is upstream-deprecated** — its README says so outright, and its only
  constructor is marked `deprecated` in the header. It also uses the
  **legacy** `i2c_port_t` driver (`driver/i2c.h`), not the
  `i2c_master_bus_handle_t` driver this stage is otherwise built on. Fetched
  and read before rejecting it, not assumed from the name.
- **U8g2 (hardware I2C via a vendored C-only build)** was evaluated third and
  is a live option if a fuller-featured display library is ever needed, but
  was dropped in favor of the option below to avoid a 40+ MB vendored font
  source tree in the repo for two lines of text.
- Landed on `esp_lcd_panel_ssd1306` instead: it's the driver Espressif's own
  deprecation notice on `espressif/ssd1306` points to, it's already present
  in the installed IDF (confirmed via `find` under `$IDF_PATH`), and its I2C
  transport (`esp_lcd_new_panel_io_i2c`) genuinely takes
  `i2c_master_bus_handle_t` (confirmed by reading
  `esp_lcd_io_i2c.h`) — the correct driver generation for this stage.

**Tradeoff accepted:** `esp_lcd_panel_ssd1306` is a raw bitmap panel with no
font or text API. `firmware/main/oled_display.c` carries a small 5x7 bitmap
font covering only the characters the two display lines use (digits, `:`,
`.`, `%`, `°`, and the specific letters in "Temp"/"Humidity") — not full
ASCII, deliberately, since nothing else needs it yet. The font table was
generated from an ASCII-art grid via a one-off script rather than
hand-transcribed, to keep the bit arithmetic out of the error-prone path;
correctness of the actual glyph shapes was confirmed by reading the display,
not by inspecting the byte values.

**Update cadence: every DHT read**, not a separate timer — the two are
already sequenced in the same loop iteration (read, then display), so a
second timer would add a moving part for no benefit.

**No GPIO conflicts.** GPIO3/GPIO10 were already reserved for the OLED
before this stage (see GPIO map); moving from software to hardware I2C only
changed which peripheral drives them, not the pins themselves.

**Stage 16 (Wi-Fi) proceeded cleanly**, as expected — nothing here touches
GPIO18/19 (USB) or claims a second I2C bus. The open question of whether the
DHT driver's critical section disrupts Wi-Fi is now **closed**; see "Wi-Fi"
below.

## Wi-Fi (stage 16, done)

**Verified on hardware:** station mode, credentials read from NVS, associates
with the AP, obtains a DHCP address, and reconnects automatically after an
outage. Reconnect was confirmed by **disabling the AP's 2.4 GHz radio for
~2 minutes, not by power-cycling the AP** — a faithful proxy from the station's
point of view (beacons stop either way), but it leaves the router's DHCP server
and lease table up, so fresh-lease acquisition after a real reboot is untested.
The full sequence was observed: reason 200 on loss, reason 201 on each failed
retry, backoff 1s → 2s → 4s → 8s → 16s → 30s with the cap holding across
several attempts, then association and `got ip` with no manual reset. The 5 s
sensor loop kept running throughout.

**Credentials live in NVS** (namespace `wifi`, keys `ssid`/`password`), flashed
as a separate image at `0x9000` — never in the source tree, never in the app
binary. `idf.py flash` writes only `0x0`, `0x8000` and `0x10000`, so credentials
survive ordinary reflashing; they need re-flashing only after `idf.py
erase-flash` or a network change. Procedure is in `README.md`. **NVS is not
encrypted** — this keeps the password out of git, not out of reach of anyone
holding the board.

**An unprovisioned board aborts at boot**, deliberately: `sensor_wifi_start()`
returns `ESP_ERR_NVS_NOT_FOUND` and `ESP_ERROR_CHECK` panics, rather than
leaving a board that looks healthy on the OLED while silently never reaching the
network.

**Reconnect runs in its own task** — not in the disconnect event handler, not on
`esp_timer`. The handler executes on the default event-loop task, so sleeping
there to back off would stall delivery of every other event on that loop.
`esp_timer.h` asks for callbacks lasting "a few microseconds" dispatched from a
single shared high-priority task, which `esp_wifi_connect()` is not. The handler
notifies with `xTaskNotifyGive`; the task owns the backoff and the retry.

**Backoff 1 s → 30 s, deliberately no attempt limit** — the DoD requires
rejoining after an outage of unknown duration. `IP_EVENT_STA_GOT_IP` resets the
delay to the floor so the next outage backs off from the bottom.

**`sensor_wifi_*`, not `wifi_station_*`** — the latter collides at link time with
a symbol inside Espressif's closed-source `libnet80211.a`, which defines its own
`wifi_station_start()`.

### Environment facts

- **The router runs a MAC address whitelist.** Any board swap breaks Wi-Fi until
  the new MCU's MAC is added, and the symptom looks nothing like an ACL problem.
  The single most expensive fact in this file. The working board's MAC is
  `10:00:3b:b1:f1:74`.
- **Reason 202 (`WIFI_REASON_AUTH_FAIL`) means the AP actively rejected us**, not
  that the password is wrong. Here it meant the MAC was not whitelisted.
- **Reason 2 (`WIFI_REASON_AUTH_EXPIRE`) at low RSSI looks like a credentials
  fault and is not** — it is the handshake timing out.
- **One of the two ESP32-C3 Super Minis is defective on receive.** Whitelisted
  and next to the router it still gave reason 2, and the stock IDF scan example
  found no APs on it while the good board found nine. Label it physically.
- RSSI is not a useful first suspect: −78 dBm associates and holds fine.
- Reason codes are enumerated in
  `components/esp_wifi/include/esp_wifi_types_generic.h`.

**Debugging method that worked:** flashing the stock `$IDF_PATH/examples/wifi/scan`
example unmodified. It removes all project code, credentials and peripherals from
the question and answers "does this radio hear anything at all" on its own. Worth
reaching for first next time, not fifth.

### Known gaps — recorded, not fixed

- **The default country config only scans channels 1–11.** `esp_wifi.h` documents
  the default as `{.cc="01", .schan=1, .nchan=11, .policy=AUTO}` and nothing calls
  `esp_wifi_set_country()`. This AP demonstrably auto-selects its channel — seen
  on channel 3, and it came back on **channel 2** after the radio was cycled. If it
  ever lands on channel 12 or 13, both legal and in use here, the node goes
  permanently blind with reason 201 and no obvious cause — the exact symptom stage
  16 spent a session chasing. Closing it means calling `esp_wifi_set_country()`,
  which is a change rather than a stage 16 requirement.
- **Worst-case reconnect latency is ~30 s plus connect time**, measured. At stage
  17's 1 Hz publish rate that is up to ~30 s of readings with nowhere to go after
  an outage. Input to whatever stage 17 decides about buffering.

### The bring-up diagnostic, commented out

`oled_show_lines()`, the uppercase font in `oled_display.c` and the declaration in
`oled_display.h` were written so the OLED could report scan results and connection
state while the board was carried to the router, where no serial console is
reachable. Kept commented rather than deleted — uncomment all three together.
`'C'`, `'H'` and `'T'` stay live because `oled_show_readings()` needs them.

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

**Resolved at stage 16:** the DHT driver's ~25 ms critical section does **not**
disrupt Wi-Fi. The 5 s sensor loop and the Wi-Fi stack ran together through
association, a two-minute outage, a full backoff walk and reassociation, with
no dropped reads and no Wi-Fi misbehaviour attributable to the critical
section. This had been flagged as the first suspect for any Wi-Fi trouble; it
was not the cause of any of stage 16's problems.

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
- `ts_ms` — epoch **milliseconds**, publisher-stamped (requires SNTP, **stage 17** —
  moved there deliberately; stage 16's DoD is link state only)
- MQTT topic `sensors/esp32c3/telemetry`, **QoS 1**, protocol version **5.0**
- **Client ID must differ from the software publisher's**, or the broker
  disconnects one of the two
- Port 1883 is bound to 127.0.0.1 only until stage 14's compose change widens it
  for the ESP32 — check this before debugging a connection failure
