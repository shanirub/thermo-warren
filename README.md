# ESP32-C3 → RabbitMQ → InfluxDB → Grafana

Learning project. The target is message broker mechanics — exchanges, queues,
bindings, acknowledgment, dead-lettering. The staged plan is the working document.

Current stage: **3 — broker up, plugins enabled, anonymous access disabled.**
The Python modules are still stubs. Every module is a
stub that resolves configuration and exits.

## Setup

```bash
cp .env.example .env      # then edit the password
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
| `src/telemetry/config.py` | single resolution point for hosts, ports, credentials | 2 |
| `src/telemetry/topology.py` | one-shot declarer; must complete before any publish | 4 |
| `src/telemetry/publisher.py` | software publisher standing in for the MCU | 5 |
| `src/telemetry/consumer_observe.py` | observation path: bounded, lossy, no DLX | 6 |
| `src/telemetry/consumer_store.py` | durable path: manual ack, DLX, writes to InfluxDB | 6, 11 |
| `rabbitmq/rabbitmq.conf` | broker config; unknown keys abort startup | 3 |
| `rabbitmq/enabled_plugins` | management + MQTT; Erlang syntax, trailing period | 3 |
| `grafana/` | provisioned datasource and dashboard | 12 |
| `firmware/` | ESP-IDF project | 14 |
