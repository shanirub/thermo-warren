# thermo-warren

A staged MCU telemetry pipeline. A sensor node publishes readings over MQTT to
RabbitMQ, which fans them out to two queues with deliberately different delivery
guarantees; a durable path stores them, an observation path is lossy by design.

**The stated learning goal is message broker mechanics.** Where a tradeoff arises
between "gets the pipeline working faster" and "makes broker behaviour visible",
choose the second. This is not a project optimising for shipping speed.

## Current state — both tracks done, one stage left

The plan had a software half (stages 1–13) and a hardware half (14–18), run in
parallel and meeting only at the payload contract. **Both halves are now
complete.** The two-track bookkeeping is over: stage 18 is the only stage left,
it needs both halves, and it is the single current stage.

| Track | State |
|---|---|
| **Software** | **Stage 13 — done, the track is finished.** A dashboard provisioned from a file: temperature and humidity, one series per device, 15 minutes at a 5 s refresh, uid `telemetry-live`. Grafana reads the pipeline back. Its datasource is provisioned from `grafana/provisioning/`, and a one-shot `dbrp` service declares the InfluxQL database. Underneath: `consumer_store` writes to InfluxDB between the parse and the ack, so the pipeline is at-least-once end to end. Measurement `readings`, `device` the only tag. A failed write retries three times and then requeues; it never dead-letters, so `telemetry.dlq` still means "failed the payload contract" and nothing else. All three dead-letter triggers were demonstrated at stages 7-9. |
| **Hardware** | **Stage 17 — done.** MQTT 5 publisher, SNTP, a chosen outage policy and an OLED link icon, all verified on hardware. **The DoD's second half is signed off**: the stage 13 dashboard renders `esp32c3-01` alongside `sim-01`, and no firmware rework was needed for it. |

Next: **stage 18**, end-to-end resilience. It is the first stage that builds
nothing — the pipeline is broken deliberately and watched, with the stage 13
dashboard as the instrument. What to have in hand before starting is recorded
under "Check first at stage 18" in `src/telemetry/CLAUDE.md`.

**The DBRP mapping did not have to be created.** InfluxDB 2.x synthesises a
read-only virtual one for any bucket without an explicit mapping, and InfluxQL
worked through it before stage 12 wrote anything — verified against the running
instance. It is declared explicitly anyway, on the "explicit over inherited"
rule. The plan's stage 12 text reads as though creating it were forced; it was
not.

**The ESP32 is already writing to InfluxDB.** It was powered on during stage 11
verification, and `esp32c3-01` appears alongside `sim-01` as a second series with
its own independent `seq`. Nothing had to change for that: the payload contract
held, and making `device` the only tag was what kept the two apart.

**A note on predicting the next stage.** Stage 6 recorded here that "stage 7 is
a one-line change by construction". It was not: choosing to validate the whole
payload contract rather than only parse failures added a module, and the call
turned out to be `basic_reject` rather than the predicted `basic_nack`. The
prediction was written as though it were a decision, and it was neither
reviewed nor re-opened before being acted on. **Record decisions, not forecasts
of stages not yet planned.**

## Layout

| Path | Contents |
|---|---|
| `src/telemetry/` | Python: publisher, consumers, topology, shared AMQP and storage plumbing, config — see `src/telemetry/CLAUDE.md` |
| `firmware/` | ESP-IDF project for the ESP32-C3, with `docs/` for the driver API, diagrams and a photo of the assembled node — see `firmware/CLAUDE.md` |
| `tests/` | pytest, **no broker and no database required** — keep it that way |
| `rabbitmq/` | broker config and enabled plugins; an unknown key aborts startup |
| `grafana/provisioning/` | datasource (stage 12) and dashboard (stage 13), read-mounted; Grafana keeps no state of its own |
| `influxdb/dbrp.sh` | one-shot declarer for the InfluxQL database mapping |
| `compose.yaml` | RabbitMQ, InfluxDB, Grafana, publisher, consumers, two one-shot declarers |
| `mcu-rabbitmq-staged-plan.md` | The 18-stage plan, with per-stage Definitions of Done |
| `docs/verification-log.md` | How every stage was checked, newest first — the commands, moved out of `README.md` at stage 13 |
| `src/telemetry/docs/` | The stage 13 dashboard image |

