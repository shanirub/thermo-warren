# ESP32-C3 → RabbitMQ → InfluxDB → Grafana

Learning project. The target is message broker mechanics — exchanges, queues,
bindings, acknowledgment, dead-lettering. The staged plan is the working document.

Current stage: **4 — topology declared in code as a one-shot job.**
`topology.py` is real. The publisher and both consumers are still stubs that
resolve configuration and exit.

## Setup

```bash
cp .env.example .env      # then edit the password
uv lock                   # pyproject changed at stage 4 (pika, pytest)
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
| `src/telemetry/consumer_observe.py` | observation path: bounded, lossy, no DLX | 6 |
| `src/telemetry/consumer_store.py` | durable path: manual ack, DLX, writes to InfluxDB | 6, 11 |
| `rabbitmq/rabbitmq.conf` | broker config; unknown keys abort startup | 3 |
| `rabbitmq/enabled_plugins` | management + MQTT; Erlang syntax, trailing period | 3 |
| `grafana/` | provisioned datasource and dashboard | 12 |
| `firmware/` | ESP-IDF project | 14 |
