# thermo-warren

A staged MCU telemetry pipeline. A sensor node publishes readings over MQTT to
RabbitMQ, which fans them out to two queues with deliberately different delivery
guarantees; a durable path stores them, an observation path is lossy by design.

**The stated learning goal is message broker mechanics.** Where a tradeoff arises
between "gets the pipeline working faster" and "makes broker behaviour visible",
choose the second. This is not a project optimising for shipping speed.

## Layout

| Path | Contents |
|---|---|
| `src/telemetry/` | Python: publisher, consumers, topology declaration, config |
| `firmware/` | ESP-IDF project for the ESP32-C3 (see `firmware/CLAUDE.md`) |
| `tests/` | pytest, **no broker required** — keep it that way |
| `compose.yaml` | RabbitMQ, publisher, consumers |
| `mcu-rabbitmq-staged-plan.md` | The 18-stage plan |
| `handover-*.md` | Per-session records of decisions and verification |

The Python half and the firmware half meet at exactly one place: the JSON payload
contract, frozen at stage 5. Nothing else couples them.

## The staged plan is the unit of work

`README.md` carries a **"Current stage"** marker. Work belongs to one stage at a
time, and the marker is updated only after that stage's Definition of Done has
been verified against real behaviour, not against a passing build.

**Do not refactor beyond the scope of the current stage.** Improvements that
belong to a later stage get noted, not implemented.

## Settled decisions stay settled

The `handover-*.md` files are authoritative. Their "Decisions made" and "Verified"
sections must not be re-opened, re-litigated or re-searched. If something in them
looks wrong, say so and stop — do not silently work around it.

Where a handover says something was **not** verified, treat it as unknown rather
than as probably fine.

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
- Configuration is resolved once in `config.py` and never re-read; **no literals
  outside it**.
- Topology lives in `topology_spec.py`, separate from the code that declares it —
  the deliberate choices in one readable file, apart from the machinery.
- `seq=<int>` appears as a bare token in every log line about a message, across
  publisher and consumers, so `grep -o 'seq=[0-9]*'` works as a comparison tool.
- Exit codes: `0` clean, `2` broker unreachable, `3` auth rejected.
- Tests must pass with no broker running.

## Environment facts learned the hard way

- **`docker compose up -d <service>` does not rebuild after a `pyproject.toml`
  change.** It silently reuses the old image. Run `docker compose build`
  explicitly after adding a dependency.
