# firmware/ — ESP32-C3 sensor node

This directory **is** the ESP-IDF project root (`CMakeLists.txt`, `main/`,
`sdkconfig.defaults` live here directly). One project, grown additively across
stages 14 → 15 → 16 → 17. Nothing is thrown away between them.

Read the repo-root `CLAUDE.md` too — the working-style rules there apply here,
including the obligation to update this file at the end of every stage.

**Hardware track state: stage 17 done, both halves of its DoD signed off.**
The hardware track is finished; stage 18 needs both halves at once.

Verified: MQTT 5 over TCP to RabbitMQ, SNTP, 1 Hz publish of the cached DHT11
reading at QoS 1, both queues filling from the MCU with the simulator stopped,
payload matching the frozen contract, the 60 s offline gate behaving correctly
through a ~250 s Wi-Fi outage, and network clients starting on
`IP_EVENT_STA_GOT_IP`.

Also verified: the `esp_mqtt_client_reconnect()` nudge on the recovery path
(30 ms from address to broker, twice), the outbox expiry reporting its own drops,
and both outage regimes — a 139 s outage losing a precisely located part of the
buffer, a 23 s one losing nothing.

The OLED link icon is confirmed by eye in both states.

**Flashed, effect not yet verified:** the 1 h outbox fuse (the Option A policy
change). Confirming it means one outage longer than 120 s showing **no**
`outbox expiry dropped` lines at all — that is what produced 29 of them under the
old 120 s fuse, so its absence is the test.

**Stage 17's DoD is now complete in both halves.** The second half — "the
dashboard shows real room temperature" — was signed off when the software track
reached stage 13: `esp32c3-01` renders as its own series on both panels of the
`telemetry-live` dashboard, alongside `sim-01`, at ~890 points per 15-minute
window. **No firmware change was needed for it**, and none was made; the panels
group by the `device` tag, which is the only tag there is.

The path underneath was already proven at stage 12: a query through Grafana's
datasource counted 12848 of this board's points against the simulator's 13187
over 24 h.

**Nothing in the firmware had to change for the storage write to work.** The
payload contract held exactly as frozen at stage 5, which is what building the
software half against a simulator first was for.

Stage 6 confirmed one half of the contract end to end: the MCU's messages are
consumed off both queues, parsed, and logged with matching `seq` on both paths,
against a consumer written with no knowledge of the firmware.

**Stage 7 now enforces that contract**, and this is the one stage-7 change with
a hardware consequence. `consumer_store` validates all five fields and
dead-letters anything that fails, so a firmware payload change that breaks the
contract no longer passes silently — it fills `telemetry.dlq` instead.

Two things were deliberately made permissive so the firmware cannot trip them by
accident. Readings accept a JSON int as well as a float, because `29` and `29.0`
are the same number and which one is emitted depends on the format string —
strict float matching would dead-letter valid DHT11 whole numbers after any
formatting change. And **extra fields are allowed**, so the firmware may add one
(RSSI, uptime) without the consumers having to be redeployed in lockstep.

What it will reject: a missing field, a non-finite reading, a JSON `true` where
a number belongs, or a payload that is not a JSON object.

**Stage 8 added a deadline as well as a shape, and stage 9 removed it again.**
`telemetry.store` currently carries **no TTL and no length cap** — just the
dead-letter exchange — so nothing here bounds how long an MCU message may wait.
Both experiments are one edit away in `topology_spec.py`, so the consequences
below still matter whenever either is switched back on.

While the 30 s `x-message-ttl` was live, an MCU message that sat unconsumed for
30 s was dead-lettered with `reason: expired` — nothing wrong with the payload,
it simply waited. Two consequences for the firmware side:

- **The MCU sets no Message Expiry Interval**, so its messages always take the
  queue's TTL. That is why MCU messages were the long-lived head during stage
  8's per-message TTL demonstration, and why a `docker compose run` publisher
  can never win the head against a live MCU.
- **A replayed outage backlog would be on the same clock as anything else.** The
  60 s offline gate can hand the broker up to ~60 readings at once on reconnect;
  if `consumer_store` happens to be down at that moment, that backlog starts
  dead-lettering 30 s later. With a consumer running it drains in well under a
  second (~10-13 ms per message, measured at stage 17), so this only bites when
  both halves are down at once — a stage 18 row.

