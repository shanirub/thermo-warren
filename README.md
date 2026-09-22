# ESP32-C3 → RabbitMQ → InfluxDB → Grafana

Learning project. The target is message broker mechanics — exchanges, queues,
bindings, acknowledgment, dead-lettering. The staged plan is the working document.

## Project Status

| Track | Stage | Status |
| --- | --- | --- |
| Software | 8 | Both dead-letter triggers verified: rejection (consumer-driven) and expiry (a 30 s TTL the broker enforces with no consumer at all) |
| Hardware | 17 | MQTT 5 publisher, SNTP, outage policy and OLED link icon, all verified on hardware — the MCU now feeds both queues |

The two tracks ran in parallel and met at the payload contract. Hardware work
lives in `firmware/` — see `firmware/README.md` for its build/flash/verification
steps, including the two one-time steps a new board needs before it will join the
network.

**That meeting has happened:** the MCU publishes to both queues and its payload
matches the contract exactly. What remains for stage 17's Definition of Done — a
dashboard showing real room temperature — is blocked on the software track, which
has to reach stage 13 first. Stage 18 needs both halves and so is the point where
the tracks stop being independent.

## Setup

```bash
cp .env.example .env      # then edit the password
uv lock                   # pyproject changed at stage 5 (paho-mqtt)
uv sync                   # creates .venv/ and installs the project editable
```

## Run modes

Both modes execute the same command against the same code. Only the resolved
configuration differs.

**Host mode** — modules run directly, against ports published by compose.
Run from the repository root; `.env` is resolved relative to the working directory.

```bash
uv run python -m telemetry.publisher
```

**Container mode** — the same module inside the compose network.

```bash
docker compose run --rm publisher
```

Configuration precedence is: process environment → `.env` → field default.
Compose loads `.env` wholesale and then overrides the hostnames per service,
so `.env` holds only the host-mode values.

## Stage 8 verification

`telemetry.store` gains `x-message-ttl: 30000`. That is a queue-argument change,
so the broker refuses a plain redeclare and **the stack will not come up cleanly
until you run `--recreate`**.

### See the 406 first

This error path was written at stage 4 and this is the first argument mismatch
that ever triggered it. Worth watching once:

```bash
uv run python -m telemetry.topology ; echo "exit=$?"
```

Expect **exit 4** and RabbitMQ's own wording quoted back:

```
PRECONDITION_FAILED - inequivalent arg 'x-message-ttl' for queue
'telemetry.store' in vhost '/': received the value '30000' of type
'signedint' but current is none
```

`docker compose up -d --wait` fails the same way, with
`service "topology" didn't complete successfully: exit 4`. That is the stage 4
gate working, not a broken stack.

```bash
uv run python -m telemetry.topology --recreate
```

Both consumers log a broker `Basic.Cancel` and reattach on their own.

### The Definition of Done

Purge `telemetry.dlq` first — `--recreate` deliberately spares it, so stage 7's
`rejected` deaths would otherwise mix with stage 8's `expired` ones.

```bash
docker compose stop consumer-store        # leave the publisher running
docker compose exec rabbitmq rabbitmqctl list_queues \
    name messages_ready consumers
```

`telemetry.store` climbs and then **plateaus at roughly publish rate x 30 s**
while `telemetry.dlq` grows linearly, with `consumers=0` the whole time.
Measured here with both publishers running (~2/s): store held at **59-61**, DLQ
grew ~2/s for 108 s. Nothing consumed anything — the broker did this alone.

Then read the header, as at stage 7:

```bash
# .env is read by Compose, not by your shell, so $RABBITMQ_USER is empty here.
# Source it in a SUBSHELL -- never into the working shell, or the process
# environment will outrank .env for every Python module you run afterwards.
( set -a; . ./.env; set +a
  curl -su "$RABBITMQ_USER:$RABBITMQ_PASSWORD" \
    -H 'content-type: application/json' \
    -d '{"count":1,"ackmode":"ack_requeue_true","encoding":"auto"}' \
    "http://localhost:$RABBITMQ_MANAGEMENT_PORT/api/queues/%2F/telemetry.dlq/get"
) | python -m json.tool
```

`reason: expired`, same queue, same header shape as stage 7 — and the payload is
a **perfectly valid message**. Nothing was wrong with it; it only waited too
long. The DLQ now holds two populations you can only tell apart by that field.

### Per-message TTL and the head of the queue

