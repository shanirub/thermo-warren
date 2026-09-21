# src/telemetry/ — the broker half

Python side of the pipeline: topology declaration, the software publisher, and
the two consumers. Speaks AMQP via `pika`, except the publisher, which speaks
MQTT via `paho-mqtt`.

Read the repo-root `CLAUDE.md` too — the working-style rules there apply here,
including the obligation to update this file at the end of every stage.

**Software track state: stage 6 done and verified.** Next is stage 7.

Both consumers are real. `stub.py` is **deleted** — its last callers are gone,
exactly as planned. Shared AMQP plumbing now lives in `amqp.py`.

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

**`amqp.py` (stage 6) holds how to reach the broker and how to stay attached to
a queue.** It knows no queue names and no arguments — that stays in
`topology_spec.py`. `connect()` was written for `topology.py` at stage 4 and
both consumers need it unchanged, along with the auth-ordering fix and the
pika-noise suppression around it; three copies would drift, which is the same
argument that made `logging_setup.py` shared. `topology.py` imports `EXIT_OK`,
`EXIT_UNREACHABLE`, `EXIT_REJECTED` from it and keeps only `EXIT_MISMATCH = 4`,
which is meaningless for a consumer.

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
  lands at stage 17, with the MQTT client that needs the timestamp. Stage 16's
  DoD is link state only and never mentions time; an earlier note placed SNTP
  at 16, which was a guess at placement rather than a DoD commitment.
- MQTT 5 **Content Type property set to `application/json`**. **Verified at
  stage 17: the plugin does map it through** to AMQP `content_type` — observed on
  a queued message from the MCU in the management API. Still nothing depends on
  it.

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

## Consumers (stage 6, implemented and verified)

Both are real; `stub.py` is gone. Shared machinery is in `amqp.py`:
`connect()`, and `run_consumer(queue, on_message, *, auto_ack, prefetch_count)`
which owns the signal handling, the reconnect loop, `basic_qos`,
`add_on_cancel_callback`, `basic_consume` and `start_consuming`.

### The asymmetry is expressed twice, deliberately

- **`consumer_observe`: `auto_ack=True`, and `basic_qos` is never called.**
- **`consumer_store`: manual ack, `prefetch_count=10`, `global_qos=False`.**

The plan's stage 6 wording attaches manual ack and bounded prefetch specifically
to the durable consumer; the observation one "logs". Making the path lossy in
the consumer *as well as* in the queue means one unclean kill produces two
different outcomes on the same messages — **verified**: store's 10
unacknowledged returned to ready, observe's in-flight deliveries were gone.

**`prefetch_count=None` means "do not call `basic_qos` at all"**, not zero.
`basic_qos` is **ignored on an auto-ack channel** — the broker considers a
message acknowledged the moment it writes it to the socket, so there is no
unacknowledged window to bound. Passing it would imply a limit that does not
exist. **Verified**: `telemetry.observe` sat at `unacked=0` throughout the
ack-delay test while `telemetry.store` pinned at 10.

Cost of auto-ack, accepted: no backpressure, permanently. A consumer slower than
the publisher accumulates in pika's frame buffer rather than in queue depth.
Irrelevant at 1 Hz; it changes where you look when something misbehaves.

### prefetch_count = 10

**10 rather than 1 because a cap of 1 is unobservable** — "unacked pinned at 1"
looks identical whether `basic_qos` was called or not, so the DoD check would
pass without proving anything. 10 is reachable in ~20 s with `--ack-delay 2`
against the 1 Hz publisher and needs no manufactured backlog; 100 would have
needed `--burst 200` first.

**The number has a second meaning that bites at stage 11:** prefetch is the
at-least-once duplicate window, because an unclean crash redelivers every
unacknowledged message. 10 is how many duplicates a crash manufactures.

Lives as `consumer_prefetch_count` in `config.py`, following
`publish_interval_seconds` — steady-state behaviour compose needs with no
arguments.

### Malformed payloads: acked, not rejected — until stage 7

`consumer_store` logs at WARNING with the delivery tag, then **acks**. Stage 7
owns reject-and-dead-letter, and doing it here would collapse two stages.

The deciding argument was stage 7's diff: with the branch already in place and
already logging, **stage 7 is one line** — `basic_ack` →
`basic_nack(requeue=False)`. `tests/test_consumers.py` carries a test asserting
the current behaviour that is meant to be **deliberately inverted** at stage 7.