See **`docs/dht-api.md`** for the sensor driver's full API, transcribed from its
source. Read it instead of guessing or searching — it also records four
behaviours that are not visible in the function signatures.

See **`docs/mechanics.md`** for the three stage 17 diagrams: the per-message
lifecycle (including the two counter-intuitive expiry transitions), the outage
timeline (which is what refutes "the gate is below the expiry, so the expiry
never fires"), and task ownership (which code runs on which task, and why the
`msg_id`->`seq` map needs a mutex).

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

**Ask for a saved log, not a paste**, for anything that takes more than a few
seconds to reproduce. Stage 17's outage test ran for minutes and the interesting
lines were separated by stretches of routine output; the pasted excerpt omitted
the window that would have settled whether the outbox expiry reported its drops,
and it had to be established from `nm` on `libmqtt.a` instead. The invocation is
in `README.md` — note that `--save-log` is a **boolean flag**, and a filename
passed after it is silently swallowed as the ELF argument.

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

Stage 17 added a third argument, `bool link_up`, and a Wi-Fi link icon — see
"OLED link icon" in the stage 17 section.

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

**Resolved at stage 17 — and the earlier note here was wrong.** There is **no**
general `reason_code` field on `esp_mqtt_event_t`; that field exists only for
connect and disconnect, inside `esp_mqtt_error_codes_t`. `esp_mqtt5_event_property_t`
carries no reason code either.

The PUBACK reason code *is* reachable, by another route: `esp_mqtt5_parse_puback()`
(`mqtt5_client.c:48`) points `event->data` at the reason-code byte with
`data_len == 1` before `MQTT_EVENT_PUBLISHED` is dispatched. `data_len == 0`
means the PUBACK omitted the code, which MQTT 5 permits and which means success.
`mqtt_publisher.c` reads it this way.

**The trap:** `MQTT_EVENT_PUBLISHED` is dispatched regardless of the code, and
the code itself is only logged at `ESP_LOGD`. Without inspecting those bytes an
unroutable publish is indistinguishable from a delivered one.

**Resolved at stage 16:** the DHT driver's ~25 ms critical section does **not**
disrupt Wi-Fi. The 5 s sensor loop and the Wi-Fi stack ran together through
association, a two-minute outage, a full backoff walk and reassociation, with
no dropped reads and no Wi-Fi misbehaviour attributable to the critical
section. This had been flagged as the first suspect for any Wi-Fi trouble; it
was not the cause of any of stage 16's problems.

## Stage 17 — MQTT publisher (verified on hardware)

`mqtt_publisher.{c,h}` (client, offline gate, `msg_id`->`seq` map) and
`time_sync.{c,h}` (SNTP). `sensor_node.c` now loops at **1 Hz**, reads the DHT
every 5th tick into a cache, and publishes the cache every tick.

**Still one task.** Stage 14 deferred "restructure into explicit tasks" to
whichever stage first needed concurrency; stage 17 does not.
`esp_mqtt_client_enqueue()` hands the network write to the MQTT client's own
task, so a second application task would buy no parallelism on this single-core
chip and would add a mutex around the cached reading for nothing.

**`enqueue()`, never `publish()`.** `esp_mqtt_client_publish()` sends in the
*calling* task and is documented as possibly blocking for several seconds
(10 s network timeout), which would stall the DHT read and OLED update sharing
this loop. `enqueue()` is the documented non-blocking form. `store=false` is
correct — that flag only matters for QoS 0.

**`seq` advances on every reading, including ones never sent.** A hole in the
published series is therefore exactly the set of readings that were lost, and
`grep -o 'seq=[0-9]*'` reads it off directly.

### Credentials

NVS namespace `mqtt`, keys `host` / `port` / `user` / `password`, read exactly
as `wifi_station.c` reads namespace `wifi`. **`wifi_creds.csv` was renamed to
`provisioning.csv`** — one file, one flash, two namespaces. `port` is `u16` so a
malformed value fails in `nvs_get_u16` naming the key instead of becoming 0.

Credentials are loaded in `sensor_mqtt_start()` at boot, **not** in the event
handler, so an unprovisioned board still panics through `ESP_ERROR_CHECK` at
startup rather than looking healthy until the first `GOT_IP`.

