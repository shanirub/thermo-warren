# src/telemetry/ — the broker half

Python side of the pipeline: topology declaration, the software publisher, and
the two consumers. Speaks AMQP via `pika`, except the publisher, which speaks
MQTT via `paho-mqtt`.

Read the repo-root `CLAUDE.md` too — the working-style rules there apply here,
including the obligation to update this file at the end of every stage.

**Software track state: stage 5 done and verified.** Next is stage 6.

`consumer_observe.py` and `consumer_store.py` are still **stage 2 stubs**.
`stub.py` still exists and is still called by both; it is deleted once the last
caller is gone, which is **at the end of stage 6**.

## Architecture decisions (settled — do not re-open)

- **Python + `pika`, hand-written.** Not a bridge tool — writing the AMQP client
  is the exercise.
- **Fan-out topology**: two queues bound to `amq.topic` with the same routing
  key. The durable path has a DLX and manual acks; the observation path has no
  DLX, is bounded, and is deliberately lossy. **The asymmetry is the point** —
  same message, two fates.
- **Classic queues, not quorum.** Quorum does not support `reject-publish-dlx`,
  which stage 9 needs.
- **JSON payload with a monotonic sequence number**, not line protocol. Forces
  real parsing, so malformed input is a natural poison-message case, and makes
  at-least-once duplicates detectable.
- **MQTT QoS 1**, chosen for what it does inside the broker rather than on the
  wire — QoS 1 publishes become *persistent* AMQP messages, QoS 0 become
  transient. Transient messages in a durable queue vanish on broker restart,
  which would make the stage 18 resilience tests meaningless.
- **Topology declared by code as a one-shot job** that must complete before any
  publisher starts, enforced by orchestration rather than discipline.
- **Software publisher before firmware.** The whole broker pipeline is built and
  proven against a simulated publisher; hardware comes last and must satisfy the
  already-validated contract.
- **Dual run modes**: containerised full stack, and modules runnable directly on
  the host against exposed ports.
- **InfluxDB 2.x with a pinned tag** (stage 10), not 1.8 and not 3 Core.
  **InfluxQL, not Flux**, through a DBRP mapping — InfluxQL survives a future
  move to 3 Core, Flux does not.

## Where things live