`--message-expiry SECONDS` sets MQTT 5's Message Expiry Interval, which
RabbitMQ turns into a per-message TTL. Confirm it is really being set before
drawing conclusions — it shows up as the AMQP `expiration` property:

```bash
uv run python -m telemetry.publisher --burst 3 --message-expiry 2
# then GET from telemetry.store: sim-01 rows show expiration=2000,
# MCU rows show none
```

**The trap.** With the MCU publishing at 1 Hz and `docker compose run` taking
~3 s to start a container, MCU messages always reach the queue head first. They
carry the 30 s queue TTL, so they block the head and your 2-second messages
linger behind them — which looks *identical* to the expiry property never being
applied. Publish from an already-connected process when queue order matters.

Measured both ways:

| Setup | Result |
|---|---|
| 2 s expiry, at the head | died at **exactly 2.0 s** |
| 2 s expiry, behind 30 s messages | survived **~30 s**, 15x its own deadline |
| 60 s expiry, at the head, 30 s queue TTL | died at **30 s** |

The last row is the `min(queue, per-message)` rule: the lower always wins.

And the rule this demonstrates is *not* that the two TTL kinds use different
machinery. Both expire lazily from the head. A uniform queue-level TTL plus FIFO
means the head is always both the oldest and the earliest to expire, so
head-first expiry is always correct. Non-uniform per-message TTLs break that
correspondence, and only then can a message outlive its deadline.

### Unacknowledged messages are out of reach

```bash
docker compose stop consumer-store
uv run python -m telemetry.consumer_store --ack-delay 35   # above the 30s TTL
```

`messages_unacknowledged` holds at **10** indefinitely — measured at 88 s,
nearly three TTLs — while ready messages expire into the DLQ around them.
Delivery takes a message out of the queue's expiry reach, which means a consumer
holding messages through an outage will not lose them to the TTL. That removes
one failure mode from stage 11's design space.

```bash
docker compose start consumer-store publisher
```

## Stage 7 verification

No rebuild and no topology change — the dead-letter side has been declared since
stage 4 and inert until now. `./src` is bind-mounted, but a running container
holds the old code, so **restart the consumers** after pulling this stage.

```bash
docker compose up -d --wait
docker compose restart consumer-observe consumer-store
```

Purge all three queues, then stop the publisher so a one-off run does not
collide with it over the same MQTT client ID.

```bash
docker compose stop publisher
```

### The Definition of Done

```bash
docker compose run --rm publisher python -m telemetry.publisher \
    --corrupt-every 5 --burst 40
```

Eight of the forty are corrupted. `telemetry.dlq` should gain exactly eight
while `telemetry.store` and `telemetry.observe` drain to zero, and the `seq`
missing from `consumer-store`'s log should be exactly 5, 10, 15 … 40.

```bash
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready
docker compose logs consumer-store | grep poison
```

Each line names the failure class — `malformed JSON` or `off-contract` — because
`x-death` cannot. `consumer-observe` logs the same eight and dead-letters none:
same bytes, two fates, because that queue has no dead-letter exchange.

### The x-death header

`rabbitmqctl` cannot show message headers, so use the management HTTP API. This
consumes nothing — `ack_requeue_true` puts the message back.

```bash
# .env is read by Compose, not by your shell, so $RABBITMQ_USER is empty here.
# Source it in a SUBSHELL -- never into the working shell, or the process
# environment will outrank .env for every Python module you run afterwards.
( set -a; . ./.env; set +a
  curl -su "$RABBITMQ_USER:$RABBITMQ_PASSWORD" \
    -H 'content-type: application/json' \
    -d '{"count":1,"ackmode":"ack_requeue_true","encoding":"auto"}' \
    "http://localhost:$RABBITMQ_MANAGEMENT_PORT/api/queues/%2F/telemetry.dlq/get"
) | python -m json.tool
```

Expect `reason: rejected`, `queue: telemetry.store`, `count: 1`, and
`routing-keys: ["sensors.esp32c3.telemetry"]` — the original key survived,
because `x-dead-letter-routing-key` is deliberately unset. The payload is
visibly truncated. Note `exchange` is `amq.topic`, the *original* exchange, not
the DLX.

### The infinite redelivery loop, on purpose

Put exactly one poison message in the queue, then run the consumer with the
flag that requeues instead of dead-lettering:

```bash
docker compose stop consumer-store
docker compose run --rm publisher python -m telemetry.publisher \
    --corrupt-every 1 --burst 1
timeout -s INT 5 uv run python -m telemetry.consumer_store --requeue-poison
```

