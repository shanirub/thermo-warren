# src/telemetry/ — the broker half

Python side of the pipeline: topology declaration, the software publisher, and
the two consumers. Speaks AMQP via `pika`, except the publisher, which speaks
MQTT via `paho-mqtt`.

Read the repo-root `CLAUDE.md` too — the working-style rules there apply here,
including the obligation to update this file at the end of every stage.

**Software track state: stage 13 done and verified. The software track is
finished.** Stages 1-13 are all closed; the next stage on any track is 18,
end-to-end resilience, and it needs both halves at once.

Both consumers are real, and `consumer_store` dead-letters anything that fails
the payload contract. **All three dead-letter triggers are now demonstrated** —
rejection, expiry and overflow — which completes the arc the plan set out at
stage 4. `telemetry.store` is back to its baseline of a dead-letter exchange and
nothing else; stages 8 and 9 each added an argument, measured it, and removed it
again. Shared AMQP plumbing lives in `amqp.py`; the payload contract lives in
`payload.py`.

**`consumer_store` now writes to InfluxDB between the parse and the ack**, and
the pipeline is at-least-once end to end. Storage plumbing lives in
`storage.py`.

**Grafana reads it back from stage 12**, with its datasource provisioned from
`grafana/provisioning/` and the InfluxQL database declared by a one-shot `dbrp`
service. **Stage 13 added the dashboard** from the same directory: two panels,
one series per device, 15 minutes at a 5 s refresh. **No Python was involved in
either stage**, and neither needed a rebuild or a `compose.yaml` change.

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
- **`influxdb:2.9`** (stage 10), not 1.8 and not 3 Core. Floating across patch
  releases exactly like `rabbitmq:4.3-management`. **InfluxQL, not Flux**,
  through a DBRP mapping — InfluxQL survives a future move to 3 Core, Flux does
  not. The explicit mapping is stage 12's; stage 10's hand checks were written
  in Flux for that reason. **That reason turned out not to hold** — 2.x
  synthesises a virtual mapping, so InfluxQL would have worked at stage 10 too.
  The Flux checks are staging, not a reversal, but they were not forced.

## Where things live

