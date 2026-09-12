# Handover — Stage 14: firmware toolchain and known-good flash

Date: 2026-09-12

No prior `handover-*.md` file exists to match style against — see "Discrepancy
noticed" below. This one follows the structure the stage-14 task instructions
asked for: decisions, files, verification (performed vs. assumed), environment
facts, open questions, next steps.

## Decisions made (settled — do not re-open)

Decided one at a time with the user, per the project's working style:

1. **Project name: `sensor-node`.** Chosen over `thermo-warren-firmware` and
   `esp32c3-sensor-node` because root `CLAUDE.md` already describes this half
   as "a sensor node" — the name describes what it is, not which chip or
   protocol it currently uses, so it doesn't need a rename as stages 15-17
   add the DHT11, Wi-Fi and MQTT.
2. **Counter runs directly in `app_main`**, not a separate FreeRTOS task.
   `app_main` already executes as the FreeRTOS "main task", so a
   loop-with-`vTaskDelay` in it is already a task; a second one buys nothing
   at this stage. Restructuring into an explicit task is deferred to whichever
   of stages 15-17 first needs real concurrency (e.g. DHT11 read vs. OLED
   update both wanting the CPU).
3. **Log tag `sensor_node`, level `ESP_LOGI`.** Tag matches the project name
   for unambiguous `grep`/filtering once more subsystems (e.g. `dht11`,
   `wifi`) get their own tags later. Info is the right level for a
   heartbeat-style counter meant for visual confirmation on the console.
4. **Firmware gets its own `firmware/README.md`**, not a section folded into
   the root README. The firmware half has its own toolchain, environment-
   sourcing step and a manual-download-mode recovery procedure that are
   irrelevant to the Python side; the root README keeps a one-line pointer to
   it (mirrors how it already points to `firmware/CLAUDE.md`).

Also decided mid-session, not in the original four:

5. **DHT11 sensor was left with VCC+GND connected (DATA floating) rather than
   fully disconnected**, after I flagged that this deviates from the stage-14
   instruction that the sensor "stays physically disconnected for this entire
   stage." The user made an explicit, informed call to proceed as-is rather
   than disconnect further. I have **not** independently verified polarity on
   this wiring — see "Carried forward" below, this compounds directly with
   the existing stage-1 reverse-polarity concern.

## Files created or changed

All under `firmware/`, which is the ESP-IDF project root. Nothing outside
`firmware/` was touched except `README.md` at the repo root (stage marker
only — see below).

| File | What |
|---|---|
| `firmware/CMakeLists.txt` | Top-level project file, `project(sensor-node)` |
| `firmware/main/CMakeLists.txt` | Component registration for `main` |
| `firmware/main/sensor_node.c` | `app_main`: counter loop, `ESP_LOGI`, no sensor/GPIO/Wi-Fi/MQTT reference |
| `firmware/sdkconfig.defaults` | Committed via `idf.py save-defconfig`, then hand-commented. Holds `CONFIG_IDF_TARGET="esp32c3"` and `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` |
| `firmware/.gitignore` | Ignores `sdkconfig`, `sdkconfig.old`, `build/`, `managed_components/`, `dependencies.lock` |
| `firmware/README.md` | New — firmware-specific build/flash/verify steps, manual download mode recovery |
| `README.md` (repo root) | "Current stage" marker moved to 14; layout table's `firmware/` row now points at `firmware/README.md` |

`firmware/sdkconfig` and `firmware/build/` exist locally (generated) but are
gitignored, as required.

## Verification actually performed (not assumed)

- `echo $IDF_PATH` → `/home/srub/esp/esp-idf-v5.5.5`; `idf.py --version` →
  `ESP-IDF v5.5.5`. Confirmed the correct checkout was active, not the
  unrelated 6.x checkout at `~/esp/esp-idf`.
- `idf.py set-target esp32c3` and `idf.py build` — both succeeded, I ran and
  watched the output directly.
- The console setting: default `sdkconfig` after `set-target` had
  `CONFIG_ESP_CONSOLE_UART_DEFAULT=y` (confirmed by `grep`, not assumed) —
  i.e. left at default it really would build fine and show nothing on
  monitor, matching what `firmware/CLAUDE.md` warns about. I edited
  `sdkconfig` to flip the choice, ran `idf.py reconfigure`, and confirmed by
  `grep` that Kconfig auto-resolved the secondary console to `NONE` and
  dropped `CONFIG_ESP_CONSOLE_UART` entirely (consistent with the `Kconfig`
  source at `components/esp_system/Kconfig`, which I read directly rather
  than assuming the dependency behaviour).