Acked rather than left unacknowledged because an unacknowledged poison message
holds a prefetch slot forever and 10 of them wedge the consumer permanently.

`consumer_observe` merely logs the same bytes — that queue has no DLX to send
them to, which is the other half of stage 7's contrast.

### basic_consume + start_consuming, not the consume() generator

**Under the generator a broker-initiated cancel is silent.**
`blocking_connection.py:2085-2091`: a `_ConsumerCancellationEvt` sets
`_queue_consumer_generator = None` and `break`s — no exception, no callback, the
loop just ends and the process exits looking cleanly shut down. In the callback
path (`:1592-1596`) pika does `del self._consumer_infos[tag]` and *then* fires
`add_on_cancel_callback`, so there is a frame to log.

This is not hypothetical: **`--recreate` deletes the queues these consumers are
attached to**, and stages 8 and 9 both run it. **Verified** — running
`--recreate` with both attached logged, on each:

```
ERROR telemetry.amqp broker cancelled our consumer on telemetry.store
      (queue deleted?): <Basic.Cancel([...])>
```

and both reattached 2 s later.

**Consequence for the loop:** `start_consuming()` **returns normally** on a
broker cancel rather than raising, so `_Session.cancelled_by_broker` is what
distinguishes it from a requested stop. Without that flag the two are
indistinguishable.

### Shutdown

SIGTERM/SIGINT set a stop flag and then call
`connection.add_callback_threadsafe(channel.stop_consuming)` — **not
`stop_consuming()` directly**, which issues a Basic.Cancel and flushes output,
re-entering pika's socket I/O from a handler that may have interrupted it
mid-operation. `add_callback_threadsafe` only enqueues.

Per `stop_consuming()`'s docstring, a clean stop **rejects pending ackable
messages** (back to ready) and **loses pending non-ackable ones** — the same
asymmetry as an unclean kill, for the same reason.

### Reconnect

**pika's `BlockingConnection` has no automatic reconnect**;
`connection_attempts` and `retry_delay` govern only the *initial* connect
(`connection.py:233-254`). So the loop is ours, and it is a deliberate
divergence from `publisher.py`, which gets reconnect free from paho.

- **The first connect stays bounded and exits 2 on failure**, so a typo'd
  hostname fails loudly naming the endpoint instead of looping forever.
- **Every later loss reconnects without limit**, backing off from
  `rabbitmq_connect_retry_delay` to `RECONNECT_MAX_DELAY_SECONDS = 30.0`. The
  30 s mirrors the firmware's Wi-Fi backoff cap, so both halves of the project
  take the same worst case to notice a network returned.
- **Unbounded because of host mode.** Under compose `restart: unless-stopped`
  would catch an exiting consumer; run directly against exposed ports there is
  nothing to catch it, and the project requires that mode.
- **The except ordering is load-bearing, exactly as in `connect()`.**
  `StreamLostError`, `ConnectionClosedByBroker`, `ConnectionClosedByClient`,
  `ProbableAuthenticationError` and `ProbableAccessDeniedError` **all** subclass
  `AMQPConnectionError`. A broad handler placed first would retry forever
  against a wrong password and would also "recover" from our own clean shutdown
  — hence the `if stopping: break` guard inside it.
- `ChannelClosedByBroker` (404 on `basic_consume`, queue gone) is caught and
  retried rather than fatal, because `--recreate` produces exactly that window.

**Verified** on a `docker compose restart rabbitmq`: both consumers logged
`CONNECTION_FORCED (320)`, backed off 2 s, failed one attempt, and resumed on
the second — about 4 s end to end, no operator action.

Note pika logs its own `ERROR Unexpected connection close detected` alongside
ours. `_quiet_pika_connect_errors()` is scoped to connect attempts only and does
not suppress it. Left visible: it names the AMQP reply code, which ours does not.

### The ack-delay flag

**`--ack-delay SECONDS` on `consumer_store` only**, default off, following the
`--recreate` / `--corrupt-every` / `--burst` precedent: a hand-run experiment,
visible in shell history, impossible to leave switched on, unreachable from a
normal `docker compose up`. `consumer_observe` has no ack to delay.

**It is a blocking `time.sleep()` inside the pika callback, which stalls the
I/O loop and stops heartbeats.** At 2 s, harmless. Past the negotiated heartbeat
interval it reproduces an unexplained broker disconnect — which is precisely the
hazard stage 11 has to design the storage write around. The flag can demonstrate
it on demand.