Five seconds is plenty. Measured here: **58,733 redeliveries of one message**,
73% of a core, `redelivered=True` on every one, and delivery tags climbing past
58,000.

**`telemetry.dlq` stays empty the whole time, and that is the point.** `x-death`
is written only on an actual dead-letter, so a requeue loop leaves no header, no
DLQ entry and nothing at all to find afterwards — just a hot core and a
repeating log line. Restart the consumer without the flag and the same message
dead-letters immediately, with `count: 1`, because `x-death` counts deaths, not
deliveries.

```bash
docker compose start consumer-store publisher
```

## Stage 6 verification

Both consumers are long-lived from this stage, so the ordinary `up` is the whole
setup. No rebuild is needed -- `pika` went in at stage 4 and all four Python
services share one image, with `./src` bind-mounted.

```bash
docker compose up -d --wait     # works from stage 6; at stage 5 it falsely failed
```

Expected steady state: `rabbitmq` healthy and `publisher`, `consumer-observe`
and `consumer-store` all Up. Only `topology` shows `Exited (0)`.

**Purge first if the MCU has been publishing**, or every count below is wrong.
Note `purge_queue` is per queue and not atomic across them: a message published
between the two purges survives in one queue and not the other, which looks
exactly like a fan-out failure.

```bash
for q in telemetry.store telemetry.observe telemetry.dlq; do
  docker compose exec rabbitmq rabbitmqctl purge_queue $q
done
```

### Both consumers see the same messages

```bash
docker compose logs consumer-observe | grep -o 'seq=[0-9]*' | sort -u
docker compose logs consumer-store   | grep -o 'seq=[0-9]*' | sort -u
```

Compare the **interiors**, not the raw sets -- the ends differ purely by which
consumer attached first. If the MCU is powered there are two publishers on one
routing key, so you will see two unrelated `seq` series interleaved: the
simulator from 1, the MCU in the tens of thousands. Split them by magnitude.

### Draining is not the test: prefetch and the unclean kill

```bash
docker compose stop consumer-store
uv run python -m telemetry.consumer_store --ack-delay 2      # host mode
```

Watch the unacknowledged count:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues \
    name messages_ready messages_unacknowledged
```

`telemetry.store` should climb to **exactly 10** unacknowledged and hold there
while `messages_ready` grows -- that is `basic_qos(prefetch_count=10)` bounding
the window.

> **Changed at stage 8.** `telemetry.store` now carries a 30 s TTL, so the
> *ready* messages this procedure accumulates start dead-lettering once they
> age past it. That is the TTL working, not a regression. The unacknowledged 10
> are unaffected -- a TTL never applies to a delivered message. `telemetry.observe` stays at **0** unacknowledged throughout, which
is not a bug: `basic_qos` is ignored on an automatic-acknowledgment channel.

Now kill it uncleanly (`kill -9` the host-mode process, or
`docker kill -s SIGKILL` the container). Those 10 **must reappear as ready**. If
they vanish, automatic acknowledgment is on somewhere it should not be.

### Fan-out independence

```bash
docker compose stop consumer-observe
```

`telemetry.store` keeps draining at 0. `telemetry.observe` grows to **100** and
then holds, silently dropping its head at `x-max-length`. The two paths are
genuinely independent.

### Reconnect, and a deleted queue

```bash
docker compose restart rabbitmq
```

Both consumers log `CONNECTION_FORCED (320)`, back off, and resume in a few
seconds with no operator action. Then, with both attached:

```bash
uv run python -m telemetry.topology --recreate
```

Each consumer must log an explicit `broker cancelled our consumer ... (queue
deleted?)` line carrying the `Basic.Cancel` frame, then reattach. That line is
the reason this project uses `basic_consume` rather than pika's `consume()`
generator, which would end silently instead.

## Stage 5 verification

```bash
docker compose build                                 # pyproject changed (paho-mqtt);
                                                       # `up` alone reuses the stale image
docker compose up -d rabbitmq
docker compose ps                                    # wait for (healthy)
docker compose run --rm topology
```

### Steady publishing

```bash
docker compose up -d publisher
docker compose logs -f publisher                      # ~1 line/second, seq incrementing
```

THE DEFINITION OF DONE. With no consumers running, both queues can only grow.
**From stage 6 the consumers run by default and drain them**, so to reproduce
this check you must stop them first:

```bash
docker compose stop consumer-observe consumer-store
```

Then check within the first minute or so of `publisher` starting:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues name messages
# wait a few seconds
docker compose exec rabbitmq rabbitmqctl list_queues name messages
```