- **Fresh-clone proof for `sdkconfig.defaults`**: deleted `sdkconfig` and
  `sdkconfig.old` entirely (stronger than `idf.py fullclean`, which only
  clears `build/` and leaves `sdkconfig` in place), then ran `idf.py build`
  with nothing but `sdkconfig.defaults` on disk. It rebuilt for `esp32c3`
  (target guessed from `sdkconfig.defaults`, confirmed against
  `tools/cmake/targets.cmake`'s `__target_init` logic — not assumed) and the
  regenerated `sdkconfig` again had `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`.
  Then re-ran the literal DoD sequence, `idf.py fullclean && idf.py build`,
  which also succeeded.
- `idf.py -p /dev/ttyACM0 flash` — **run by the user**, not by me (my Bash
  sandbox could not see `/dev/ttyACM0` even though `lsusb`, run by the user,
  showed the board enumerated as `303a:1001 Espressif USB JTAG/serial debug
  unit`). User reported success, after an unplug/replug cycle.
- `idf.py -p /dev/ttyACM0 monitor` — **run by the user**, per instruction not
  to run monitor myself (it's interactive and never exits). User pasted the
  full output: boot log, then `sensor_node: counter=0` through `counter=18`
  at 1000 ms intervals (timestamps `71, 1071, 2071, …` ms), confirming the
  full Definition of Done — build, flash, and a counter incrementing roughly
  once a second over USB Serial/JTAG.

**Not verified by me**: DHT11 polarity on the current V+GND wiring (see
Carried forward). I did not run `idf.py flash` or `idf.py monitor` myself at
any point — both were run by the user per the task's explicit instruction.

## Environment facts learned

- `echo $IDF_PATH` is a reliable, cheap check that the right ESP-IDF checkout
  is active; confirmed working as described in `firmware/CLAUDE.md`.
- Piping the `export.sh` sourcing command through another command (e.g.
  `. export.sh | tail`) runs the `source` in a subshell in this shell, so the
  exported variables (`IDF_PATH`, `PATH` additions) do **not** persist to
  later commands in the same tool call. Source it as a standalone statement,
  chained with `&&`/`;`, never piped.
- `idf.py fullclean` removes `build/` but does **not** remove `sdkconfig`.
  Proving that `sdkconfig.defaults` (not a stale `sdkconfig`) is what drives
  the configuration requires deleting `sdkconfig` itself, not just
  `fullclean`.
- Setting `CONFIG_IDF_TARGET="esp32c3"` in `sdkconfig.defaults` (which
  `save-defconfig` does automatically after a `set-target`) means a fresh
  clone's first `idf.py build` resolves the correct target on its own —
  confirmed against `tools/cmake/targets.cmake` (`__target_init` falls back
  to reading `CONFIG_IDF_TARGET` from `sdkconfig`/`sdkconfig.defaults` when
  no environment variable or CMake cache entry is set). `idf.py set-target`
  is still worth running explicitly on a fresh clone for clarity, but isn't
  strictly required by the build.
- The `mcp-api-doc` skill referenced in `firmware/CLAUDE.md` for API
  verification is **not available** in this environment (not in the skill
  list, not found via tool search). Substituted by grepping the installed
  ESP-IDF source directly (`components/log/include/esp_log.h`,
  `components/freertos/.../task.h`, `.../projdefs.h`) for `ESP_LOGI`,
  `vTaskDelay`, `pdMS_TO_TICKS` before using them.
- This Bash tool's sandbox cannot see `/dev/ttyACM*` even when the board is
  genuinely connected and enumerated (`lsusb` from the user's own shell
  showed it). Flash and monitor had to be run by the user throughout this
  session, not just monitor.
- The board's USB Serial/JTAG port disappears and re-enumerates on reset, as
  `firmware/CLAUDE.md` describes — the user needed one unplug/replug cycle
  before `idf.py flash` could find `/dev/ttyACM0`.

## Discrepancy noticed (flagged, not silently worked around)

`CLAUDE.md` (root) and `firmware/CLAUDE.md` both reference
`mcu-rabbitmq-staged-plan.md` and prior `handover-*.md` files as existing,
authoritative documents. Neither exists anywhere in the repo or its git
history (`git ls-files`, `find`, and `git log --all` all confirm this). This
did not block stage 14, since the task instructions supplied the stage's
scope and Definition of Done directly, but it means:

- I could not cross-check stage 14's scope against the staged plan's own
  text, only against what was given inline.
- I had no existing handover document to match formatting/style against for
  this one.
- I cannot confirm from the plan file whether jumping the root README's
  "Current stage" marker from 5 straight to 14 (skipping stages 6-13, which
  appear to be the software consumer stages, still stubs) is the plan's
  intended interleaving of the hardware and software tracks, or a gap. I
  followed the task's explicit instruction that the marker "reads 14" as
  given, and added a note in the root README clarifying the software track's
  own state is unchanged since stage 5.

Worth resolving before stage 15, if `mcu-rabbitmq-staged-plan.md` exists
somewhere outside this repo.

## Carried forward (explicit, per instruction)

- **DHT11 sensor health is still unconfirmed.** A reverse-polarity event
  occurred during stage 1; resistance re-measurements afterward were
  unchanged (ruling out a rail-to-rail short, not proving the die responds).
  A spare is on order. **This session added a new factor**: VCC+GND were
  reconnected during stage 14's flash/monitor test (DATA left floating), on
  the user's explicit decision after I flagged the deviation from "stays
  physically disconnected." I have not verified polarity on this connection
  myself. If stage 15's first DHT11 reads fail, both a pre-existing dead
  sensor *and* a repeat polarity issue from this session are live hypotheses
  — don't assume a driver bug before checking wiring.
- **DHT11 data GPIO (6 or 7) is not yet chosen.** Both remain free with no
  ADC function per the GPIO map in `firmware/CLAUDE.md`; the physical layout
  decides. Not touched this stage — no GPIO was referenced in
  `sensor_node.c` at all, per stage 14's scope.

## Next steps

- Stage 15: `esp-idf-lib/dht` driver, `idf.py add-dependency`, choice of
  GPIO 6 vs 7, first sensor reads. Confirm DHT11 wiring (polarity, and
  whether the data line even gets connected now) before assuming any read
  failure is a driver bug, given the two live hypotheses above.
- Consider resolving the missing `mcu-rabbitmq-staged-plan.md` /
  `handover-*.md` discrepancy before relying on stage numbering beyond what
  is given explicitly per-session.