`nvs_partition_gen` accepts `#` comment lines (it filters them before the
`csv.DictReader`) and the `u16` encoding — both confirmed by generating an image,
not assumed.

### The outage policy, and where its reasoning was wrong

ESP-MQTT's defaults lose data silently. Verified in the v5.5.5 source:

- The outbox has **no message-count limit**. `outbox.limit` is a **byte** cap,
  defaults to 0, and every enforcement site is gated on `> 0`.
  `OUTBOX_MAX_SIZE (4*1024)` in `mqtt_config.h` is **dead code**, referenced
  nowhere — a red herring if you go looking.
- The real bound is a **per-message age**, default 30 s, swept every iteration
  of the MQTT task loop whether connected or not, and swept *before* the resend
  step — so a message can be dropped even though the link returned in that same
  iteration.
- That deletion is silent unless `MQTT_REPORT_DELETED_MESSAGES` is set.
- On the `enqueue()` path the expiry clock is **never reset**: `outbox_set_tick()`
  is called only in the `publish()` write path. A message therefore keeps its
  original enqueue timestamp through transmission, and one near the edge can be
  deleted *after the broker already has it* — a false "dropped" report. This is
  why the application gate, not the expiry, is the trustworthy record.

**Decided: the 60 s gate is the only bound that drops anything.** The outbox
expiry is set to **1 h** and `outbox.limit` to **16 KB**, both deliberately far
out of reach, so nothing the gate admitted is ever discarded by the outbox.

The argument is specific to this project. `telemetry.observe` already carries
`x-max-length: 100` with `drop-head` while `telemetry.store` is unbounded with a
DLX — **the broker is already the thing that drops**, and the asymmetry between
those two fates is the stated lesson. Delivering the whole backlog lets that
asymmetry be watched on reconnect: observe sheds its head, store keeps
everything. A firmware that dropped its own backlog would hand both queues
identically truncated data and the lesson would vanish.

Two smaller points also favour it. The gate cannot evict and the outbox is FIFO,
so what survives is the **first** minute of the outage — which is *contiguous
with the pre-outage series*, leaving no hole between what was delivered live and
what arrived late. And one drop reason keeps `grep -o 'seq=[0-9]*'`
interpretable: every hole is a gate decision, logged when it was made, with no
false positives from the expiry's untrustworthy reporting.

Replayed points land at their correct historical times because `ts_ms` is
publisher-stamped — exactly the cost stage 5 accepted SNTP for.

**The plan predicted the expiry would never fire because 60 s < 120 s. That was
wrong**, and the layers are not nested. The gate bounds how *many* messages enter
the outbox; the expiry bounds how *long* each one waits, timed from its own
enqueue. Any outage longer than 120 s therefore expires part or all of the
buffer regardless of where the gate sits. Buffering only ever rescues outages
shorter than the fuse.

**Measured over two outages in one run**, which between them cover both regimes:

| | Duration | Gate drops | Expiry drops | Replayed |
|---|---|---|---|---|
| Outage 1 | 139 s | 79 (seq 106-184) | 29 (seq 38-66) | seq 67+ |
| Outage 2 | 23 s | 0 | 0 | **all — zero loss** |

The survival boundary landed exactly where "enqueue + 120 s vs reconnect at
185.7 s" predicts, with under half a second of margin:

```
seq 65: queued 65059ms  fuse 185059ms  EXPIRED
seq 66: queued 66099ms  fuse 186099ms  EXPIRED  (link already back up)
seq 67: queued 67109ms  fuse 187109ms  acked @186689  <- survived by 420 ms
```

**seq 66 expired 430 ms *after* the link returned**, while queued behind the
replay backlog — the `QUEUED -> expired` race against `QUEUED -> TRANSMITTED`
actually happening, not just theoretically possible.

Loss is never silent: 29 `outbox expiry dropped` lines were logged, so the
reporting branch works (also confirmed statically — `nm` on `libmqtt.a` shows
`outbox_delete_single_expired` is called and the silent `outbox_delete_expired`
is never referenced).

**This measurement is what the policy decision was made on, and it describes the
old 120 s configuration.** Under the 1 h fuse the expected behaviour is: the gate
admits ~60 readings, all of them are replayed on reconnect however long the
outage lasted, and `outbox expiry dropped` never appears. That log line is now
`ESP_LOGE` and reads "should not happen" — under the old fuse it fired
routinely, which made it worthless as a signal.