## These CLAUDE.md files are the decision record

There are no `handover-*.md` files and there should not be. Decisions, verified
facts and hard-won environment quirks live in the `CLAUDE.md` file nearest the
code they apply to. Nothing needs to be pasted in at the start of a session.

**This imposes an obligation: at the end of every stage, update the relevant
`CLAUDE.md` before committing.** Record what was decided and why, what was
actually verified as distinct from assumed, and anything learned about the
environment. A decision that exists only in a chat transcript is lost.

Keep entries compact and written as current-state reference, not as narrative.
"Why" matters; "what we tried third" usually does not.

**Three documents, three jobs**, settled at stage 13 when the README's
verification sections were moved to `docs/verification-log.md`:

| Document | Holds |
|---|---|
| `mcu-rabbitmq-staged-plan.md` | each stage's Definition of Done — what "done" means before the work starts |
| `CLAUDE.md`, one per directory | what was verified, what it proved, what was decided. **The decision record** |
| `docs/verification-log.md` | how to re-run it: the commands, newest stage first |

The split is what stops any one of them growing without bound. The README had
become an append-only log ten times the length of the page it was appended to;
the verification log is allowed to grow that way, because that is all it is.

**Settled decisions stay settled.** Anything recorded here is not to be
re-opened, re-litigated or re-searched. If something looks wrong, say so and
stop — do not silently work around it. Where a file says something was **not**
verified, treat it as unknown rather than as probably fine.

## The staged plan is the unit of work

Work belongs to one stage at a time, on one track. A stage is done when its
Definition of Done has been verified against real behaviour, not against a
passing build.

**Do not refactor beyond the scope of the current stage.** Improvements
belonging to a later stage get recorded, not implemented.

## Working style

- **One open question at a time, easiest first.** Batching has been offered and
  declined. Present each as: what is already fixed, what is worth flagging, then
  the live choice with a leaning and its reasoning.
- **Propose, don't assume.** Filenames, layout and library APIs are open and
  should be proposed and confirmed rather than chosen unilaterally.
- **Explicit over inherited, always** — including where a value matches the
  library or broker default, with an inline comment saying it is explicit for
  clarity.
- **Non-obvious constants get an inline comment giving the reason for the value.**
- **Say plainly when something was not verified** rather than presenting it as
  settled. Distinguish "I checked and it is X" from "I expect X".
- **Cross-check library APIs against the installed source**, not memory or
  tutorials. This has caught real signature drift more than once.
- Reason your suggestions. Do not be overconfident.
- Use professional/technical terms, explain them briefly inline, offer to
  elaborate.

## Python conventions

- Dependencies via **uv**; `pyproject.toml` is the source of truth.
- Configuration resolved once in `config.py`, never re-read.
- **No literals outside the three files that own them**: `config.py` (what
  differs between run modes), `topology_spec.py` (what the broker enforces) and
  `payload.py` (what publisher and consumers must agree on, which the broker
  never inspects).
- Topology lives in `topology_spec.py`, separate from the code that declares it.
- `seq=<int>` appears as a bare token in every log line about a message, so
  `grep -o 'seq=[0-9]*'` works as a comparison tool.
- Exit codes: `0` clean, `2` broker unreachable, `3` auth rejected — shared in
  `amqp.py`. Two are owned by the one module each means something to: `4`
  topology mismatch (`topology.py`), `5` storage unconfigured
  (`consumer_store.py`, an empty `INFLUXDB_TOKEN`). Note `3` is the *broker*
  refusing supplied credentials, which `5` is not.
- Tests must pass with no broker running.

## Environment facts

- **`docker compose up -d <service>` does not rebuild after a `pyproject.toml`
  change.** It silently reuses the old image. Run `docker compose build`
  explicitly after adding a dependency.
- Host is **Fedora Linux**, shell is **zsh**, user is in `dialout`.