The rule: **`config.py` holds what differs between run modes** (endpoints,
credentials, this process's own behaviour). **`topology_spec.py` holds what the
broker enforces** (names, queue arguments).

The reasoning matters. A wrong hostname fails loudly by name; a wrong routing key
fails **silently** — both queues stay empty forever with no error anywhere.
`extra="ignore"` on the settings model means a typo'd `.env` key is inert, and
any name field would need a default, so a typo would fall back rather than raise.

- **`topology_spec.py` does not import `config.py`, and is not imported by it.**
  Cross-referencing docstrings serve discoverability instead. Making names
  reachable as `settings.routing_key` was rejected — it creates an obvious next
  step toward letting `.env` override them.
- Both modules expose `describe()`; the topology job logs both at startup.
- **No literals outside `config.py` and `topology_spec.py`.**

## Topology

| Thing | Value |
|---|---|
| MQTT topic | `sensors/esp32c3/telemetry` |
| Routing key (derived, not written twice) | `sensors.esp32c3.telemetry` |
| Durable-path queue | `telemetry.store` |
| Observation queue | `telemetry.observe` |
| Dead-letter exchange | `telemetry.dlx` (type `topic`) |
| Dead-letter queue | `telemetry.dlq` |
| DLQ binding key | `#` |

`ROUTING_KEY = MQTT_TOPIC.replace("/", ".")` — derived so the two cannot drift.

Queue names deliberately avoid the word "durable", since both queues are
declared durable and it would mislead. They are named after what their consumer
does, matching the module names, so a log line maps to a `list_queues` row with
no translation. The `telemetry.` prefix is **naming hygiene, not isolation** —
real isolation is a vhost, deferred because MQTT has no vhost concept and the
workarounds would land in the ESP32's credentials at stage 17.

### Queue arguments

- **`x-queue-type: classic` stated explicitly on all three**, though it is the
  4.3 default. Documents the classic-vs-quorum decision and immunises against a
  vhost- or policy-level default changing underneath.
- **`telemetry.store` baseline: `x-dead-letter-exchange` only.** No
  `x-message-ttl` (stage 8), no `x-max-length` (stage 9). Declaring them early
  with permissive values was considered and rejected — 406 fires on *any*
  argument difference, so it would muddy stages 8 and 9 for no gain. The
  destructive redeclare is unavoidable.
- **`telemetry.observe`: `x-max-length: 100`, `x-overflow: drop-head`, both
  explicit.** 100 because at 1 Hz it fills in ~100 s — long enough to watch the
  cap arrive, short enough to eyeball which `seq` values survived. `drop-head`
  stated explicitly because plain `reject-publish` discards **without**
  dead-lettering.
- **`telemetry.observe` is `durable=True` despite being lossy.** Unrelated
  properties. A non-durable queue would vanish on broker restart, taking its
  binding with it, and its consumer would then sit receiving nothing forever with
  no error — the most confusing failure this project could produce.
- **DLQ has no DLX of its own** (a DLQ that dead-letters is a loop) and is
  **unbounded** (its contents are the evidence).

### Dead-letter exchange

- **Topic with a `#` binding, not fanout.** Fanout is more forgiving, but dead
  letters keep their original routing key, so a topic DLX lets a second DLQ
  later be bound to a narrower pattern as a routing exercise. `#` matches any
  key; `*` would silently stop matching multi-word keys.
- **`x-dead-letter-routing-key` deliberately unset** — setting it rewrites the
  key on republish and costs the ability to see where a message came from.
- **Declared before anything references it.** Dead-lettering follows normal
  routing rules, so an unbound DLX is a hole in the floor.

### Destructive redeclare

- **CLI flag `--recreate`**, not an environment variable. Appears in shell
  history, cannot be left switched on, unreachable from a normal `compose up`.
  An env var in `.env` would make every subsequent `up` destructive and, given
  `extra="ignore"`, would not even be validated.
- **Scope is `telemetry.store` and `telemetry.observe` only.** The DLQ survives,
  because a routine "change the TTL" must not discard collected `x-death`
  headers. Emptying it is a separate operation (`purge_queue`).
- **No `if_empty` / `if_unused` guards** — they would refuse exactly when needed,
  since stages 8 and 9 recreate queues that have been allowed to fill.

## The payload contract (frozen at stage 5)

This is the **single interface between the software and hardware halves**. Stage
17's firmware must satisfy it exactly.

```json
{
  "seq": 1234,
  "device": "sim-01",
  "temp_c": 24.4,
  "humidity_pct": 62.5,
  "ts_ms": 1756400000123
}
```

- **`seq`** — integer, monotonic, restarts at 1 each process run. Deliberately
  **not** globally unique across restarts.
- **`device`** — distinguishes simulator from ESP32; becomes an InfluxDB tag at
  stage 11.
- **`temp_c` / `humidity_pct`** — floats, units in the field name, matching
  `dht_read_float_data()` so stage 17 needs no type conversion.
- **`ts_ms`** — epoch **milliseconds**, publisher-stamped. Milliseconds
  specifically because InfluxDB point identity is measurement + tag set +
  timestamp, so points sharing a timestamp overwrite rather than accumulate; at
  second resolution a stage 9 burst would silently collapse.
- Publisher-stamped rather than consumer-stamped because arrival-time stamping
  smears the timeline during exactly the stage 8, 11 and 18 demonstrations those
  stages exist to produce. The cost — the ESP32 needing SNTP — is accepted and
  lands at stage 16.
- MQTT 5 **Content Type property set to `application/json`**. Whether the plugin
  maps it through to AMQP `content_type` is **unverified**; nothing depends on it.

**Note for stage 17:** a real DHT11 through `esp-idf-lib/dht` yields whole
numbers only (`24.0`, never `24.4`) — the driver discards the fractional byte for
that sensor type. The example value above is reachable by the simulator, not by
the hardware. See `firmware/docs/dht-api.md`.

## Publisher (stage 5, implemented)

- **`paho-mqtt` >= 2.0**, not `pika`. The publisher must speak MQTT because that
  is what the MCU speaks at stage 17. `pika` remains correct for the consumers,
  which speak AMQP.
- **`CallbackAPIVersion.VERSION2` passed explicitly.** V1 is deprecated and has
  different callback signatures; most tutorial code online is V1.
- **MQTT protocol version 5.0**, written in source, **not configurable.** A
  config field for it was considered and rejected as an unnecessary axis. MQTT 5
  was chosen because its PUBACK carries a reason code, which is what makes an
  unroutable publish observable.
- **Reason-code handling in `on_publish`:** any non-zero code logs at ERROR with
  the affected `seq`.
- **Paho's built-in reconnect**, not a hand-rolled loop — a deliberate divergence
  from `topology.py`, which hand-rolls because it is one-shot and must exit with
  a meaningful code. Backoff is set explicitly.
- **Client ID set explicitly**, never paho-generated. Source carries a comment
  that stage 17's firmware must use a **different** ID or the broker disconnects
  one of the two.
- **`DriftSimulator`** — bounded random walk, ±1 unit per tick, over the DHT11's
  real range (0–50 °C, 20–90 % RH). Slow drift rather than noise, and ±1 matches
  the sensor's actual resolution rather than implying false precision.

### Controls: rate is config, experiments are CLI

- **`publish_interval_seconds`** (default 1.0) is a **config field** — it is
  steady-state process behaviour, and compose needs it without arguments.
- **Corruption and burst are CLI flags**, following the `--recreate` precedent:
  hand-run experiments, visible in shell history, impossible to leave on.
  - **`--corrupt-every N`** — **truncates the JSON string**, so it genuinely
    fails `json.loads`. Not "valid JSON with wrong fields" — stage 7's
    poison-message case is a parse failure.
  - **`--burst N`** — publishes N fast, then calls `wait_for_publish()` on
    **every** message before disconnecting.

### The burst-mode trap (resurfaces at stage 9)

At QoS 1, `publish()` returns before the broker has acknowledged anything. A
burst that publishes N messages and exits immediately disconnects with PUBACKs
still in flight, and those messages may never be delivered. **Retrying is the
wrong fix** — it is unavailable mid-connection and would create duplicates. The
implemented fix is `wait_for_publish()` on every message before disconnecting.

This matters at stage 9 because a premature disconnect and an overflow drop
produce the *same* symptom: a hole in the `seq` sequence.

## Logging, errors, testing

- **Shared `logging_setup.py`**, console only. No file handlers, no
  structured/JSON logging — actively worse to read by eye on a single-machine
  project.
- **`pika`'s logger pinned to `WARNING`.** This is the concrete reason the module
  is shared: it logs a line per AMQP frame at DEBUG, and the pin would otherwise
  be duplicated across four modules and drift.
- **`log_level` is a `Literal` in `config.py`**, so a typo fails at startup
  naming the field.
- **`seq=<int>` as a bare token** in every log line about a message, across the
  publisher and both consumers, so `grep -o 'seq=[0-9]*'` is the stage 6 and
  stage 9 comparison tool.
- **`rabbitmq_host` has no default.** A forgotten compose override must fail
  loudly by name rather than falling back to localhost.
- **Hand-rolled retry loop in `topology.py`, not pika's `connection_attempts`**,
  for clear per-attempt logging and easier testing. `connection_attempts=1` is
  set explicitly so a future edit cannot nest pika's retries inside ours and
  multiply attempts invisibly.
- **Auth failures abort immediately**, before the broad connection handler.
  `ProbableAuthenticationError` and `ProbableAccessDeniedError` subclass
  `AMQPConnectionError`, so the ordering of `except` blocks is load-bearing.
- **406 is caught, translated, and quotes RabbitMQ's `reply_text` verbatim.**
  Quoting verbatim was a condition of the decision — the raw broker wording is
  worth learning to recognise; the wrapper adds the queue name and the
  `--recreate` remedy.
- **Exit codes:** `0` clean/declared, `2` broker unreachable, `3` auth or
  permissions rejected, `4` argument mismatch or other channel error. SIGTERM and
  SIGINT are handled for a clean disconnect, since compose sends SIGTERM.
- **Tests must pass with no broker running.** 19/19 at stage 5. `declare(channel)`
  takes a channel rather than opening its own connection — that is the lever that
  makes the topology assertable against a mock, and the arguments dicts *are* the
  policy, so they are the thing worth asserting. Each test corresponds to a
  recorded decision; a failure means either the code drifted or the decision
  changed.

## Orchestration

- **uv** for dependencies; **pydantic-settings** for configuration.
- **A single `.env`** holds host-mode values, with per-service overrides in
  compose. Compose consumes it twice: `env_file:` for the Python services, and
  `${VAR}` interpolation for the broker's credentials.
- **`extra="ignore"`** on the settings model, so `.env` may carry keys for stages
  the config class has not grown fields for yet.
- **Fully explicit compose services — no YAML anchors, no `extends:`.**
  `extends:` was rejected because its handling of `depends_on` has regressed
  between Compose minor versions, and stage 4's
  `service_completed_successfully` gate is load-bearing.
- **Project name pinned** (`name: thermo-warren`) so a rename cannot orphan
  volumes.
- **`rabbitmq:4.3-management`**, floating across patch releases. 4.2's community
  support ended 31 Jul 2026; 4.3 runs to 30 Nov 2026.
- **Broker user via `RABBITMQ_DEFAULT_USER`/`PASS`**, not `definitions.json` —
  declarative definitions would collide with the topology-declared-by-code goal.
- **Ports bound to 127.0.0.1 only.** 1883 widens to all interfaces at stage 14+,
  when the ESP32 needs it, and deliberately not before.
- **`anonymous_login_user = none`** in `rabbitmq.conf`. `mqtt.allow_anonymous`
  was deliberately omitted — an unsupported key aborts boot, and the docs are
  ambiguous about whether it survives in 4.3.
- **Single image for all four Python services**, with `./src` bind-mounted so
  edits need no rebuild.
- **Healthcheck: `rabbitmq-diagnostics check_port_connectivity`**, preferred over
  `ping` because the MQTT listener comes up after the node. It only opens a
  socket — liveness, not correctness.
- `topology` stays one-shot permanently. `publisher` is long-lived
  (`restart: unless-stopped`) from stage 5.

## Verified broker facts — do not re-search

**MQTT plugin**
- Publishes into the pre-existing `amq.topic` exchange; the MQTT topic becomes
  the AMQP routing key. No custom exchange needed.
- QoS 2 is not supported — publishes and subscribes at QoS 2 are silently
  downgraded to QoS 1.
- QoS 1 publishes become **persistent** messages internally (`delivery_mode: 2`,
  confirmed in the Management UI at stage 5 — that was the stage 5 DoD); QoS 0
  become transient. No server-side configuration needed.
- **A topic exchange with no matching binding discards the message silently** —
  no error, no failed PUBACK, nothing in the logs. This is why topology
  declaration must precede publishing.

**PUBACK and confirms**
- **A QoS 1 PUBACK is already a publisher confirm.** RabbitMQ withholds it until
  every destination queue has confirmed receipt. No opt-in needed.
- MQTT 5 PUBACK reason codes from RabbitMQ: `0` success, `16` no matching
  subscribers (could not route to any queue), `131` implementation specific error
  (e.g. a target classic queue unavailable).
- **MQTT 3.1 / 3.1.1 have no error channel at all** — other than closing the
  connection, the server cannot communicate a publishing error.

**paho-mqtt**
- V2 `on_publish` signature: `(client, userdata, mid, reason_code, properties)`.
- **Trap:** at QoS 0 paho *fabricates* the reason code — always success, always
  empty properties — because MQTT has no reason code on a PUBLISH. A success code
  at QoS 0 proves nothing.
- Paho deliberately does **not** retry QoS > 0 messages within a connection,
  because the spec forbids it.

**MQTT retransmission**
- Retransmission is **reconnect-scoped only**: a client reconnecting with a
  session present must resend unacknowledged PUBLISH packets with their original
  packet identifiers. That is the only circumstance where redelivery is required.
- Redelivered packets carry the **DUP flag**, but a receiver seeing DUP cannot
  assume it has actually seen an earlier copy.
- Session state needs `clean_start=False` **and** a non-zero session expiry
  interval; the MQTT 5 default expiry is 0, so the session evaporates on
  disconnect and queued messages are lost.
- Consequence: absence of a PUBACK means **unknown**, not **not delivered** — the
  acknowledgement itself may have been lost. Republishing manufactures
  duplicates. This is at-least-once, and it is why `seq` exists in the payload.

**Dead-lettering**
- Death reasons: `rejected` (nack/reject with requeue false), `expired` (TTL),
  `maxlen` (length limit). Recorded in the `x-death` header with the originating
  queue and a count.
- Overflow behaviours: `drop-head` (default) dead-letters the **oldest**;
  `reject-publish-dlx` dead-letters the **newest**; plain `reject-publish`
  **discards silently without dead-lettering**. That last is a trap and is why
  the default is stated explicitly.
- `reject-publish-dlx` is **classic-queue only**.
- **Per-message TTL only takes effect once a message reaches the head of the
  queue**, so an expired message behind a long-lived one lingers past its
  deadline. Queue-level TTL does not behave this way.
- **Redeclaring a queue with different arguments raises 406 rather than updating
  it.** Since stages 8 and 9 change queue arguments, destructive redeclare had to
  be supported from the start.

## Correction to the staged plan

Stage 5's definition of done in `mcu-rabbitmq-staged-plan.md` says both queue
depths climb **in lockstep**. **That is wrong.** `telemetry.observe` carries
`x-max-length: 100` with `drop-head`, so it **plateaus at 100** while
`telemetry.store` grows unbounded. A burst does not add the same count to both.

This is stage 9's lesson arriving early, not a bug. Treat the plan's wording as
superseded; the README explains the cap.

## Environment facts learned the hard way

- **`docker compose up -d <service>` does not rebuild after a `pyproject.toml`
  change.** It silently reused the stage 4 image, which had no `paho-mqtt`. Run
  `docker compose build` explicitly. **Expect this again at stage 6 when `pika`
  is added to the consumers' image.**
- **Bind-mounted broker config must be mode 644.** The container runs as uid 999;
  a 600 file produces `eacces` and a boot failure whose log misleadingly asks
  about Cuttlefish format. An unreadable `enabled_plugins` fails *silently*.
- **The `enabled_plugins` file is Erlang term syntax and requires a trailing
  period.** Without it the file is ignored with no error.
- **`.env` is read by Compose, not by the shell.** `$RABBITMQ_PASSWORD` in an
  interactive command expands to empty. Pass `--env-file .env` to `docker run`
  and let the container's shell expand it. Never `source .env` into the working
  shell — process environment outranks `.env` in settings resolution.
- **Host-mode Python must run from the repo root** — `.env` resolves relative to
  CWD.
- **Cross-check library APIs against the installed source**, not memory or
  tutorials. Done at stage 5 for `ReasonCode.is_failure`,
  `Properties.ContentType` and `wait_for_publish` timeout semantics. **Worth
  repeating for `pika` at stage 6.**

## Check first at stage 6

**Were `telemetry.store` and `telemetry.observe` purged after stage 5
verification?** Not confirmed.

If not, `telemetry.store` still holds stage 5 traffic **including deliberately
malformed messages** from the `--corrupt-every` runs. The new
`consumer_store.py` will hit those on its first run — before stage 7 has built
any reject-and-dead-letter path — and it will look like a consumer bug. **Purge
both queues, or know they are there.**

## Open questions

**Stage 6, raised and not yet answered:**

- **`consumer_observe`'s acknowledgment mode.** Manual or automatic. The
  non-obvious coupling: **prefetch (`basic_qos`) only applies to channels using
  manual acknowledgment** — with `auto_ack=True` the broker considers a message
  acknowledged the moment it writes it to the socket, so there is no
  unacknowledged window to bound and no flow control at all. Choosing auto-ack is
  therefore also choosing "this consumer has no backpressure, permanently", which
  is arguably the point of a lossy path but should be chosen rather than
  inherited. Also undecided: prefetch value for `consumer_store`, consumer
  lifecycle (long-lived vs drain-and-exit), where an ack-delay test control
  lives, malformed-JSON handling per path, and reconnect strategy.

**Carried forward from earlier stages:**

- **Publisher `clean_start` and session expiry interval** — whether the publisher
  uses a clean session, and whether persistent-session behaviour is reachable, is
  undecided. Harmless for stage 6. It is the mechanism behind the stage 18
  duplicate demonstration, so **do not let it disappear.**
- **Stage 11's storage-failure policy** — retry with backoff vs dead-letter after
  N attempts. Left to the user during implementation.
- **InfluxDB and Grafana image tags** — unverified. RabbitMQ's is settled at
  `rabbitmq:4.3-management`.
- **Grafana healthcheck** — unknown whether `curl` or `wget` exists in that image.
- **`.env.example` contents** — whether it gained any stage 5 keys was not
  reported. Check it matches `config.py` before stage 6 adds more.
- **Optional hardening, not scheduled** — `iot` carries the `administrator` tag.
  Splitting a human admin from a scoped application user was raised and deferred.