`telemetry.store` and `telemetry.observe` should show the same depth each
time, and that depth should have grown by roughly the number of seconds you
waited. If they diverge before message 100, the fan-out from stage 4 broke; if
neither grows, the publisher isn't connected -- check `docker compose logs
publisher`.

Leave it running longer and the lockstep ends on purpose:
`telemetry.observe` was declared in stage 4 with `x-max-length: 100`, so once
it fills it holds steady at 100 (`x-overflow: drop-head` evicts the oldest to
make room for the newest) while `telemetry.store` keeps growing unbounded.
That divergence is the stage 9 lesson arriving early, not a bug -- lockstep
growth is only the invariant up to the cap.

Then confirm the payload and delivery mode in the management UI at
<http://localhost:15672>: **Queues → telemetry.store → Get Message(s)**. Set
"Ack Mode" to a requeue option so inspecting a message doesn't consume it, then
check:

- **Payload** is well-formed JSON with exactly the five contract fields:
  `seq`, `device`, `temp_c`, `humidity_pct`, `ts_ms`.
- **Properties → delivery_mode** shows **`2` (persistent)** -- this is the
  proof that QoS 1 mapped to a persistent AMQP message, not just a claim in a
  comment.

Stop the long-lived publisher before the next two experiments, so they don't
collide with it over the same MQTT client ID:

```bash
docker compose stop publisher
```

### Burst mode

Ignores the rate setting; publishes as fast as possible, waits for every
PUBACK, then exits.

```bash
docker compose run --rm publisher python -m telemetry.publisher --burst 20
```

Check the depths again. `telemetry.store` should have grown by exactly 20.
`telemetry.observe` grows by 20 too *only if* it was still under its 100-cap
when the burst ran; if the steady-publishing step above already filled it,
these 20 just evict the 20 oldest and it holds at 100 -- same cap behaviour as
above, not a sign the burst under-delivered.

### Corruption mode

Combined with `--burst` here so the run is bounded rather than needing a
manual Ctrl-C; `--corrupt-every` alone is meant for a long steady run once
stage 7 exists to observe what happens to a malformed message.

```bash
docker compose run --rm publisher \
  python -m telemetry.publisher --burst 20 --corrupt-every 5
```

`docker compose logs publisher` (or the run's own stdout) should show a
`WARNING ... emitting malformed payload: seq=5` line for `seq=5, 10, 15, 20`
and nothing else unusual for the rest. RabbitMQ does not parse the payload, so
these land in both queues exactly like any other message -- the malformed
bytes only become a problem for stage 7's consumer.

Bring the long-lived publisher back if you want it running:

```bash
docker compose up -d publisher
```

Purge both queues afterwards, from the management UI or with
`rabbitmqctl purge_queue`, so stage 6 starts from a known state:

```bash
docker compose exec rabbitmq rabbitmqctl purge_queue telemetry.store
docker compose exec rabbitmq rabbitmqctl purge_queue telemetry.observe
```

### Host mode

```bash
uv run python -m telemetry.publisher
uv run python -m telemetry.publisher --burst 20
uv run python -m telemetry.publisher --burst 20 --corrupt-every 5
```

### Tests

```bash
uv run pytest
```

No broker needed. `build_payload`, `corrupt`, `should_corrupt` and
`DriftSimulator` are pure functions, exercised the same way `declare()` is
tested against a mock channel in stage 4 -- against no broker at all.


## Stage 4 verification

```bash
docker compose up -d rabbitmq
docker compose ps                                    # wait for (healthy)

# 1. The job runs and exits 0.
docker compose run --rm topology
echo $?                                              # expect 0

# 2. It is idempotent -- a second run changes nothing and still exits 0.
docker compose run --rm topology
echo $?                                              # expect 0

# 3. The bindings exist. Both queues, same routing key, one exchange.
docker compose exec rabbitmq rabbitmqctl list_bindings
docker compose exec rabbitmq rabbitmqctl list_queues name arguments

# 4. THE FAN-OUT PROOF. Publish exactly one MQTT message...
docker run --rm --network thermo-warren_default --env-file .env eclipse-mosquitto \
  sh -c 'mosquitto_pub -h rabbitmq -t sensors/esp32c3/telemetry \
         -m "{\"seq\":1}" -q 1 -u "$RABBITMQ_USER" -P "$RABBITMQ_PASSWORD"'

