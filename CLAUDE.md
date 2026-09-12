# thermo-warren

A staged MCU telemetry pipeline. A sensor node publishes readings over MQTT to
RabbitMQ, which fans them out to two queues with deliberately different delivery
guarantees; a durable path stores them, an observation path is lossy by design.

**The stated learning goal is message broker mechanics.** Where a tradeoff arises
between "gets the pipeline working faster" and "makes broker behaviour visible",
choose the second. This is not a project optimising for shipping speed.

## Current state — two independent tracks

The plan has a software half (stages 1–13) and a hardware half (14–18). They run
in parallel and meet only at the payload contract. **There is no single "current
stage"; a scalar marker cannot describe two tracks.**

| Track | State |
|---|---|
| **Software** | **Stage 5** — software publisher, verified. Both consumers (`consumer_observe.py`, `consumer_store.py`) are still stage 2 stubs. |
| **Hardware** | **Stage 14** — toolchain and known-good flash, verified. |

Next on each: stage 6 (both consumers, manual ack, bounded prefetch) and stage 15
(DHT11 reads). Either can proceed without the other.

## Layout

| Path | Contents |
|---|---|
| `src/telemetry/` | Python: publisher, consumers, topology, config — see `src/telemetry/CLAUDE.md` |
| `firmware/` | ESP-IDF project for the ESP32-C3 — see `firmware/CLAUDE.md` |
| `tests/` | pytest, **no broker required** — keep it that way |
| `compose.yaml` | RabbitMQ, publisher, consumers |
| `mcu-rabbitmq-staged-plan.md` | The 18-stage plan, with per-stage Definitions of Done |

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
- Configuration resolved once in `config.py`, never re-read; **no literals
  outside it**.
- Topology lives in `topology_spec.py`, separate from the code that declares it.
- `seq=<int>` appears as a bare token in every log line about a message, so
  `grep -o 'seq=[0-9]*'` works as a comparison tool.
- Exit codes: `0` clean, `2` broker unreachable, `3` auth rejected.
- Tests must pass with no broker running.

## Environment facts

- **`docker compose up -d <service>` does not rebuild after a `pyproject.toml`
  change.** It silently reuses the old image. Run `docker compose build`
  explicitly after adding a dependency.
- Host is **Fedora Linux**, shell is **zsh**, user is in `dialout`.