The rule: **`config.py` holds what differs between run modes** (endpoints,
credentials, this process's own behaviour). **`topology_spec.py` holds what the
broker enforces** (names, queue arguments).

**`storage.py` (stage 11) is the fourth home, and it holds almost nothing.** The
measurement name, which payload fields are tags rather than fields, and the
write precision — three constants that are none of "differs between run modes",
"the broker enforces it" or "publisher and consumers must agree on it". Every
*key name* is imported from `payload.py` rather than re-spelled, which is the
module's one rule: a renamed field would otherwise still write, just into a
differently-named column, with no error anywhere. The endpoint, org, bucket and
token do differ between run modes and so live in `config.py` as usual.

The reasoning matters. A wrong hostname fails loudly by name; a wrong routing key
fails **silently** — both queues stay empty forever with no error anywhere.
`extra="ignore"` on the settings model means a typo'd `.env` key is inert, and
any name field would need a default, so a typo would fall back rather than raise.

**`payload.py` (stage 7) holds the frozen payload contract** — field names,
types, `build_payload()` and the validating `parse()`. It is a third category
the other two do not cover, and the broker is the reason: **it never inspects
the message body.** It routes on the key alone, so nothing in the broker will
ever reject a payload for being the wrong shape, which makes this file the only
enforcement point there is. It cannot live in `topology_spec.py` without
breaking that module's one rule ("if the broker enforces it, it belongs here").

It imports `config.py`, unlike `topology_spec.py`, for one reason stated in the
source: `device` is the single contract field whose *value* legitimately differs
between the simulator and the MCU, which is exactly what `config.py` is for. The
field *names* are not configurable and never will be.

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
- **No literals outside `config.py`, `topology_spec.py` and `payload.py`.**
  Three files, three categories, and the split is the point: an endpoint, a
  queue argument and a field name fail in three different ways. `payload.py`
  joined the list at stage 7.

**The four non-Python files from stages 12 and 13 do not join that list.** The
database name is not a literal in the stage 12 pair — `influxdb/dbrp.sh` and the
datasource file both take it from `${INFLUXDB_BUCKET}`, so bucket and database
cannot drift apart. What *is* literal there is the retention policy `autogen` and
the datasource uid `influxdb-telemetry`, and both are deliberate: `autogen`
matches the name InfluxDB gives the virtual mapping it replaces, and the uid is
pinned because stage 13's dashboard references it.

**Stage 13's dashboard restates three things Python already owns**, and they are
the known cost of a dashboard being data rather than code:

- the datasource uid, four times — once on each panel and once on each target;
- the measurement `readings` and the field names `temp_c` / `humidity_pct`,
  which `storage.py` owns and derives from `payload.py`;
- the `device` tag name, same source.

**`$VAR` substitution does not reach a dashboard JSON** — verified, not assumed:
a panel titled `Humidity $INFLUXDB_BUCKET` was read back from the API with the
`$INFLUXDB_BUCKET` still literal, although the same variable is substituted in
the datasource YAML two directories away. So this duplication has no mechanism
available to remove it. **The failure mode is quiet**: rename a field in
`payload.py` and the pipeline keeps working while a panel goes empty, with no
error anywhere. `storage.describe()` in the consumer's startup log is what a
panel should be checked against.

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
- **`telemetry.store`: `x-dead-letter-exchange` and nothing else.** That is the
  baseline, and stage 9 returned it there. Stages 8 and 9 each added one
  argument (`x-message-ttl`, then `x-max-length` + `x-overflow`), demonstrated
  it, and took it out — the plan calls for returning to steady-state settings
  once the lesson is recorded, and stages 10-13 want data reaching InfluxDB
  unimpeded. Both sets of constants are still defined in `topology_spec.py`, so
  either experiment is one edit away; `STORE_ARGS` carries the exact edit in a
  comment.
- **`telemetry.observe` has no TTL**, deliberately. It already sheds its head at
  the cap, and a second reason for a message to vanish there would make it
  impossible to say which one acted. The DLQ has none either — its contents are
  the evidence.
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

## The payload contract (frozen at stage 5, enforced from stage 7)

This is the **single interface between the software and hardware halves**. Stage
17's firmware must satisfy it exactly.

**It lives in `payload.py`** — names, types, `build_payload()` and the
validating `parse()`, which both consumers call. From stage 7 a payload that
fails it is dead-lettered rather than merely logged, so the contract is no
longer only a document.

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
- **`device`** — distinguishes simulator from ESP32. **It is the InfluxDB tag**
  from stage 11, and the only one, so it is what keeps `sim-01` and
  `esp32c3-01` in separate series with independent `seq` counters.
- **`temp_c` / `humidity_pct`** — **floats**, units in the field name, matching
  `dht_read_float_data()` so stage 17 needs no type conversion. Unchanged since
  stage 5; this is what a publisher is obliged to emit.

  **`parse()` is deliberately more tolerant than the contract here**, and the
  distinction matters: it accepts a JSON integer as well, so a publisher that
  emitted `29` instead of `29.0` would not be dead-lettered. That is a safety
  net against a format-string change silently destroying good readings — they
  would look perfectly correct sitting in the DLQ — and **not** a relaxation of
  what publishers must send. Both publishers still send floats.

  Both must be **finite**. Python's `json` accepts `NaN` and `Infinity`, which
  pass every `isinstance` check and fail only at the storage write.

  **Answered at stage 11, and the prediction held.** Both halves are now
  measured. `Point.field("temp_c", 29)` serializes as `temp_c=29i` — an
  *integer* field — while `float(29)` gives `temp_c=29`. And InfluxDB rejects an
  integer written to an established float field with **HTTP 422**
  (`ApiException`). So the tolerance would genuinely have reached storage.

  `storage.to_point()` coerces with `float()`, which keeps the tolerance a
  dead-lettering decision rather than a storage one, exactly as predicted.
  **Worth knowing what the coercion prevents:** `ApiException` subclasses
  `InfluxDBError` and so is caught by `storage.WRITE_FAILURES`, meaning an
  uncoerced integer would be retried three times and requeued — forever, on a
  message that can never be stored. A permanent failure dressed as a transient
  one.
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

### Malformed payloads: acked at stage 6, rejected from stage 7

**Superseded — see "Rejection and dead-lettering" below.** Kept because the
reasoning still holds and one prediction in it was wrong, which is worth
recording.

Stage 6 acked poison messages so that reject-and-dead-letter stayed stage 7's,
rather than collapsing two stages. It also predicted stage 7 would be **one
line**, `basic_ack` → `basic_nack(requeue=False)`. **Both halves of that
prediction turned out wrong**: the call became `basic_reject`, not `basic_nack`,
and validating the whole contract rather than only parse failures made the stage
considerably larger than one line. The test written to be inverted was inverted
as intended.

The part that held: a poison message must never be left unacknowledged, because
it would hold a prefetch slot forever and 10 of them wedge the consumer. A
reject settles the delivery exactly as an ack does, so the property survived the
change of call.

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
I/O loop and stops heartbeats.** At 2 s, harmless. Past the heartbeat interval
it reproduces an unexplained broker disconnect — the hazard stage 11's storage
write had to be designed around, and the flag still reproduces it on demand.
That interval is no longer merely "negotiated": `HEARTBEAT_SECONDS = 60` is
explicit from stage 11.

### Where the stage 11 write went

After the parse, **before** the ack, in `consumer_store.build_handler()`.
Acknowledging first would lose the message if the process died between the two;
acknowledging after redelivers it. See "Storage writes (stage 11)" below.

### Stage 6 DoD — verified against real behaviour

| Check | Result |
|---|---|
| Both queues drain with the publisher running | Both at 0 ready, 1 consumer each |
| Both consumers report the same `seq` | Identical sets over the interior of both publisher streams |
| Unacked pins at the prefetch limit | `telemetry.store` held at exactly **10** while ready grew; `telemetry.observe` at **0** throughout |
| Unclean kill returns messages to ready | `SIGKILL` → unacked 10 → 0, ready rose by the 10 returned. Not lost |
| Stop only `consumer_observe` | `telemetry.observe` grew 31 → 62 → 92 → **100** and held at its cap; `telemetry.store` stayed at 0, still draining |

## Rejection and dead-lettering (stage 7, implemented and verified)

`consumer_store` rejects anything that fails the contract, without requeueing,
so it routes through `telemetry.dlx` into `telemetry.dlq`. **No topology change
was needed** — the dead-letter side has been declared and inert since stage 4.

### basic_reject, not basic_nack

Both dead-letter identically and both produce `x-death` reason `rejected`, so
the DoD cannot separate them. The difference is protocol-level: **`basic.reject`
is AMQP 0-9-1 core; `basic.nack` is a RabbitMQ extension**, negotiated per
connection and exposed by pika as `channel.basic_nack_supported`
(`blocking_connection.py:974`).

The tie-breaker was the explicit-over-inherited rule. nack's only addition is
`multiple`, for rejecting a batch by delivery tag — which this never does, and
which would have to be spelled out as `multiple=False` for no gain. `reject` has
no such parameter.

### What counts as poison: the whole contract, not just a parse failure

**This is the decision that widened stage 7 beyond the one-line change stage 6
predicted.** `payload.parse()` validates all five fields and their types, and
both failure classes dead-letter:

| Class | Raised | Example |
|---|---|---|
| malformed JSON | `json.JSONDecodeError` | what `--corrupt-every` produces |
| off-contract | `ContractViolation` | missing field, `NaN`, `true` for `seq`, a JSON array |

**The reason is stage 11.** That stage's lesson is write failures and
acknowledgment ordering, and a payload-shape failure arriving there adds a
second variable to exactly the stage that should have one. Validating where the
DLX already exists keeps stage 11 about the storage container being down.
Accepted cost: branches that can only fire if one of our own publishers breaks
its own contract.

**Verified this was not theoretical:** an injected `"temp_c": NaN` dead-lettered
on the contract check. Without it, `NaN` passes every `isinstance` check and
reaches the InfluxDB write.

### Both consumers use the same parse()

The lesson is "same bytes, two fates", which is only an honest comparison if both
consumers consider the same messages bad. Only the consequence differs:
`consumer_store` rejects, `consumer_observe` logs and moves on because
`telemetry.observe` has no DLX to reject into.

**Verified:** a 40-message burst with `--corrupt-every 5` produced 8 malformed
payloads; `telemetry.dlq` gained exactly 8, `consumer_observe` logged exactly 8
and dead-lettered none, and the `seq` absent from `consumer_store`'s stored set
were exactly `[5, 10, 15, 20, 25, 30, 35, 40]`.

### x-death carries the broker's reason, not ours

**There is no way to attach an application reason to a rejection.** `x-death`
records `rejected` for every rejection regardless of why we rejected, so a
message sitting in `telemetry.dlq` does not say whether it was truncated or
merely off-contract. Reading the payload tells you, and so does the consumer log
— which is why the log line names the failure class. Getting a reason *into* the
DLQ would mean republishing with your own headers instead of using the DLX at
all, which is a much heavier pattern.

### --requeue-poison: the pathology, on purpose

A hand-run flag on `consumer_store`, default off, following the `--recreate` /
`--corrupt-every` / `--burst` / `--ack-delay` precedent. Re-runnable matters:
stage 18's failure matrix has a "malformed payload in steady state" row.

**Measured on one poison message over 5 seconds:**

| | |
|---|---|
| Redeliveries | **58,733** (~11,700/s) |
| CPU | 73% of one core |
| `redelivered` flag | `True` on all 58,733 |
| Delivery tags | climbed 1 → 58,752 |
| `telemetry.dlq` | **0 — stayed empty throughout** |

**The empty DLQ is the whole lesson.** `x-death` is written only on an actual
dead-letter, so a requeue loop produces *no artifact at all* — no header, no
DLQ entry, nothing to inspect afterwards. The only evidence is a log line
repeating and a hot core. The same message, replayed with the flag off,
dead-lettered once immediately with a complete `x-death`.

Note `x-death`'s `count` read **1**, not 58,733: it counts **deaths, not
deliveries**. A requeue is not a death.

## Expiry (stage 8, implemented and verified)

> **Removed again at stage 9**, which returned `telemetry.store` to its
> baseline. The constant is still in `topology_spec.py` and `STORE_ARGS` carries
> the one-line edit that re-enables it. Everything below is what was measured
> while it was live, not current configuration.

`telemetry.store` carried **`x-message-ttl: 30_000`**. The contrast against
stage 7 is the whole point: those deaths needed a consumer to decide something,
these happen with **no consumer attached at any point**. Same DLQ, same
`x-death` shape, `reason: expired` rather than `rejected`.

**The DLQ now holds two populations** distinguishable only by that field — and
the stage 8 ones are *perfectly valid messages*. Nothing was wrong with them;
they only waited too long.

### Why 30 s

With no consumer, ready count plateaus at roughly **publish rate x TTL** while
the DLQ grows linearly, which makes the result a number you can predict rather
than "stuff moved". **Measured**: `telemetry.store` climbed and held at **59-61**
with both publishers running (~2/s x 30 s = 60), while the DLQ grew ~2/s, for
108 s with `consumers=0` throughout.

30 s is also an order of magnitude above the 2-4 s consumer restarts measured at
stages 6 and 7, so a routine restart never dead-letters live data and the DLQ
stays clean evidence. Stage 9 removes it again.

### The 406, finally exercised

`EXIT_MISMATCH` (4) was written at stage 4 and had **never been triggered by a
real argument mismatch** until this stage. It works. A plain declare against the
old arguments exits 4 and quotes RabbitMQ verbatim:

```
PRECONDITION_FAILED - inequivalent arg 'x-message-ttl' for queue
'telemetry.store' in vhost '/': received the value '30000' of type
'signedint' but current is none
```

**Note the knock-on**: until `--recreate` is run, `docker compose up -d --wait`
fails with `service "topology" didn't complete successfully: exit 4`, because
the one-shot declarer hits the same mismatch. That is correct behaviour, not a
broken stack — the gate is doing its job.

### Per-message TTL, and what the head-of-queue rule really means

**MQTT 5's Message Expiry Interval is RabbitMQ's per-message TTL.** Recovered
from the compiled plugin (`mc_mqtt`, `rabbitmq_mqtt-4.3.6`):

```erlang
#{'Message-Expiry-Interval' := Seconds} -> Anns0#{ttl => timer:seconds(Seconds), ...
```

So `publisher.py --message-expiry SECONDS` reaches it, and **no separate AMQP
publishing path is needed**. MQTT carries whole **seconds**, RabbitMQ stores
**milliseconds** — one second is the finest granularity from an MQTT publisher.
**Confirmed on the wire**: a message published with `--message-expiry 2` shows
`expiration=2000` in the management API, next to MCU messages showing none.

**What is actually being demonstrated is not two mechanisms.** Queue-level and
per-message TTL both expire lazily from the head. A uniform queue-level TTL plus
FIFO ordering means the head is always both the oldest message *and* the
earliest to expire, so head-first expiry is always correct. Non-uniform
per-message TTLs break that correspondence — and only then can a message outlive
its own deadline.

**Both halves measured:**

| Setup | Result |
|---|---|
| 2 s expiry, **at the head** (purge→publish in 2 ms) | died at **exactly t=2.0 s** |
| 2 s expiry, **behind 30 s messages** | survived to **~30 s** — 15x its own deadline |

The second one was nearly misread. With the MCU publishing at 1 Hz, a
`docker compose run` publisher takes ~3 s to start, so MCU messages always land
at the head first — which reproduces the lingering *accidentally* and looks
identical to the expiry property never being set. Distinguishing them needed the
`expiration=2000` evidence above. **If you re-run this, publish from a process
that is already connected**, or you are not testing what you think.

### min(queue TTL, per-message TTL) — verified both ways

No longer an assumption:

| Per-message | Queue | Expired at |
|---|---|---|
| 2 s | 30 s | **2 s** |
| 60 s | 30 s | **30 s** |

The lower of the two wins in both directions.

### The TTL does not touch unacknowledged messages

**Measured, and it matters at stage 11**: with `--ack-delay 35` (above the 30 s
TTL), `messages_unacknowledged` held at exactly **10** for 88 s — nearly three
TTLs — while ready messages kept expiring into the DLQ around them.

Once a message is delivered to a consumer it is out of the queue's expiry reach.
A consumer holding messages through a storage outage will **not** lose them to
the TTL, which removes one failure mode from stage 11's design space.

Incidentally: three consecutive 35 s blocking sleeps inside the pika callback
did **not** trip a heartbeat disconnect. The hazard recorded at stage 6 is real
but the threshold is higher than 35 s.

### A slow callback makes shutdown take prefetch x delay

Found by accident at stage 8, and it matters at stage 11. A consumer running
`--ack-delay 35` did not exit on SIGINT for minutes. It was not hung: the 10
messages already buffered locally by the prefetch are still dispatched to the
callback one at a time before `start_consuming()` can return, so worst-case
shutdown is **`consumer_prefetch_count` x callback duration** — here 10 x 35 s.

The production consequence: compose sends SIGTERM and waits **10 s** before
SIGKILL. Any callback slow enough to matter, with a prefetch of 10, cannot
finish draining in 10 s, so `docker compose stop` ends in a SIGKILL and an
unclean stop. Nothing is lost — unacknowledged messages return to ready — but
every stop looks like a crash.

**Answered at stage 11, and none of the three options listed here was the
answer.** Prefetch stayed at 10 and `stop_grace_period` was left alone; instead
the callback learned to *abandon* its work on SIGTERM. Both halves were needed:
an interruptible backoff, and then — because that alone still cost one write
timeout per prefetched delivery and died at exit 137 — skipping the write
entirely once storage is known bad. Measured at 1.2 s and exit 0. See "The
shutdown budget" under "Storage writes (stage 11)".

## Overflow (stage 9, implemented and verified)

The third and last death reason. **All three are now observed**: `rejected`
(stage 7, a consumer decided), `expired` (stage 8, the broker's clock), and
`maxlen` (stage 9, the queue was full).

Two stage 4 decisions were paid off here. `x-queue-type: classic` exists because
**`reject-publish-dlx` is classic-only**, and this is the stage that needed it.

### Oldest-out versus newest-out — and who finds out

`--burst 40` into a cap of 20, consumer stopped, run twice:

| `x-overflow` | Survived | Dead-lettered | Publisher told? |
|---|---|---|---|
| `drop-head` | **31-40** (newest) | 1-30 (oldest) | **No.** Every PUBACK success |
| `reject-publish-dlx` | **1-18** (oldest) | 19-40 (newest) | **Yes.** Reason code 151 per message |

**The second column was the expected lesson; the fourth is the better one.** The
two modes differ not only in which end of the queue is sacrificed but in
**whether the publisher is told anything at all**. `drop-head` evicts a message
that was already accepted, so there is nothing to report and the publisher
carries on believing everything landed. `reject-publish-dlx` refuses the
incoming publish, so the refusal travels back up the protocol.

That makes the choice a bigger design decision than "which end": one mode is
silent to the producer and one is not, independently of the data you keep.

### Reason code 151, and a correction to this file

**`publisher._on_publish`'s error branch had never fired.** It was written at
stage 5 to log any non-zero MQTT 5 reason code at ERROR with the affected `seq`,
and stage 9 is the first time anything made it run:

```
ERROR publish not accepted: seq=19 reason_code=151 (Quota exceeded)
```

**151 is not in the list recorded at stage 5.** That list — `0` success, `16` no
matching subscribers, `131` implementation specific error — was incomplete.
`151` (`0x97`, **Quota exceeded**) is what RabbitMQ returns when a queue with a
`reject-publish*` overflow policy is full. The list has been corrected under
"Verified broker facts".

This is also the payoff of the stage 5 decision to use MQTT 5 rather than 3.1.1
"because its PUBACK carries a reason code, which is what makes an unroutable
publish observable". Four stages later, it observed one.

### Three fates from one burst

`--burst 150` with both consumers stopped, `telemetry.observe` at its cap of 100
with `drop-head` and **no DLX**, `telemetry.store` at 20 with
`reject-publish-dlx` and a DLX:

| Queue | Kept | Lost | Evidence |
|---|---|---|---|
| `telemetry.observe` | 100 | ~53 | **none anywhere** |
| `telemetry.store` | 20 | 133 | 133 in the DLQ, and 133 errors at the publisher |

Same messages, same instant, three different outcomes. `telemetry.observe` has
been silently shedding its head since stage 4 and nothing has ever caught it —
that is the lossy-by-design policy working, and it is only visible by contrast.

### Why the cap is not in the committed configuration

`STORE_MAX_LENGTH = 20` and both overflow constants are defined but **not** in
`STORE_ARGS`. The plan says to return both queues to steady-state settings
afterwards, keeping the dead-letter exchange, and `topology_spec.py` already
called DLX-only this queue's *baseline*. A cap of 20 is an experiment value, not
a production one, and stages 10-13 want data reaching InfluxDB unimpeded.

**This was an interpretation, made without consultation**, and it is the stage 9
decision most worth revisiting: "steady-state settings" could also have meant
"keep a sensible cap". If so, the fix is one line in `STORE_ARGS`.

Two other stage 9 decisions were also taken unilaterally: **20** for the cap
(small enough that every surviving `seq` fits on one line, and deliberately not
`telemetry.observe`'s 100 so the two bounded queues do not read as one
convention), and **switching overflow mode by editing the constant rather than
adding a `--overflow` CLI flag** — stage 4 explicitly refused to make topology
values reachable from outside `topology_spec.py`, and an overflow mode is a
value where `--recreate` is an operation.

## Storage (stage 10, implemented and verified)

`influxdb:2.9` in `compose.yaml`, provisioned at first start. **No Python was
written**: this is the first stage in six that adds a container rather than
changing queue arguments, and `config.py` deliberately did not grow fields.

| Thing | Value |
|---|---|
| Image | `influxdb:2.9` (patch `2.9.1` at the time of writing) |
| Org | `thermo-warren` |
| Bucket | `telemetry` |
| Retention | `0` — infinite, stated explicitly |
| Published port | `127.0.0.1:8086` |
| Volumes | `influxdb-data` → `/var/lib/influxdb2`, `influxdb-config` → `/etc/influxdb2` |

### Naming

- Org `thermo-warren` mirrors the pinned compose project name, so the org and
  the volume prefix say the same thing. Bucket `telemetry` joins the
  `telemetry.store` / `telemetry.observe` family, so one word names the data
  everywhere.
- **`.env` keys are prefixed `INFLUXDB_`, deliberately not `INFLUX_`.** The
  `influx` CLI reads `INFLUX_HOST`, `INFLUX_TOKEN` and `INFLUX_ORG` straight
  from the environment, so `INFLUX_`-prefixed keys would silently configure any
  CLI that inherits the `env_file`. `INFLUXDB_` cannot collide.

### Retention is infinite, explicitly

`DOCKER_INFLUXDB_INIT_RETENTION=0`. The entrypoint passes `--retention` **only
when the variable is non-empty**, so leaving it out would be inheriting the
default silently rather than choosing it.

Infinite rather than a window **for the stage 8 reason**: `telemetry.observe`
was given no TTL because it already shed its head at the cap, and a second
reason for data to vanish makes it impossible to say which one acted. Stage 18
asks where a message went; an expiring bucket would be a second silent answer.
A finite retention is the storage-layer echo of stage 8's queue TTL if it is
ever wanted as its own exercise — **recorded, not scheduled**.

Consequence, accepted: the point written by hand at stage 10 is **permanent**.
It is named `stage10_check`, not `readings` (the measurement stage 11 chose), so
it can never be mistaken for real telemetry. `influx delete --predicate` removes it.

### One admin token, and why a scoped one was not possible here

`DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` is generated by hand into `.env`
(`openssl rand -hex 32`) **before the first `up`** — hex rather than base64 so
the value carries no `/`, `+` or `=` to quote in `.env` or escape into a curl.
It must exist beforehand because **from 2.9 tokens are hashed on disk and the
plaintext cannot be recovered**.

A scoped write token for `consumer_store` was considered and rejected for this
stage. **Verified in the `influx-cli` source: `influx auth create` has no flag
to supply a token value** — it takes `--read-bucket` / `--write-bucket` /
`--operator` / `--all-access` and reads `auth.GetToken()` out of the API
response. Combined with 2.9's hashing, a scoped token is printed exactly once at
creation and is unrecoverable afterwards, so it cannot be seeded into `.env` the
way the admin token can. Provisioning one would mean an executable script under
`/docker-entrypoint-initdb.d` that echoes the token to the container log for a
human to copy — a manual step in the middle of a stage whose point is
provisioning at first start.

**This is the deliberate parallel to `iot` still carrying the RabbitMQ
`administrator` tag** — same shape of compromise, same deferred-hardening note.
Deferring costs little: a scoped token can be created at any later time with one
CLI call.

### Healthcheck: `/health`, readiness not liveness

`curl -sf http://localhost:8086/health`.

- **Needs no token**, which the plan demanded — a query-based check would log an
  authentication failure every interval forever. **Verified: zero `401` or
  `unauthorized` lines** in the container log across two full starts.
- `curl` is already in the image (Debian bookworm-slim); nothing is installed.
- **`/health` returns 200 healthy, 503 unhealthy**, so it maps cleanly onto
  `curl -f`. **`/ready` has no failure status code in the OSS API spec** and so
  cannot express "not ready" to a healthcheck at all.
- **Readiness, the opposite of rabbitmq's deliberately liveness-only check.**
  Stage 11 gates `consumer-store` on this one, and a socket that merely accepts
  would not be enough.

### Two volumes, destroyed together

The image declares `VOLUME /var/lib/influxdb2 /etc/influxdb2`. Both are named.

- **Data** holds `influxd.bolt` and the TSM engine. Whether `influxd.bolt`
  exists is exactly what decides if the setup wrapper runs.
- **Config** holds `/etc/influxdb2/influx-configs`, where the entrypoint writes
  the CLI's host and admin token after setup. Left anonymous, a
  `docker compose down` drops the CLI's credentials into a throwaway volume
  while the data survives, and in-container `influx` commands then fail for no
  visible reason.

**Verified**: `docker compose exec influxdb influx bucket list` needs **no
`--token`**, before and after a `down`/`up`. If it ever asks for one, the
`influxdb-config` volume is the thing to check — that diagnostic is what the
second volume buys.

Destroy them together. Data alone and setup re-runs underneath a config that
already names an org; config alone and setup is skipped while the CLI has no
token. `down -v` takes both, which is the only always-coherent combination.

### Stage 10 DoD — verified against real behaviour

| Check | Result |
|---|---|
| `docker compose up -d --wait` | exit 0, **12.0 s** cold (8.1 s warm), `influxdb` Healthy |
| Bucket exists | `telemetry`, retention printed as **`infinite`**, org `thermo-warren` |
| CLI needs no token | `influx bucket list` and `influx org list` both work bare |
| Write one point by hand | `stage10_check,src=hand value=1` accepted |
| Query it back | returned with `_value 1`, `_measurement stage10_check` |
| `down` (no `-v`) then `up` | bucket and point both survive; setup does not re-run |
| Healthcheck auth noise | **zero** `401`/`unauthorized` lines |
| Tests still green with no broker | 85/85, unchanged |

## Storage writes (stage 11, implemented and verified)

`consumer_store` writes each reading to InfluxDB between the parse and the ack.
New module `storage.py`, new dependency `influxdb-client` (1.50.0 installed),
seven new `config.py` fields, and a `depends_on` gate in compose.

### The schema

| Thing | Value |
|---|---|
| Measurement | `readings` |
| Tag | `device` — the only one |
| Fields | `temp_c`, `humidity_pct` (floats), `seq` (integer) |
| Timestamp | `ts_ms`, millisecond precision |

- **`readings`, not `telemetry`.** That word already names the bucket, the queue
  prefix and (from stage 12) the InfluxQL database, so the measurement would read
  as `FROM telemetry` in database `telemetry` in every query. `readings` names
  what one row is — the same principle that named the queues after what their
  consumers do.
- **`seq` is a field, not a tag.** Tag values are indexed and every distinct tag
  set is a series; `seq` is monotonic and unbounded, so tagging it would create
  one series per message (~86,400/day at 1 Hz) against a bucket whose retention
  is infinite. As a field it is still selectable and still the gap-and-duplicate
  evidence stage 18 wants.
- **`device` as the only tag pays off immediately.** The ESP32 was live during
  stage 11 verification, and `esp32c3-01` and `sim-01` each carry their own
  independent `seq` counter starting at 1. They do not collide, because the tag
  set separates the series. A per-device gap analysis over both came back with
  **zero gaps and zero same-`seq` collisions**.

### Synchronous, and blocking inside the pika callback

- **`SYNCHRONOUS` is a requirement, not a tuning choice.** The batching write API
  returns once the point is buffered and flushes later on its own thread, so
  acking after it would ack before any confirmation exists — and would fail
  *silently*, looking correct until an outage.
- **The write blocks the AMQP I/O loop**, deliberately, rather than moving to a
  worker thread. Ack-after-write stays four readable lines, and the stalled-loop
  hazard stage 8 set up stays visible. A worker thread would have made stage 11 a
  concurrency stage.

### The failure policy: retry, then requeue — never dead-letter

Three attempts, backoff `0.5 → 1 → 2 s`, then `basic_reject(requeue=True)`.

- **The DLQ keeps meaning exactly one thing**: this message failed the payload
  contract. A good reading that arrived during an outage is not poison, `x-death`
  has no room for our reason, and a bounded-then-dead-letter policy would start
  discarding valid data the moment an outage outlasted the budget — surfacing as
  a hole in the stage 13 dashboard.
- **The backoff sleeps after every failed attempt, the last one included.** That
  final 2 s is what paces the requeue; without it the message returns
  immediately and a storage outage becomes the hot loop `--requeue-poison`
  exists to demonstrate.
- **Every sleep is interruptible** via `amqp.sleep_unless_stopping()`.
- **Auth and schema failures are treated identically to an outage**, on purpose.
  A 401 or a 422 retries and requeues forever with a loud log rather than being
  classified as permanent. The uniform path is simpler, loses nothing, and the
  backoff paces what would otherwise be a hot loop; the cost is that a genuinely
  permanent failure never resolves itself.

### The shutdown budget, which needed two fixes rather than one

**Measured, and the first fix was not enough.** Stage 8 recorded that shutdown
costs `prefetch × callback duration`. Making the backoff interruptible was the
obvious fix and it was insufficient: SIGTERM while storage was **paused** still
took the full 10 s grace and died with **exit 137**, because pika drains its
prefetched deliveries through the callback and each one paid a full 2 s write
timeout *before* the handler could notice the shutdown. 10 × 2 s against a 10 s
grace.

The second fix is a `storage_down` flag in the handler closure: once one message
has exhausted its budget, a shutdown skips the write attempt outright. Re-measured
at **1.2 s and exit 0**, with the whole prefetched batch returned to ready inside
the same millisecond.

**Conditional on the flag, not on `is_stopping()` alone**, so a normal shutdown
still writes and acks its prefetched batch. Skipping those would return ten
perfectly writable messages to ready on every restart and log ten `redelivered`
warnings, draining that warning of the meaning it was added to carry.

### Duplicates: overwrite, no dedup state, logged on success

**Verified**: three writes of one identity produce **one point**, holding the
last value. Point identity is measurement + tag set + timestamp, so an
at-least-once redelivery is idempotent by construction — which is what the
publisher-stamped `ts_ms` bought at stage 5. No dedup machinery exists and none
is wanted; a seen-set would need bounding, would be lost on restart exactly when
duplicates occur, and `seq` restarts at 1 each publisher run so it would be
unsound anyway.

A `redelivered` WARNING is logged **on the success path only**. Every requeue
comes back with `redelivered` set, so logging before the write would fill an
outage with lines about writes that never happened. **Verified**: a restart
during an outage produced exactly **10** of them — `consumer_prefetch_count`,
confirming stage 6's claim that prefetch *is* the duplicate window.

### Explicit heartbeat, and why it arrived now

`amqp.connect()` passes `heartbeat=60` explicitly (`HEARTBEAT_SECONDS`). It
matches what would have been negotiated; the point is having a stated number to
size the blocking write against, instead of stage 8's unmeasured "above 35 s".
pika sends at half the interval and the broker closes after two are missed, so
roughly 120 s of silence is fatal against a worst-case write budget of ~7.5 s.

**Verified**: 4.5 minutes of continuous 7.5 s stalls (storage paused) produced
**zero** connection losses, cancels or reconnects, with the consumer still
attached and unacked pinned at 10 throughout. This closes the
explicit-over-inherited gap carried since stage 6.

### Configuration

Seven fields, and **every one has a default** — a deliberate departure from
`rabbitmq_host`, which has none. `Settings` is instantiated at import for every
module, so a required field would make `topology`, `publisher` and
`consumer_observe` refuse to start without a token none of them touches. "Fail
loudly by name" survives in `storage.connect()`, which rejects an empty token
with a message naming `INFLUXDB_TOKEN`, in the one process that needs it.

`influxdb_timeout_ms = 2000` (a fifth of the library default) because the write
blocks the I/O loop. `influxdb_write_attempts` / `_retry_delay` mirror
`rabbitmq_connect_attempts` / `_retry_delay`: attempts and base delay are
configuration, the doubling is in the code.

**`EXIT_UNCONFIGURED = 5`** is local to `consumer_store`, exactly as `topology.py`
owns `EXIT_MISMATCH = 4`. Distinct from 3, which is the *broker* refusing
credentials that were supplied.

### compose

`consumer-store` gains `depends_on: influxdb: condition: service_healthy`, which
is what stage 10's readiness-style healthcheck was written for. Not load-bearing
— the retry and requeue would cover the gap — but without it every message
published during the startup window pays 3.5 s of backoff for nothing.
`INFLUXDB_URL: http://influxdb:8086` overrides the host-mode value in `.env`, the
same shape as `RABBITMQ_HOST`.

**Observed as a side effect**: after `docker unpause`, the gate refuses to start
`consumer-store` until the healthcheck recovers, reporting `dependency failed to
start: container ... is unhealthy`. Correct behaviour, but it looks like a
failure — wait for health rather than retrying the command.

### Stage 11 DoD — verified against real behaviour

| Check | Result |
|---|---|
| Points appear in near-real-time | `_time` correct to the millisecond, `seq` typed `long`, `temp_c` typed `double` |
| Logged `seq` == stored `seq` | 119 vs 119 over a 150 s window, no gaps, sets identical |
| Ack strictly follows a confirmed write | Asserted as an ordering in tests; proved live by the outage, where the queue did **not** drain |
| Outage: `docker compose stop influxdb` | 3 attempts at 0.5/1/2 s, requeue; `telemetry.store` grew to 486 ready with unacked pinned at **10**; `telemetry.dlq` **0**; `telemetry.observe` untouched |
| Nothing silently lost | Both devices: **zero gaps**, zero same-`seq` duplicates, across the whole outage |
| Outage: `docker pause influxdb` | `ReadTimeoutError` at **1.999 s**, cycle ~7.5 s, no heartbeat loss over 4.5 min |
| SIGTERM during an outage | **1.2 s, exit 0** after the `storage_down` fix (was 10.2 s, exit 137) |
| Recovery | Queues drain to 0, exactly 10 `redelivered` warnings, DLQ still 0 |
| Tests with nothing running | **114/114**, no broker and no InfluxDB |

## Visualization (stage 12, implemented and verified)

Grafana, plus the DBRP mapping that exposes the bucket to InfluxQL. **No Python,
no new dependency, no rebuild** — two compose services, one shell script and one
provisioning file.

| Thing | Value |
|---|---|
| Image | `grafana/grafana:13.2` (patch `13.2.2` at the time of writing, Alpine base, runs as uid 472) |
| Published port | `127.0.0.1:3000` |
| Datasource uid | `influxdb-telemetry` — pinned, because stage 13's dashboard references it |
| InfluxQL database | `telemetry` (= the bucket name), retention policy `autogen` |
| State volume | **none**, deliberately |
| Files | `grafana/provisioning/datasources/influxdb.yaml`, `influxdb/dbrp.sh` |

### The DBRP mapping did not have to be created

**InfluxDB 2.x synthesises a read-only *virtual* DBRP mapping for any bucket that
has no explicit one**, using the bucket name as the database and `autogen` as the
retention policy. Verified before anything was written: `influx v1 dbrp list`
showed `telemetry` under `VIRTUAL DBRP MAPPINGS (READ-ONLY)`, and
`GET /query?db=telemetry&q=SELECT ... FROM readings` already returned HTTP 200
with rows. **The plan's stage 12 text is wrong on this point** — see "Correction
to the staged plan" below.

It is declared explicitly anyway, on the standing "explicit over inherited"
rule: the repo should state that the database exists rather than depend on a 2.x
convenience that InfluxDB 3 will not carry.

**Verified both ways:** creating the explicit mapping makes the virtual one
disappear from the listing, and deleting the explicit one brings it straight
back. Explicit shadows virtual; the two never coexist for one bucket.

### `influxdb/dbrp.sh` — and the grep that is the whole trick

A one-shot service on the `influxdb:2.9` image (the CLI ships inside the server
image, so nothing extra is pulled), the same shape as `topology`: runs to
completion, exits, and `grafana` gates on `service_completed_successfully`.

Compose re-runs it on every `up`, so it must be idempotent, and the naive check
is a trap:

- **`influx v1 dbrp list --json` INCLUDES virtual mappings**, each carrying
  `"virtual": true`; an explicit one carries `"virtual": false`. Matching on the
  database name alone would always find the virtual mapping and therefore never
  create anything. The check greps for `"virtual": false`.
- **`influx v1 dbrp create` takes `--bucket-id`, not a bucket name**, so the id
  is looked up first with `influx bucket list --name ... --hide-headers | cut -f1`.
  That nested command substitution is why this is a script file rather than an
  inline compose `command:` — inline it would also need compose's `$$` escaping.
- **`jq` is not in the image** (Debian 12 base). `grep`, `sh`, `bash` and `curl` are.
- **`--rp autogen` deliberately reuses the virtual mapping's retention-policy
  name**, so any query written against the virtual mapping keeps working.
  `--default` is what lets a query name the database bare instead of
  `"telemetry"."autogen"`.

**`INFLUX_HOST` / `INFLUX_ORG` / `INFLUX_TOKEN` appear in this service's
`environment:` and nowhere else.** That is the single deliberate exception to
stage 10's rule that `INFLUX_`-prefixed names stay out of `.env` — the rule
exists so no service is configured silently by inheriting `env_file`, and this
is the one service that actually wants them, so it maps them by hand from the
`INFLUXDB_` keys and takes no `env_file` at all.

### The datasource: a token header, not user/password

Checked against Grafana's current documentation rather than memory. For InfluxDB
**2.x with InfluxQL** the provisioned shape is `jsonData.dbName` plus a custom
`Authorization` header — **not** the `user`/`password` pair a 1.x server takes,
and not the `token` field the Flux shape uses:

```yaml
jsonData:      { version: InfluxQL, dbName: $INFLUXDB_BUCKET,
                 httpMode: GET, httpHeaderName1: Authorization }
secureJsonData:{ httpHeaderValue1: Token $INFLUXDB_TOKEN }
```

- **`dbName` is the DBRP database, not the measurement.** The measurement is
  `readings` and belongs in a query's `FROM`. Stage 11 chose two different words
  precisely so this never reads as `FROM telemetry` in database `telemetry`.
- **`$VAR` lookup works in provisioning files, including inside
  `secureJsonData`**, so the token stays in `.env` and never enters the repo.
  Grafana treats any text after a `$` as a variable name and would corrupt a
  value containing one — `INFLUXDB_TOKEN` is hex rather than base64, a stage 10
  decision taken for `.env` quoting, which pays off a second time here.
- **`access: proxy`.** Grafana's backend makes the query, so the browser never
  talks to InfluxDB and 8086 can stay on loopback. `direct` would require
  exposing 8086 to the LAN and would hand the token to every browser that loads
  a dashboard.
- **A provisioned datasource comes back `"readOnly": true` from the API** and
  cannot be edited in the UI. Correct, and worth knowing before stage 13: the
  dashboard references it, it is not adjusted by hand.

### No state volume, deliberately

Grafana keeps users, preferences and UI-created dashboards in
`/var/lib/grafana/grafana.db`. It is left in the container's writable layer.

**The stage's own verification is the reason.** The DoD asks for a teardown that
proves provisioning rather than a stale volume — and with a named volume, a
datasource surviving `docker compose down` would prove nothing, because the
provisioner writes into a database that persisted. With no volume every boot
starts empty, so a datasource that is present afterwards can only have come from
the file. Distinguishing the two otherwise would need `down -v`, which is
all-or-nothing and would destroy the readings the dashboard exists to render.

Cost, accepted: **UI panel edits and Explore history are scratch and do not
survive `down`** (they do survive `stop`/`restart`). Stage 13 provisions the
dashboard from a file anyway. Confirmed after a `down`: three volumes, none of
them Grafana's.

### Healthcheck, and a lag worth knowing

`curl -sf http://localhost:3000/api/health` — no authentication needed, so it
cannot log a 401 every interval forever, the same reasoning as `influxdb`'s
`/health`. **Both `curl` and `wget` are present in the image**, checked before
the line was written; `curl` was chosen to match `influxdb`'s check exactly.

**The container's health status lags real readiness.** Measured across a
`restart`: the HTTP port is closed for ~2 s (curl exits 000, and a `curl -s` into
a JSON parser fails on an empty body rather than on anything meaningful), the
datasource answers `"status":"OK"` at ~3 s, and Docker still reports `starting`
until ~6 s, because `start_period: 15s` and `interval: 10s` delay the first
probe. Conservative, not wrong — but poll the endpoint, don't race it.

### Stage 12 DoD — verified against real behaviour

| Check | Result |
|---|---|
| `grafana` starts with the datasource already present | `GET /api/datasources` → 1 entry, uid `influxdb-telemetry`, `dbName` substituted to `telemetry` |
| Connection test passes | `GET /api/datasources/uid/influxdb-telemetry/health` → `"status":"OK"`, *"2 measurements found"* (`readings` + the permanent `stage10_check` point) |
| A bare query returns rows | `POST /api/ds/query`, InfluxQL through Grafana's proxy → three frames (`readings.temp_c`, `.humidity_pct`, `.seq`) |
| Both devices reachable | `count(seq)` grouped by `device` over 24 h: `sim-01` 13187, `esp32c3-01` 12848 |
| Teardown, **no `-v`**, then up | Datasource and health both still OK; `docker volume ls` shows **three** volumes, none Grafana's |
| `dbrp` idempotent across re-runs | Second `up`: *"already present"*, and exactly **1** explicit mapping, not two |
| Explicit mapping exists | `influx v1 dbrp list` → one row, `VIRTUAL` section now empty |
| Tests with nothing changed | **114/114** |

## Dashboard (stage 13, implemented and verified)

Two panels reading what `consumer_store` wrote. **No Python, no new dependency,
no rebuild, and no change to `compose.yaml`** — the `./grafana/provisioning`
bind mount from stage 12 already covers a new `dashboards/` subdirectory.

| Thing | Value |
|---|---|
| Dashboard uid | `telemetry-live` — pinned, for the same reason the datasource's is |
| Title | `Live telemetry` |
| Time range / refresh | `now-15m` / `5s` |
| Screenshot | `src/telemetry/docs/stage13-dashboard.png` — how it was taken is below |
| Panels | `Temperature` (unit `celsius`) and `Humidity` (unit `humidity`), side by side, 12 columns each |
| Provider name | `telemetry` |
| Files | `grafana/provisioning/dashboards/dashboards.yaml`, `grafana/provisioning/dashboards/telemetry.json` |

### Hand-written JSON, not a UI export

An export carries `__inputs` / `${DS_INFLUXDB}` placeholders, which the UI's
import dialog resolves and a file provider has no dialog to run — and nothing
substitutes a variable into a dashboard JSON at provisioning time, which was
verified separately below. Plus a pile of editor state. Every panel and every
target names `{"type": "influxdb", "uid": "influxdb-telemetry"}` directly
instead — which is what the uid was pinned at stage 12 for.

`"id": null` and `"schemaVersion": 42`. **42 is `DASHBOARD_SCHEMA_VERSION` in
13.2.2**, read out of `public/app/features/dashboard/state/DashboardMigrator.ts`
in the running container rather than guessed; a lower number would silently be
migrated forward on load.

JSON has no comments, so the reasoning lives in the dashboard's and each panel's
`description` field, where Grafana also shows it in the UI. The provider file is
YAML and carries its comments normally.

### Raw points, no `GROUP BY time()`

```sql
SELECT "temp_c" FROM "readings" WHERE $timeFilter GROUP BY "device"
```

`"rawQuery": true`, so the JSON states the InfluxQL it runs instead of encoding
it as the query builder's structured model. No `mean()`, no `fill()`, no time
bucketing: at 1 Hz over 15 minutes the panel plots the points the pipeline
actually stored. Bucketing would average a duplicate away and interpolate across
a gap — the two things stages 9 and 18 exist to make visible. ~890 points per
series per panel, measured through the API; panel rendering at that density was
not timed.

### `GROUP BY "device"`, and no template variable

One series per publisher, `sim-01` and `esp32c3-01` together in one panel. A
`WHERE device = 'sim-01'` filter would hide the hardware the project spent four
stages on.

**A template variable was rejected**: `SHOW TAG VALUES FROM readings WITH KEY =
device` lists five devices, three of them the permanent stage 11 artefacts
(`burst-probe`, `int-probe`, `overwrite-probe`). A short time range excludes them
for free — they have no recent points — so the dashboard needs no filtering
logic at all. **Verified**: the 15-minute query returns exactly two frames.

**`"alias": "$tag_device"`** on each target, so the legend reads `sim-01` rather
than `readings.temp_c {device: sim-01}`. Verified through `POST /api/ds/query`:
the frames come back named `sim-01` and `esp32c3-01`, with `labels` intact.

### 5 s refresh, because 5 s is the floor

**`min_refresh_interval = 5s` in Grafana's `defaults.ini`** — read out of the
running 13.2.2 image, not remembered. A dashboard asking for less does not get
it; what Grafana does with the request (clamp, or drop the refresh) was **not
tested**, because this dashboard asks for exactly the floor. Riding the floor
keeps stage 13 a pure provisioning stage: no
`GF_DASHBOARDS_MIN_REFRESH_INTERVAL` override, and `compose.yaml` is untouched.
Going faster is one explicit environment line on the `grafana` service if a later
stage ever wants it.

### `insertNulls`, so an outage is a break and not a straight line

`custom.insertNulls: 3000` on both panels. **Verified present in 13.2.2**:
`public/app/plugins/panel/timeseries/config.ts` defines path `insertNulls`
("Disconnect values"), default `false`, applied only when `drawStyle` is `line`,
taking a millisecond threshold. 3000 is three times the 1 Hz publish interval of
*both* publishers (the ESP32's loop is 1 s plus execution time, so it drifts),
which is wide enough that normal jitter cannot draw a false break.

**Observed working**, in the screenshot below. The `docker compose down` / `up`
of the teardown check left `sim-01` a **10210 ms** gap at 21:35:44, measured in
the query response, and the rendered trace breaks there instead of drawing a line
across — while the 1001 ms normal interval either side stays connected. A
`docker compose restart publisher` does **not** produce a break: it costs about a
second, under the threshold, which is the behaviour wanted.

### `allowUiUpdates: false` is enforced — but `meta.canSave` does not say so

`GET /api/dashboards/uid/telemetry-live` reports `"provisioned": true`,
`"provisionedExternalId": "telemetry.json"` — **and `"canSave": true`,
`"canEdit": true`**, which is about permissions and is not the provisioning
guard. The guard is real and was tested rather than assumed: a `POST
/api/dashboards/db` with a changed title returns **HTTP 400 `{"message":"Cannot
save provisioned dashboard"}`** and the title is unchanged afterwards.

`"editable": false` in the JSON is the matching statement on the dashboard side.
Flipping it to `true` is the one-line way to allow scratch edits in the browser
during a stage 18 investigation — they still cannot be saved, and they die at the
next `down` regardless.

### `$VAR` substitution stops at the YAML

Stage 12's datasource file resolves `$INFLUXDB_BUCKET` and `$INFLUXDB_TOKEN` out
of Grafana's environment. **That does not extend to a dashboard JSON.** Verified:
a panel titled `Humidity $INFLUXDB_BUCKET` came back from the API with the text
still literal. The measurement, field and tag names in the panel queries are
therefore duplicated from `storage.py` with nothing available to remove the
duplication — see "Where things live" for what that costs.

### `updateIntervalSeconds: 10` — a rescan, not a restart

Verified: editing the title in `telemetry.json` reached the API within the
interval with no `docker compose restart grafana`. The provider *config* file is
read at startup only; the dashboard JSON is rescanned on the timer. So a panel
can be iterated on by editing the file, which matters because the file is the
only way to edit it.

### The teardown proves provisioning, and did so by deleting something

`docker compose down` (no `-v`) then `up -d`. Before the teardown, Grafana also
held a `New dashboard` someone had created in the UI. Afterwards **only
`telemetry-live` was listed** — the clicked-in one was gone with the container's
writable layer, and the provisioned one was back. That is the stage 12 argument
for having no state volume, demonstrated rather than restated.

### Provisioning log noise, pre-existing

Every Grafana start logs two `level=error` lines:

```
Failed to read plugin provisioning files from directory  path=/etc/grafana/provisioning/plugins
can't read alerting provisioning files from directory    path=/etc/grafana/provisioning/alerting
```

The read-only bind mount replaces the **whole** `/etc/grafana/provisioning`
directory, so only the subdirectories that exist in the repo exist in the
container. Present since stage 12 and harmless; provisioning of what is there
succeeds in the same second. Creating empty `plugins/` and `alerting/`
directories to silence it would add two directories that declare nothing.

### Screenshotting the dashboard without the renderer plugin

Most of the verification below goes through Grafana's HTTP API — the same queries
the panels issue, through the same datasource proxy — but an API cannot show that
a panel *draws*. Grafana renders server-side only with the image renderer plugin,
which is not installed and was not installed for this.

`docs/stage13-dashboard.png` was captured with the host's own headless Chrome
instead. **Two things make this harder than it looks, both learned the hard way:**

- **Credentials in the URL do not work.** `http://user:pass@localhost:3000/d/...`
  lands on the login page: Chrome drops embedded credentials, and Grafana's
  frontend wants a session rather than basic auth regardless.
- **So the session cookie has to be injected**, which `--screenshot` alone cannot
  do. `POST /login` with a JSON body returns a `grafana_session` cookie; Chrome is
  then launched with `--remote-debugging-port`, and over the DevTools protocol:
  `Network.setCookie`, `Page.navigate`, wait, `Page.captureScreenshot`.

Two other details worth keeping: `?kiosk` on the URL drops the nav chrome, and
the wait before capturing must be generous (~14 s used) because the panels draw
well after the load event — there is no load event to race.

### Stage 13 DoD — verified against real behaviour

| Check | Result |
|---|---|
| Dashboard present without anyone opening the UI | `GET /api/search?type=dash-db` → `telemetry-live`; `provisioned: true`, `provisionedExternalId: telemetry.json` |
| Both panels render live data | `POST /api/ds/query`, both panel queries → **two frames each**, ~890 points per series over 15 min, named `sim-01` / `esp32c3-01`; and drawn, in `docs/stage13-dashboard.png` — two panels, two series each, legend by device, °C and %H |
| `insertNulls: 3000` draws a break | The teardown's 10210 ms gap in `sim-01` renders as a **disconnect**, the 1001 ms normal interval does not |
| Artefact devices absent | Two frames, not five — the short range excludes them with no filter |
| **Publisher range changed** | `TEMP_MIN_C, TEMP_MAX_C = 40, 50`, `restart publisher` → `sim-01` stepped ~33 → 40 on the first tick, well inside one 5 s refresh; reverted → back to ~23 |
| Provisioned dashboard cannot be saved | `POST /api/dashboards/db` → **HTTP 400**, "Cannot save provisioned dashboard", title unchanged |
| File edit picked up without a restart | Title change visible via the API inside `updateIntervalSeconds: 10` |
| Teardown, **no `-v`**, then up | `telemetry-live` back from the file; a UI-created dashboard **gone**; three volumes, none Grafana's |
| Steady state unchanged | three healthy, three up, `topology` and `dbrp` both `Exited (0)` |
| Tests with nothing changed | **114/114**, no broker and no InfluxDB |

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
- **Tests must pass with no broker running.** 85/85 at stage 9, across
  `test_topology.py`, `test_publisher.py`, `test_consumers.py` and
  `test_payload.py`. The last two are split on purpose: `test_payload.py`
  asserts what `parse()` accepts and rejects, `test_consumers.py` asserts what
  each consumer *does* about it. Both are needed — the handler catches
  `ContractViolation` broadly, so "parse rejects a `NaN`" and "the consumer
  dead-letters a `NaN`" are separate claims, and the second is parametrized over
  every off-contract class. `declare(channel)`
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
- **AMQP, management and InfluxDB bound to 127.0.0.1; MQTT is not.** 1883
  publishes on `0.0.0.0` from stage 17, because the ESP32 reaches it over the
  LAN and no narrower binding works. **8086 stays on loopback** — nothing off
  this host talks to InfluxDB: Grafana reaches it over the compose network with
  `access: proxy`, and the published port exists only for host mode and hand
  checks. **3000 is on loopback too**, from stage 12. Exposing 1883 exposes the
  `iot` user, which still carries the `administrator` tag — the answer to that
  is the scoped application user in "Open questions", not a bind address.
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
- **`dbrp` is the second permanent one-shot**, added at stage 12. From here the
  steady state has **two** `Exited (0)` services, not one. It gates `grafana` on
  `service_completed_successfully`, the same enforcement idiom as stage 4's gate
  — and not merely for ordering, since the datasource's own health check issues
  a query against the v1 endpoint.
- **`grafana` binds 127.0.0.1:3000 and has no state volume.** It takes no
  `env_file`: the four keys it needs are passed by hand, so the broker
  credentials never enter its environment.
- **`influxdb` gates `consumer-store`** from stage 11
  (`condition: service_healthy`), which is what its readiness-style healthcheck
  was written for at stage 10. Not load-bearing — the write retry and requeue
  would cover the gap — but without it every message published during the
  startup window pays 3.5 s of backoff for nothing. Note the gate also refuses
  to start `consumer-store` while `influxdb` is merely *recovering*, e.g. right
  after a `docker unpause`; wait for health rather than retrying the command.

### Running the stack

```
docker compose build          # only after a pyproject.toml change -- see below
docker compose up -d          # start everything, detached
docker compose logs -f rabbitmq   # follow one service; Ctrl-C detaches the reader only
```

Stopping: **`docker compose stop`** keeps the containers, **`down`** removes them
but keeps the named volumes, **`down -v`** destroys all three —
`rabbitmq-data`, `influxdb-data` and `influxdb-config`. Keep `-v` for when it is
meant: destroying those volumes is the only way
`RABBITMQ_DEFAULT_USER`/`PASS` and the `DOCKER_INFLUXDB_INIT_*` values get
re-applied, since both sets apply solely on first boot against an empty data
directory. Note `-v` is all-or-nothing across services — there is no "reset only
InfluxDB" short of `docker volume rm` by name.

**Expected steady state from stage 12:** `rabbitmq`, `influxdb` and `grafana` all
Up (healthy), and `publisher`, `consumer-observe` and `consumer-store` all Up.
**`topology` and `dbrp` are both `Exited (0)`** — the two one-shot declarers
having done their jobs, which is what everything else gates on. Both queues sit
near `ready=0` with `consumers=1` each. Management UI at
`http://localhost:15672`; InfluxDB UI at `http://localhost:8086`, logging in with
`INFLUXDB_USERNAME`/`_PASSWORD`; Grafana at `http://localhost:3000` with
`GRAFANA_USERNAME`/`_PASSWORD`.

Note `down -v` still destroys **three** volumes, not four: Grafana has none.

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
  subscribers (could not route to any queue), `131` implementation specific
  error (e.g. a target classic queue unavailable), and **`151` Quota exceeded**
  — returned when a destination queue's `reject-publish*` overflow policy
  refuses the publish. **Observed at stage 9**; the first three were read from
  documentation at stage 5 and the list was incomplete.
- **Only a `reject-publish*` overflow tells the publisher anything.**
  `drop-head` evicts a message that was already accepted, so the PUBACK is a
  success and the producer never learns it lost data.
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

**Reading the broker's own Erlang** (stage 8)

- **`strings` is not installed in the broker image**, and beam atom tables are
  compressed so copying a `.beam` out and running `strings` locally finds almost
  nothing either.
- **`rabbitmqctl eval` with `beam_lib` is the way.** Atom table:
  `beam_lib:chunks(code:which(Mod),[atoms])`. Full source, where debug info
  survives: `beam_lib:chunks(code:which(Mod),[abstract_code])` piped through
  `erl_prettypr:format(erl_syntax:form_list(AC))`. That is how the MQTT expiry
  mapping above was established rather than guessed. Core broker modules such as
  `rabbit_variable_queue` are stripped; plugin modules like `mc_mqtt` are not.

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

**Dead-lettering — observed at stage 7, not just read**

- **`x-death` records the broker's reason, never the application's.** Every
  rejection lands as `reason: rejected` whatever the consumer's own reason was,
  and RabbitMQ offers no way to attach one. Distinguishing "truncated" from
  "off-contract" means reading the payload or the consumer log.
- **A requeue writes no `x-death` at all.** The header is written only on an
  actual dead-letter, so a redelivery loop leaves no artifact anywhere.
- **`x-death`'s `count` counts deaths, not deliveries.** A message redelivered
  58,733 times and then dead-lettered once reads `count: 1`.
- **`x-death.exchange` is the *original* exchange** (`amq.topic`), not the DLX.
- **The original routing key survives** in `routing-keys`, confirming the
  deliberate choice to leave `x-dead-letter-routing-key` unset.
- **RabbitMQ also sets flat `x-first-death-*` and `x-last-death-*` headers**
  (exchange, queue, reason) alongside the `x-death` array. Convenient for a
  single death; the array is still the place to read the count.
- **`content_type: application/json` survives dead-lettering**, so the MQTT 5
  Content Type property set by the publisher is still visible on the DLQ
  message.

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
  queue** — **measured at stage 8**: 2 s at the head died at 2.0 s; the same 2 s
  behind 30 s messages survived ~30 s. Queue-level TTL appears not to behave
  this way only because a uniform TTL plus FIFO makes head-first expiry always
  correct; the machinery is the same.
- **The effective deadline is `min(queue TTL, per-message TTL)`** — verified
  both directions at stage 8 (2 s under a 30 s queue expired at 2 s; 60 s under
  a 30 s queue expired at 30 s).
- **A TTL never applies to unacknowledged messages.** Verified at stage 8:
  unacked held at 10 for 88 s against a 30 s TTL while ready messages expired
  around them. Delivery takes a message out of the queue's expiry reach.
- **MQTT 5's Message Expiry Interval becomes the per-message TTL.** `mc_mqtt`
  does `'Message-Expiry-Interval' := Seconds -> ttl => timer:seconds(Seconds)`;
  MQTT carries seconds, RabbitMQ stores milliseconds, and it surfaces as the
  AMQP `expiration` property (`expiration=2000` observed for a 2 s expiry).
- **Redeclaring a queue with different arguments raises 406 rather than updating
  it.** Since stages 8 and 9 change queue arguments, destructive redeclare had to
  be supported from the start.

## Correction to the staged plan

Stage 12's text says InfluxDB 2.x "needs a mapping exposing the bucket under a
v1-style database name". The mapping is needed; **it does not have to be
created**. 2.x synthesises a read-only virtual DBRP for any bucket without an
explicit one, and InfluxQL worked through it before a line was written — checked
against the running instance, not assumed. Declaring one explicitly is a choice
taken on the "explicit over inherited" rule, and the plan reads as though it were
forced. See "Visualization (stage 12)" above.

Stage 5's definition of done in `mcu-rabbitmq-staged-plan.md` says both queue
depths climb **in lockstep**. **That is wrong.** `telemetry.observe` carries
`x-max-length: 100` with `drop-head`, so it **plateaus at 100** while
`telemetry.store` grows unbounded. A burst does not add the same count to both.

This is stage 9's lesson arriving early, not a bug. Treat the plan's wording as
superseded; the stage 5 section of `docs/verification-log.md` explains the cap.

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
- **Three JSON typing traps, all verified in the interpreter at stage 7**, and
  all of them silent:
  - **`json.loads("29")` is an `int`, `json.loads("29.0")` is a `float`.** JSON
    has one number type, so which one arrives depends entirely on the
    publisher's formatting. A strict `isinstance(x, float)` would dead-letter
    valid readings after a firmware format-string change, and they would look
    perfectly correct sitting in the DLQ.
  - **`isinstance(True, int)` is `True`** — `bool` subclasses `int`, so a JSON
    `true` passes a bare int check, and `isinstance(True, (int, float))` passes
    too.
  - **Python's `json` accepts `NaN`, `Infinity` and `-Infinity`** as an
    extension to the spec. A `NaN` passes every `isinstance` check and only
    fails later, at the storage write. `payload.py` requires `math.isfinite`.
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
- **InfluxDB's entrypoint runs `main()` twice, so `found existing boltdb file,
  skipping setup wrapper` appears on a genuine first boot as well.** The
  entrypoint ends with `exec setpriv --reuid=influxdb ... "$BASH_SOURCE"` to
  drop from root, and the re-exec re-enters `main()` — by which point setup has
  already created the bolt file. **That log line is therefore not the
  first-boot marker it looks like.** Measured: **first boot logs it once**
  alongside `Executing user-provided scripts` and `initialization complete`;
  **a restart logs it twice with neither of those**. Count the setup markers,
  not the skip line.
- **InfluxDB's `DOCKER_INFLUXDB_INIT_*` variables apply only on first boot
  against an empty data volume** — the same trap as
  `RABBITMQ_DEFAULT_USER`/`PASS`, and for the same reason. Editing the org,
  bucket, retention or token later changes nothing until `influxdb-data` is
  destroyed.
- **First start is two-phase and 8086 does not listen during it.** The
  entrypoint boots a temporary `influxd` on port 9999 (`INFLUXD_INIT_PORT`),
  runs `influx setup` against it, kills it, then starts the real server. A
  healthcheck against 8086 therefore needs a `start_period` covering a startup
  *sequence*, not merely a slow boot. 30 s is comfortable — the measured cold
  `up -d --wait` was 12 s.

- **`influxdb-client`'s `write_precision` defaults to `'ns'`, and the Point's own
  precision does not rewrite the number.** Verified in 1.50.0: an int timestamp
  is serialized verbatim at every serialization precision, so the only thing that
  assigns meaning to `1756400000123` is the query parameter `WriteApi.write()`
  sends. Left at the default, every point lands in **January 1970** with no error
  anywhere. `storage.write()` passes `WritePrecision.MS` explicitly for this
  reason alone.
- **A failed write does not necessarily raise the library's own exception.**
  With `retries=False`, urllib3's exceptions reach the caller unwrapped:
  a stopped *container* gives `NameResolutionError` (compose removes it from the
  network's DNS, so the hostname fails before any connection is attempted), a
  stopped *process* on a reachable host gives `NewConnectionError`, and a paused
  container gives `ReadTimeoutError`. All three subclass
  `urllib3.exceptions.HTTPError`; the library's `ApiException` subclasses
  `InfluxDBError`. Hence `storage.WRITE_FAILURES` is a two-tuple — catching only
  `InfluxDBError` would miss every outage shape.
- **A burst collapses in storage, and the broker is blameless.** `--burst 200`
  published 200 messages in **7 milliseconds**; all 200 were delivered, consumed
  and written (queues drained, DLQ empty), and **7 points** survived — one per
  distinct millisecond, each holding the last `seq` written in that millisecond.
  Roughly 29 messages per millisecond overwriting one another.

  This is stage 5's reasoning arriving at its limit rather than being wrong: it
  chose milliseconds because "at second resolution a stage 9 burst would silently
  collapse", and that is exactly what milliseconds do to a *tight* burst too. At
  the 1 Hz steady state the margin is 1000x and it is a non-issue.

  **What matters is the diagnosis**, because the symptom is misleading: the
  dashboard shows a hole that looks like message loss, while the broker lost
  nothing. Stage 9's demonstrations use bursts, and stage 18 will ask where
  messages went. Check `count(distinct ts_ms)` against `count(seq)` before
  blaming the broker. Not scheduled for a fix — microsecond precision would
  change the frozen payload contract and the firmware with it.
- **`influx delete --predicate` removes the points but the tag value lingers**
  in the series index until compaction, so `schema.tagValues` keeps listing a
  deleted device. Query for actual points to confirm a deletion, not the tag list.

## Check first at stage 18

**Stage 18 is the first stage that needs both halves**, and it is the first one
on this track that is not a build stage at all: nothing new is written, the
existing pipeline is broken deliberately and watched. What to have in hand before
starting:

- **The dashboard is the instrument.** `http://localhost:3000/d/telemetry-live`,
  15 minutes at 5 s. `custom.insertNulls: 3000` draws a gap wider than 3 s as a
  break rather than a line across — **verified at stage 13** against a 10.2 s hole
  left by a `down`/`up`. So a break on a panel is a real outage rather than a
  rendering artefact, and the absence of one is meaningful too. Screenshotting it
  without the renderer plugin is a two-step dance with a session cookie — the
  recipe is in the stage 13 section.
- **`seq` is a field, so it is selectable.** `SELECT count("seq") FROM readings
  WHERE $timeFilter GROUP BY "device"` is the gap-and-duplicate evidence. A panel
  for it was deliberately *not* added at stage 13 — the DoD asked for two panels
  and the plan's rule is that improvements belonging to a later stage get
  recorded, not implemented. This is that record.
- **`ts_ms` is millisecond precision and point identity is measurement + tags +
  timestamp.** Two messages inside one millisecond overwrite, so the dashboard
  can show a hole the broker never caused. Check `count(distinct ts_ms)` against
  `count(seq)` before blaming the broker — see "Environment facts learned the
  hard way" above.
- **The `device` tag index holds three permanent stage 11 artefacts** —
  `burst-probe`, `int-probe`, `overwrite-probe` — with no recent points. They stay
  out of any short-range query for free, but any `SHOW TAG VALUES` listing shows
  five devices, not two.
- **Two devices publish at 1 Hz** with independent `seq` counters starting at 1,
  and the ESP32's loop drifts (1 s plus execution time). Anything that assumes the
  two are aligned is wrong.
- **`docker compose build` is only needed when `pyproject.toml` changes.** Stages
  12 and 13 needed none — but `up -d <service>` silently reuses an old image, so
  check whether anything Python changed.

## Open questions

**All stage 13 questions are answered.** One live choice was put to the user and
decided in conversation: the refresh cadence and time range (5 s / `now-15m`,
chosen because 5 s is Grafana's own `min_refresh_interval` floor, so
`compose.yaml` stays untouched and the stage remains pure provisioning).

**Four stage 13 details were decided without consultation** and are flagged as
such, following the precedent stages 9 and 11 set:

- **`GROUP BY "device"` with no template variable.** A variable would list the
  three permanent stage 11 artefacts; a short time range excludes them for free.
- **Raw points, no `GROUP BY time()`.** Bucketing averages a duplicate away and
  interpolates across a gap, which stages 9 and 18 need to see.
- **`custom.insertNulls: 3000`**, three times the publish interval, so an outage
  draws as a break. Observed working against a 10.2 s hole.
- **`"editable": false`.** Flipping it to `true` is the one-line way to allow
  scratch edits during a stage 18 investigation; they still cannot be saved.

**Stage 13 closed five things that had been carried as unknown or unstated:**
Grafana's `min_refresh_interval` floor of 5 s, that `insertNulls` exists in 13.2.2
with a millisecond threshold and really does break the line, that `$VAR`
substitution does **not** reach a dashboard JSON although it reaches the
datasource YAML, that `meta.canSave` stays `true` on a provisioned dashboard while
the save is refused with HTTP 400, and how to screenshot a dashboard with no
renderer plugin — the session cookie has to be injected over CDP, because
credentials in the URL land on the login page.

**All stage 12 questions are answered**, and all four live choices were decided
in conversation rather than unilaterally: the image tag, whether to declare the
DBRP mapping explicitly given that a virtual one already worked, whether Grafana
gets a state volume, and the datasource's authentication shape. Each is recorded
with its reasoning in "Visualization (stage 12)" above.

**Stage 12 closed three things that had been carried as unknown:** the Grafana
image tag and its healthcheck binary (`grafana/grafana:13.2`, and **both** `curl`
and `wget` are present), whether an explicit DBRP mapping has to exist at all (it
does not — a virtual one is synthesised), and what `list --json` reports for each
kind (`"virtual": true` / `false`, which is what makes the declarer idempotent).

**Nothing from stage 12 was decided without consultation.**

**All stage 11 questions are answered**, and the five live choices were decided
in conversation rather than unilaterally: the schema (measurement name, and
`seq` as a field), whether the write blocks the callback, the storage-failure
policy, duplicate handling, and the time budget. Each is recorded with its
reasoning in "Storage writes (stage 11)" above.

**Three stage 11 details were decided without consultation** and are flagged as
such, following stage 9's precedent:

- **The `INFLUXDB_*` config fields carry defaults rather than being required.**
  Forced by a recorded stage 10 decision — a required field would stop three
  modules that never touch storage from starting at all.
- **The shutdown flag moved to module scope in `amqp.py`.** A handler needs to
  see it and is built before `run_consumer()` is called. `consumer_observe`
  shares the module and is unaffected.
- **`EXIT_UNCONFIGURED = 5`**, following `topology.py`'s `EXIT_MISMATCH = 4`.

**Stage 11 closed three things that had been carried as unknown:** whether
`parse()`'s integer tolerance survives InfluxDB's typed fields (it does not —
HTTP 422, so `to_point()` coerces), where the heartbeat threshold sits relative
to a blocking write (explicit at 60 s now, and 4.5 minutes of 7.5 s stalls did
not trouble it), and whether a redelivery needs deduplicating (it does not —
three writes of one identity give one point).

**All stage 10 questions are answered**, and all four were decided in
conversation rather than unilaterally: the org/bucket/prefix naming, the
retention value, whether `config.py` grows fields at 10 or 11, and the token
model. Each is recorded with its reasoning in "Storage (stage 10)" above.

**All stage 6-9 questions are answered.** Stage 9's three were decided without
consultation and are flagged as such in "Overflow (stage 9)" above — the cap
value, how the overflow mode is switched, and what "steady state" meant. The
third is the one worth revisiting.

**All stage 6, 7 and 8 questions are answered.** Stage 8: the TTL value, and
how to demonstrate the head-of-queue rule.

**Stage 8 closed two things that had been carried as unknown:** whether
`min(queue TTL, per-message TTL)` holds (it does, both ways) and whether a TTL
touches unacknowledged messages (it does not).

**All stage 6 and stage 7 questions are answered.** Stage 6: ack mode, prefetch
value, ack-delay control, malformed-JSON handling, loop shape, reconnect. Stage
7: reject vs nack, how to reproduce the redelivery loop, what counts as poison,
where the contract lives, how strict the type check is. None is open.

**Raised at stage 7, deliberately not acted on:**

- **Poison-message *counting* is not implemented and no stage asks for it.**
  Bounded retry — redeliver a message N times before dead-lettering — is a real
  production pattern, and `--requeue-poison` deliberately has no cap because
  "nothing stops it on its own" is the lesson. If it is ever wanted, the hook is
  `method.redelivered` plus a per-delivery-tag counter, and it belongs nowhere
  in the current plan.
- **Nothing consumes `telemetry.dlq`, by design.** Its contents are the
  evidence. Stages 8 and 9 add to it; nothing drains it but `purge_queue`.
- **`payload.parse()` is called once per message and rebuilds nothing.** If the
  1 Hz rate ever rises far enough for validation cost to matter, the check table
  is a dict of small functions and is the obvious thing to look at. Not a
  concern at 1–2 Hz.

**Raised at stage 6, deliberately not acted on:**

- **Heartbeat interval — CLOSED at stage 11.** Now explicit at
  `HEARTBEAT_SECONDS = 60` in `amqp.connect()`. Kept here only to record that
  the gap existed from stage 6 to stage 11.
- **`run_consumer()` takes no "drain and exit" mode.** Not needed by any stage
  so far; noted because stage 18 might want one.

**Carried forward from earlier stages:**

- **Publisher `clean_start` and session expiry interval** — whether the publisher
  uses a clean session, and whether persistent-session behaviour is reachable, is
  undecided. Harmless for stage 6. It is the mechanism behind the stage 18
  duplicate demonstration, so **do not let it disappear.**
- **Millisecond timestamps are not enough for a tight burst** — 200 messages in
  7 ms collapse to 7 points. Recorded under "Environment facts"; **not
  scheduled**, because microsecond precision would change the frozen payload
  contract and the firmware with it. It is a diagnosis to remember, not a bug to
  fix.
- **A permanent write failure retries and requeues forever.** A 401 or a 422 is
  treated exactly like an outage. Deliberate — the uniform path is simpler and
  the backoff paces it — but nothing ever escalates. Bounded retry with a
  classification of permanent-vs-transient is the hook if it is ever wanted, and
  it belongs nowhere in the current plan.
- **Grafana image tag — CLOSED at stage 12.** `grafana/grafana:13.2`, floating
  across patches like `influxdb:2.9` and `rabbitmq:4.3-management`. Both `curl`
  and `wget` are in the image; `curl` is used, to match `influxdb`'s check. Kept
  here only to record that the gap existed from stage 10 to stage 12.
- **Optional hardening, not scheduled — now in three places.** `iot` carries the
  RabbitMQ `administrator` tag; `consumer_store` holds the InfluxDB **admin**
  token rather than a bucket-scoped write token; and from stage 12 **Grafana
  holds that same admin token** to do nothing but read. All three were raised and
  deferred deliberately; see "One admin token" above for why a scoped token
  cannot simply be pre-seeded the way the admin one can. Grafana's is the
  easiest of the three to narrow if it is ever wanted — a read-scoped token
  created by one CLI call, dropped into `.env`, no code touched — and it is also
  the widest gap, since a read-only client is holding a credential that can
  write and delete.
