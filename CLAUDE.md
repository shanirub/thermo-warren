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
| **Software** | **Stage 6** — both consumers real and verified: manual ack and bounded prefetch on the durable path, auto-ack on the lossy one. `stub.py` deleted. |
| **Hardware** | **Stage 17** — MQTT 5 publisher, SNTP, a chosen outage policy and an OLED link icon, all verified on hardware. The firmware half of stage 17 is complete; the stage's DoD also needs a dashboard, which waits on the software track. |

**The two tracks have now met, and the parallelism ends here.** Hardware is done
through stage 17; stage 18 (end-to-end resilience) is the first stage that needs
*both* halves, so it cannot start until software reaches stage 13. There is no
independent hardware work left to schedule.

Next: **stage 7** on the software track (dead-lettering trigger 1: the durable
consumer rejects unparseable messages without requeueing), then 8-13. Stage 17's
own DoD gets its second half signed off when stage 13 lands — the MCU side is
already proven and needs no rework for it.

Stage 7 is a one-line change by construction: `consumer_store`'s parse-failure
branch already exists and already logs, so `basic_ack` becomes
`basic_nack(requeue=False)`. The test asserting the current behaviour is meant
to be inverted, not deleted.

## Layout

| Path | Contents |
|---|---|
| `src/telemetry/` | Python: publisher, consumers, topology, shared AMQP plumbing, config — see `src/telemetry/CLAUDE.md` |
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