### Where stage 11 goes

`consumer_store.build_handler()` marks the insertion point in a comment: after
the parse, **before** the ack. Acknowledging first loses the message on a crash
between the two; acknowledging after redelivers it. That ordering is what makes
the pipeline at-least-once end to end.

### Stage 6 DoD — verified against real behaviour

| Check | Result |
|---|---|
| Both queues drain with the publisher running | Both at 0 ready, 1 consumer each |
| Both consumers report the same `seq` | Identical sets over the interior of both publisher streams |
| Unacked pins at the prefetch limit | `telemetry.store` held at exactly **10** while ready grew; `telemetry.observe` at **0** throughout |
| Unclean kill returns messages to ready | `SIGKILL` → unacked 10 → 0, ready rose by the 10 returned. Not lost |
| Stop only `consumer_observe` | `telemetry.observe` grew 31 → 62 → 92 → **100** and held at its cap; `telemetry.store` stayed at 0, still draining |

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
- **Tests must pass with no broker running.** 33/33 at stage 6. `declare(channel)`
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
- **AMQP and management bound to 127.0.0.1; MQTT is not.** 1883 publishes on
  `0.0.0.0` from stage 17, because the ESP32 reaches it over the LAN and no
  narrower binding works. This exposes the `iot` user, which still carries the
  `administrator` tag — the answer to that is the scoped application user in
  "Open questions", not a bind address.
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

### Running the stack

```
docker compose build          # only after a pyproject.toml change -- see below
docker compose up -d          # start everything, detached
docker compose logs -f rabbitmq   # follow one service; Ctrl-C detaches the reader only
```

Stopping: **`docker compose stop`** keeps the containers, **`down`** removes them
but keeps the named volume, **`down -v`** destroys `rabbitmq-data`. Keep `-v` for
when it is meant — destroying that volume is the only way
`RABBITMQ_DEFAULT_USER`/`PASS` get re-applied, since they apply solely on first
boot against an empty data directory.

**Expected steady state at stage 6:** `rabbitmq` Up (healthy), and
`publisher`, `consumer-observe` and `consumer-store` all Up. **Only `topology`
is `Exited (0)`** — the one-shot declarer having done its job, which is what the
other three gate on. Both queues sit near `ready=0` with `consumers=1` each.
Management UI at `http://localhost:15672`.

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

**AMQP consumers** (verified at stage 6)

- **RabbitMQ sends `Basic.Cancel` to a consumer whose queue is deleted.**
  Observed directly by running `topology.py --recreate` with both consumers
  attached. The channel stays open; only the consumer goes away. This is a
  RabbitMQ extension, not core AMQP 0-9-1.
- **`basic_qos` is ignored on a channel using automatic acknowledgment.** Not a
  pika quirk — there is no unacknowledged window to bound, because the broker
  considers the message done when it writes it to the socket. Confirmed by
  `telemetry.observe` sitting at `unacked=0` while `telemetry.store` pinned at
  its prefetch limit.
- **pika's `BlockingConnection` has no automatic reconnect.**
  `connection_attempts` and `retry_delay` apply to the *initial* connect only
  (`connection.py:233-254`). A mid-session loss raises out of
  `start_consuming()`.
- **`start_consuming()` returns normally on a broker cancel**, rather than
  raising — pika deletes the consumer from `_consumer_infos`
  (`blocking_connection.py:1592-1596`) and the loop's `while self._consumer_infos`
  condition simply ends. A flag set by the on-cancel callback is the only way to
  tell it apart from a requested stop.
- **`StreamLostError`, `ConnectionClosed`, `ConnectionClosedByBroker`,
  `ConnectionClosedByClient`, `ProbableAuthenticationError` and
  `ProbableAccessDeniedError` all subclass `AMQPConnectionError`.** Except
  ordering around a reconnect loop is load-bearing: a broad handler first would
  retry forever against a wrong password and would also "recover" from a clean
  shutdown.

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

Stage 6's DoD says both consumers "report the same sequence numbers". True, but
**only over the interior of a run, and only while `consumer_observe` keeps up**.
Two things make a naive `diff` of the two `seq` sets show spurious differences:

- **The ends never match.** The two consumers attach at different instants, so
  the first and last few messages legitimately differ. Compare interiors.