### The gate's clock starts late — an 8.4 s blind window

Not anticipated, and a real limitation of keying the gate on MQTT session state.
In outage 1 the last successful ack was **seq 37**, but the Wi-Fi driver only
reported the loss at t=46.4 s (`reason=200`, beacon timeout, `bcn_timeout: 25000`
in the driver log). For those ~8 s `s_connected` was still true, so:

- the gate did not apply,
- publishes logged as ordinary `queued seq=N` with no `(offline, within gate)`
  marker — **the log looked healthy while nothing was reaching the broker**,
- seq 38-45 were never acked and eventually expired out of the outbox.

Those eight are also the `TRANSMITTED -> expired` transition observed in the
wild: sent into a dead link, never acked, and deleted carrying their original
enqueue timestamps.

The gate's 60 s therefore runs from *when the driver notices*, not from when the
link fails. Real worst case is ~60 s plus the beacon timeout.

### enqueue() has three outcomes, logged separately

| Return | Meaning | Who is dropped |
|---|---|---|
| `msg_id` | queued | — |
| `-1` | MQTT 5 in-flight cap: unacked QoS>0 count above the broker's Receive Maximum | the **new** message |
| `-2` | `outbox.limit` bytes exceeded | the **new** message |

Note the asymmetry, which mirrors stage 9: the expiry discards the **oldest**,
these two reject the **newest**. esp-mqtt defaults Receive Maximum to 65535 and
lowers it only if CONNACK carries the property; RabbitMQ's MQTT plugin sets no
such value, so `-1` is very unlikely here.

Pre-CONNACK defaults are `max_qos = 2` and `receive_maximum = 65535`, which is
why enqueueing works before the client has ever connected.

### sdkconfig.defaults additions

| Symbol | Value | Why |
|---|---|---|
| `CONFIG_MQTT_PROTOCOL_5` | `y` | Gates `mqtt5_client.c` into the build at all. Without it the client speaks 3.1.1, which has **no error channel** — the PUBACK reason code never arrives. |
| `CONFIG_MQTT_REPORT_DELETED_MESSAGES` | `y` | Makes outbox expiry visible. Standalone symbol, no dependency on custom config. |
| `CONFIG_MQTT_USE_CUSTOM_CONFIG` | `y` | Required only to reach the expiry timeout. |
| `CONFIG_MQTT_OUTBOX_EXPIRED_TIMEOUT_MS` | `3600000` | Memory backstop, **not** a data policy — see the outage policy above. Was `120000`, which fired routinely. |
| `CONFIG_MQTT_TRANSPORT_SSL` / `_WEBSOCKET` | `n` | Both unused; worth 13,552 bytes, measured. Does not remove mbedtls — WPA2 still needs it. |

**Enabling `MQTT_USE_CUSTOM_CONFIG` is behaviour-neutral**: every value it gates
has an identical fallback in `mqtt_config.h`. Confirmed after regeneration —
buffer 1024, stack 6144, priority 5, poll 1000 ms, event queue 1, TCP port 1883
all unchanged.

Coupling worth knowing: the expiry constant also bounds how long an already
transmitted message waits for its PUBACK.

### Start both network clients on IP_EVENT_STA_GOT_IP

Both SNTP and MQTT were originally started from `app_main`, before the
interface had an address, and both paid a retry timeout for it:

- **MQTT** burns a connect attempt into a dead route (`esp-tls: connect() error:
  Host is unreachable` at t=389 ms) and then waits out its ~10 s reconnect timer.
- **SNTP** is worse. lwIP waits a random 0–5 s before its first request
  (`CONFIG_LWIP_SNTP_STARTUP_DELAY`, max 5000 ms here); if that request goes out
  before there is a link it gets no reply, and `SNTP_RETRY_TIMEOUT` is 15 s,
  **doubling** per failure to a 150 s cap.

Measured cost: one boot synced 2.7 s after DHCP and lost 5 readings; another
synced >14 s after and lost 21, purely on where the random startup delay landed.
On outage recovery, DHCP completed at t=339919 ms and the broker was not reached
until t=352899 ms — **13 s and 12 readings lost with the network already up.**