# ...and confirm BOTH queues show a depth of one.
docker compose exec rabbitmq rabbitmqctl list_queues name messages
```

If only one queue received it, the second binding is wrong — and note that
nothing reported an error. That silence is the lesson.

Note `--env-file .env` above rather than `$RABBITMQ_PASSWORD` in your own shell:
`.env` is read by Compose, not by your shell, so the variable would expand to
empty. Let the container's shell expand it instead.

Purge both queues afterwards, from the management UI or with
`rabbitmqctl purge_queue`.

### Destructive redeclare

Stages 8 and 9 change queue arguments, and RabbitMQ answers a declare with
different arguments with `PRECONDITION_FAILED` (406) rather than updating the
queue. The job reports that error, quotes what the broker said, and exits 4.
The remedy is explicit and never runs as part of `docker compose up`:

```bash
docker compose run --rm topology python -m telemetry.topology --recreate
```

It deletes `telemetry.store` and `telemetry.observe` only. `telemetry.dlq` is
left standing, because its contents are the evidence you collected.

### Host mode

```bash
uv run python -m telemetry.topology
uv run python -m telemetry.topology --recreate
```

No `depends_on` gate exists here — nothing stops you publishing before the
topology exists. That is also the easiest way to reproduce the silent discard
on purpose.

### Tests

```bash
uv run pytest
```

No broker needed. `declare()` takes a channel, so a mock records exactly which
arguments were passed — and the arguments dicts are the policy.

## Stage 3 verification

```bash
docker compose up -d rabbitmq
docker compose ps                                    # wait for (healthy)

# Both plugins enabled -- proves enabled_plugins was not silently ignored
docker compose exec rabbitmq rabbitmq-plugins list -e

# The dedicated user exists; guest should not
docker compose exec rabbitmq rabbitmqctl list_users

# Anonymous MQTT is refused (no -u/-P). Expect a connection failure.
docker run --rm --network thermo-warren_default eclipse-mosquitto \
  mosquitto_pub -h rabbitmq -t test -m hi

# ...and succeeds with credentials
docker run --rm --network thermo-warren_default eclipse-mosquitto \
  mosquitto_pub -h rabbitmq -t test -m hi -u iot -P "$RABBITMQ_PASSWORD"
```

Management UI: <http://localhost:15672>, using the credentials from `.env`.

Note the second publish succeeds but the message goes nowhere: `amq.topic` has
no bindings until stage 4, and a topic exchange with no matching binding
discards silently.

## Stage 2 verification (kept for reference)

```bash
# 1. The image builds.
docker compose build

# 2. The same module resolves a different broker hostname in each mode.
uv run python -m telemetry.topology            # rabbitmq_host = localhost
docker compose run --rm topology               # rabbitmq_host = rabbitmq

# 3. The secrets file is ignored.
git check-ignore -v .env
```

Nothing connects to a broker yet — RabbitMQ arrives at stage 3.

## Layout

| Path | Purpose | Stage |
|---|---|---|
| `src/telemetry/config.py` | what differs between run modes: endpoints, credentials, this process's behaviour | 2 |
| `src/telemetry/topology_spec.py` | what the broker enforces: names and queue arguments, identical in every mode | 4 |
| `src/telemetry/logging_setup.py` | one logging configuration, shared; pins pika's logger | 4 |
| `src/telemetry/topology.py` | one-shot declarer; must complete before any publish | 4 |
| `tests/` | unit tests over the declared topology; no broker required | 4 |
| `src/telemetry/publisher.py` | software publisher standing in for the MCU | 5 |
| `src/telemetry/amqp.py` | shared AMQP plumbing: connect with retry, and the reconnecting consumer runner | 6 |
| `src/telemetry/payload.py` | the frozen payload contract: field names, types, and the validating parse() | 7 |
| `src/telemetry/consumer_observe.py` | observation path: bounded, lossy, no DLX | 6 |
| `src/telemetry/consumer_store.py` | durable path: manual ack, DLX, writes to InfluxDB | 6, 11 |
| `rabbitmq/rabbitmq.conf` | broker config; unknown keys abort startup | 3 |
| `rabbitmq/enabled_plugins` | management + MQTT; Erlang syntax, trailing period | 3 |
| `grafana/` | provisioned datasource and dashboard | 12 |
| `firmware/` | ESP-IDF project (see `firmware/README.md`) | 14–17 |