- **If `telemetry.observe` ever falls behind past its 100-message cap it drops
  its head**, and those `seq` are genuinely absent from that path forever. That
  is the design working, not a fan-out failure.

Neither is a defect in the plan's intent, but a literal reading of "the same
sequence numbers" will send you hunting for a bug that is not there. It did
once, at stage 6, before the non-atomic `purge_queue` behaviour was understood.

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
  `Properties.ContentType` and `wait_for_publish` timeout semantics, and at
  stage 6 for `basic_consume`, `basic_qos`, `start_consuming`, `consume()` and
  `add_callback_threadsafe` — which is how the generator's silent broker-cancel
  was found before it was written into the design rather than after.
- **`configure_logging()` detaches pytest's `caplog` handler.** It calls
  `logging.basicConfig(force=True)`, which removes *every* existing root
  handler. A test that calls a `main()` then asserts on `caplog.text` sees an
  empty capture while the lines themselves appear on stderr — which reads as
  "the log line is missing" rather than "the handler is gone".
  `tests/test_consumers.py` monkeypatches it to a no-op.
- **`rabbitmqctl purge_queue` is per queue and not atomic across queues.**
  Purging `telemetry.store` and then `telemetry.observe` leaves any message
  published in between alive in the first and gone from the second — which looks
  exactly like a fan-out failure when you diff the two consumers' `seq`. Cost a
  real detour at stage 6. Compare interior ranges, or stop the publishers first.
- **`docker compose up` without `-d` ties the stack's lifetime to the terminal.**
  Ctrl-C sends SIGTERM to every service and stops the lot — a graceful shutdown,
  not a kill, so nothing is corrupted, but the stack is then down. `restart:
  unless-stopped` does **not** bring it back, by design: that policy ignores an
  explicit operator stop. To read logs without owning the lifecycle, use
  `docker compose logs -f`.
- **`docker compose up -d --wait` works from stage 6 and is now the right
  readiness gate.** **Verified**: exit 0 in 7.5s, blocking until the broker is
  healthy and both consumers are actually consuming. At stage 5 it exited **1**
  with `container thermo-warren-consumer-observe-1 exited (0)`, because `--wait`
  treats any service leaving the running set as a failed wait and the stage 2
  stubs exited immediately; making both consumers long-lived removed the cause.
  `topology` still exits without tripping it — now observed twice, though the
  explanation (its `service_completed_successfully` gate) remains **inferred,
  not verified**.

## Check first at stage 7

**Stage 6 confirmed the stage 6 prediction exactly**: at stage 6 start
`telemetry.observe` held **100** (its cap) and `telemetry.store` held **66,702**
— the MCU's well-formed `device=esp32c3-01` traffic accumulated since stage 17,
with the unbounded queue showing what "unbounded" means. Both were purged.

**Two publishers are live whenever the MCU is powered**: the simulator
(`device=sim-01`, `seq` from 1) and the MCU (`device=esp32c3-01`, `seq` in the
tens of thousands). Both use the same routing key, so both queues carry an
interleaved stream and `grep -o 'seq=[0-9]*'` returns two unrelated series.
Split them by magnitude, or stop one. This is stage 18's "run both publishers at
once" row happening incidentally, and it is worth knowing before it looks like a
bug.

**`telemetry.dlq` is empty and stage 7 is the first thing that will put anything
in it.** No stage 5 malformed traffic survives to confuse the first run.

## Open questions

**All six stage 6 questions are answered** — ack mode, prefetch value, ack-delay
control, malformed-JSON handling, loop shape and reconnect strategy. See
"Consumers (stage 6)" above; none of them is open.

**Raised at stage 6, deliberately not acted on:**

- **`consumer_store` has no dead-letter behaviour yet** — that is stage 7, and
  the test asserting malformed payloads are *acked* is meant to be inverted
  there.
- **Heartbeat interval is left at pika's negotiated default.** `--ack-delay`
  past that interval reproduces a heartbeat timeout; nothing sets it explicitly,
  which is a gap against the project's explicit-over-inherited rule. Recorded,
  not fixed — stage 11 is where a blocking call in the callback stops being
  hypothetical.
- **`run_consumer()` takes no "drain and exit" mode.** Not needed by any stage
  so far; noted because stage 18 might want one.

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
- **Optional hardening, not scheduled** — `iot` carries the `administrator` tag.
  Splitting a human admin from a scoped application user was raised and deferred.