Now: `time_sync_start()` and `sensor_mqtt_start()` validate configuration and
register for `GOT_IP`; neither service starts until there is an address. Later
`GOT_IP` events restart SNTP (its retry timer may have doubled out to 150 s) and
call `esp_mqtt_client_reconnect()` (guarded on `!s_connected`). Both calls are
non-blocking, so doing this on the default event-loop task does not violate
stage 16's rule about never stalling that loop.

Order still matters: `sensor_wifi_start()` creates the default event loop the
other two register against.

**Verified on hardware, and the improvement is large:**

| | Before | After |
|---|---|---|
| DHCP -> broker connected | ~13,400 ms | **30 ms** (6299 -> 6329) |
| DHCP -> clock set | up to ~14 s | **2.78 s** |
| Readings lost at boot | 5 to 21 | **9, all waiting on SNTP** |

The `esp-tls: connect() error` line is gone entirely, and `sntp started` /
`mqtt client started (address acquired)` both land in the same millisecond as
`got ip`. The remaining skipped `seq` are Wi-Fi association (`reason=2`, one
retry) plus the SNTP round trip — no longer anything self-inflicted.

**The reconnect nudge is verified too.** It fired twice in the outage run, and
both times `got ip` -> `address reacquired, reconnecting now` -> `connected to
broker` took **30 ms** (185639 -> 185669, and 221199 -> 221229), against the
~13,000 ms this path cost before the change.

### Include-order trap

`mqtt5_client.h` and `mqtt_client.h` include each other, and with
`CONFIG_MQTT_PROTOCOL_5=y` the latter pulls in the former itself. Including
`mqtt5_client.h` first wins the include guard and leaves `mqtt_client.h`
compiling against types it has not seen — `unknown type name
'esp_mqtt5_event_property_t'`, which reads as a missing dependency rather than
an ordering problem. **Include only `mqtt_client.h`.**

### Measured on hardware

- **Outbox replay is fast.** ~13 ms per message at RSSI -76 (11 messages in
  140 ms), and ~10 ms per message at -75 during the outage run's replay of ~40
  backlogged messages. The 1 s `MQTT_POLL_READ_TIMEOUT_MS` floor was the
  suspected risk and is not reached: PUBACK traffic keeps the poll returning
  early. At RSSI -83 the same acks took ~400 ms, so treat 10-13 ms as a best
  case and ~400 ms as the degraded one.
- **Replay does not save a message whose fuse expires mid-drain.** seq 66 died
  430 ms into the replay. At 1 Hz production and ~100/s drain the backlog clears
  in well under a second, so this only bites messages already at their fuse.
- **RSSI varies a lot here** — -76, -83 and -87 across runs, and association at
  -87 needed a retry (`reason=203`, `WIFI_REASON_ASSOC_FAIL`). Stage 16 recorded
  -78 as fine; -87 is marginal.
- The AP moved from **channel 3 to channel 2** across the outage, confirming the
  recorded auto-select behaviour and the channel-12/13 risk in "Known gaps".
- Boot `seq` values are permanently absent from the broker (clock not yet
  synced). Expected, and visible as a leading gap.

### Loop cadence

`vTaskDelay` is relative, so the period is 1 s *plus* loop execution time and
drifts slightly. Harmless: `ts_ms` is stamped per message, never inferred from
cadence. `vTaskDelayUntil` would fix it if it ever matters. Recorded, not fixed.

### OLED link icon (verified)

**Verified on hardware in both states.** `oled_show_readings()` takes a
`bool link_up` and draws a three-arc Wi-Fi fan in the top-right, struck through
with a diagonal when the broker session is down.
Driven by `sensor_mqtt_is_connected()` — MQTT state, not Wi-Fi state, since that
is what decides whether readings reach the broker. Refresh is the 5 s read
cadence, and only after a *successful* DHT read, so the marker goes stale if the
sensor stops responding.

A drawn icon rather than a word because the font covers only the characters
"Temp"/"Humidity" need. The arcs are **generated** — a one-off script taking
points within 0.6 px of radius 3, 6 and 9 from the apex inside a ~120 degree
cone — and pasted in as an ASCII grid, the same approach and reasoning as the
font table.

Two things learned, both commented in `oled_display.c`:

- **11x9 is too small.** A 1 px strike through 1 px arcs merges into the pattern
  and reads as a smudge. Two attempts were discarded before 15x10.
- **The strike needs a halo and two passes** — clear every halo pixel before
  setting any line pixel. In one pass each point's halo erases the line pixel its
  predecessor drew, leaving a dashed strike.

### App partition is nearly full — and the chip has room

`sensor-node.bin` is at **4% free** of the stock 1 MB app partition.

**The chip is 4 MB, confirmed** by `esptool flash_id`: ESP32-C3 (QFN32) rev
v0.4, "Embedded Flash 4MB (XMC)", manufacturer 46 device 4016, 40 MHz crystal.
The build nevertheless sets `CONFIG_ESPTOOLPY_FLASHSIZE_2MB` and the stock
single-app table gives the app 1 MB — so roughly 3 MB of the chip is unused.

The fix is `CONFIG_ESPTOOLPY_FLASHSIZE_4MB` plus a custom partition table
enlarging the `factory` app partition. **`nvs` must stay at offset `0x9000` at
24 KB** or the provisioned Wi-Fi and broker credentials are lost and need
re-flashing; the app partition starts at `0x10000` and only needs its *size*
changed, so nothing below it has to move.

**Still parked** as a change of its own, but no longer blocked on information.

## The payload contract (stage 17)

Frozen at stage 5, and **enforced by the consumers from stage 7** — a payload
that fails it is dead-lettered into `telemetry.dlq`, not merely logged. Firmware
must satisfy it exactly; not open to renegotiation. The contract lives in
`src/telemetry/payload.py`, and the rationale for each field is in
`src/telemetry/CLAUDE.md`.

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
- `temp_c` / `humidity_pct` — **floats**, matching `dht_read_float_data()` so no
  conversion is needed. Note the driver's argument order is **humidity first**,
  the opposite of this field order. Both must be finite.

  **Keep emitting floats, and stage 11 turned the reason into a measured one.**
  The consumer tolerates a bare `29` rather than `29.0` so that a format-string
  change cannot silently dead-letter good readings — a safety net, not
  permission to change what the firmware sends. **Verified at stage 11**:
  InfluxDB rejects an integer written to a field already created as a float with
  **HTTP 422**. The consumer's `storage.to_point()` now coerces with `float()`,
  so an integer would in fact survive — but that coercion exists to keep the
  tolerance a dead-lettering decision, not to license integers on the wire. A
  DHT11 yields whole numbers (`24.0`, never `24.4`), so the formatting is the
  only thing standing between this and the trap.
- `ts_ms` — epoch **milliseconds**, publisher-stamped; needs SNTP, implemented at
  stage 17 in `time_sync.c`
- MQTT topic `sensors/esp32c3/telemetry`, **QoS 1**, protocol version **5.0**
- **Client ID must differ from the software publisher's**, or the broker
  disconnects one of the two. Implemented: client id `sensor-node-01`,
  `device` field `esp32c3-01`, against the simulator's `telemetry-sim` / `sim-01`.
- **Port 1883 now publishes on `0.0.0.0`** (`compose.yaml`), widened at stage 17
  because the ESP32 needs it from the LAN. This exposes the `iot` user, which
  still carries the `administrator` tag; the answer to that is a scoped
  application user, not a bind address.

**Verified against a real MCU message at stage 17:** same keys, same order, same
types as `build_payload()`, `delivery_mode: 2` confirming QoS 1 became a
persistent AMQP message, and routing key `sensors.esp32c3.telemetry`. Only
`device` and the reading values differ. DHT11 whole numbers came through as
`29.0` / `53.0` exactly as the driver's behaviour predicted.

Also settled: the MQTT 5 **Content Type property does map through** to AMQP
`content_type` (`application/json` observed on the queued message), and it
**survives dead-lettering** — it is still on the message in `telemetry.dlq`.

**What will now dead-letter an MCU payload** (stage 7): a missing field, a
non-finite reading, a `true` where a number or integer belongs, a string where a
number belongs, or a body that is not a JSON object. **Extra fields are
allowed** — the firmware may add one, such as RSSI or uptime, without the
consumers needing to be redeployed in lockstep. That permissiveness is
deliberate: firmware and consumers are deployed separately and cannot be updated
together.
