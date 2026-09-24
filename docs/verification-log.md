# Verification log

How each stage was checked against real behaviour: the commands, in the order
they were run, newest stage first. Moved out of `README.md` at stage 13, which is
when an append-only log had grown to ten times the length of the page it was
appended to.

**Three documents, three jobs.** `mcu-rabbitmq-staged-plan.md` holds each stage's
Definition of Done. The `CLAUDE.md` files hold what was actually verified, what it
proved and what was decided as a result — they are the decision record. **This
file holds only how to re-run it.**

Read a section together with that stage's entry in `src/telemetry/CLAUDE.md`. The
commands are as they were run at the time, and later stages changed things under
them: queue arguments were added and removed again at stages 8 and 9, storage
arrived at 10, and the steady state gained a second one-shot service at 12. A
section reproduces its own stage, not the current one.

**`.env` is read by Compose, not by your shell.** Where a command needs those
values, source it in a **subshell** — never into the working shell, or the
process environment will outrank `.env` for every Python module run afterwards.

---

## Stage 13 verification

A dashboard provisioned from a file: `grafana/provisioning/dashboards/`, a
provider YAML and one dashboard JSON. Nothing Python changed, so no rebuild.

The provider *config* is read at Grafana startup; the dashboard JSON is rescanned
on `updateIntervalSeconds`. So a new subdirectory needs a restart, and every
later edit to the dashboard does not.

```bash
docker compose restart grafana
docker compose logs grafana --since 60s | grep provisioning.dashboard
# -> "starting to provision dashboards" / "finished to provision dashboards"
#
# Two level=error lines about /etc/grafana/provisioning/plugins and /alerting
# are expected and harmless: the read-only bind mount replaces the WHOLE
# provisioning directory, so only the subdirectories in the repo exist.
```

**The dashboard is present without anyone opening the UI**, and Grafana says it
came from a file:

```bash
(set -a; . ./.env; set +a
 curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" \
   'http://localhost:3000/api/search?type=dash-db'
 curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" \
   http://localhost:3000/api/dashboards/uid/telemetry-live)
# -> uid telemetry-live, and meta.provisioned: true,
#    meta.provisionedExternalId: telemetry.json
#
# Note meta.canSave is ALSO true. That is about permissions and is not the
# provisioning guard -- see the save attempt below.
```

**Both panel queries return data**, through the same datasource proxy the panel
uses. Two frames per query, named by the target's `alias: $tag_device`:

```bash
(set -a; . ./.env; set +a
 now=$(date +%s%3N); from=$((now-900000))
 curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" -H 'Content-Type: application/json' \
   -X POST http://localhost:3000/api/ds/query -d "{
     \"from\":\"$from\",\"to\":\"$now\",
     \"queries\":[{\"refId\":\"A\",
       \"datasource\":{\"type\":\"influxdb\",\"uid\":\"influxdb-telemetry\"},
       \"rawQuery\":true,
       \"query\":\"SELECT \\\"temp_c\\\" FROM \\\"readings\\\" WHERE \$timeFilter GROUP BY \\\"device\\\"\",
       \"resultFormat\":\"time_series\",\"alias\":\"\$tag_device\",
       \"intervalMs\":5000,\"maxDataPoints\":1000}]}")
# -> frames named sim-01 and esp32c3-01, ~890 points each over 15 minutes.
#    TWO frames, not five: the three permanent stage 11 tag artefacts
#    (burst-probe, int-probe, overwrite-probe) have no recent points, so a
#    short time range excludes them with no filtering in the panel.
```

**The provisioned dashboard cannot be saved over.** `allowUiUpdates: false` is
enforced by Grafana, not merely declared:

```bash
(set -a; . ./.env; set +a
 body=$(curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" \
          http://localhost:3000/api/dashboards/uid/telemetry-live |
        python3 -c "import json,sys; d=json.load(sys.stdin)['dashboard']; d['title']='SHOULD NOT PERSIST'; print(json.dumps({'dashboard':d,'overwrite':True}))")
 curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" -H 'Content-Type: application/json' \
   -X POST http://localhost:3000/api/dashboards/db -d "$body")
# -> HTTP 400 {"message":"Cannot save provisioned dashboard"}, title unchanged
```

**The stage's own verify step — change the publisher's output range.** The source
is bind mounted, so a restart is enough and no rebuild is needed. `_clamp` applies
on the first tick, so the trace steps immediately, well inside one 5 s refresh:

```bash
sed -i 's/^TEMP_MIN_C, TEMP_MAX_C = 0, 50$/TEMP_MIN_C, TEMP_MAX_C = 40, 50/' \
  src/telemetry/publisher.py
docker compose restart publisher
# watch the Temperature panel: sim-01 steps from ~33 to the new floor of 40.

# then put it back, and watch it walk down again
sed -i 's/^TEMP_MIN_C, TEMP_MAX_C = 40, 50$/TEMP_MIN_C, TEMP_MAX_C = 0, 50/' \
  src/telemetry/publisher.py
docker compose restart publisher
git diff --stat src/telemetry/publisher.py    # must be empty
```

**Teardown proves provisioning rather than a stale volume** — the same check as
stage 12, and it proves more than it looks. Grafana has no state volume, so a
dashboard created by clicking dies here while the provisioned one comes back:

```bash
docker compose down            # NO -v: that would destroy the readings
docker volume ls --filter name=thermo-warren --format '{{.Name}}'
# -> three volumes, none of them Grafana's
docker compose up -d
# -> only telemetry-live is listed afterwards; a UI-created dashboard is gone
```

**Screenshotting the panels, with no image renderer plugin installed.** An API
cannot show that a panel draws. Credentials in the URL do not work — Chrome drops
them and Grafana's frontend wants a session — so the `grafana_session` cookie
from `POST /login` has to be injected over the DevTools protocol:

```bash
google-chrome --headless=new --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-profile about:blank &
# then, over CDP against the ws URL from http://127.0.0.1:9222/json/version:
#   Network.setCookie  grafana_session=<from POST /login>, domain localhost
#   Page.navigate      http://localhost:3000/d/telemetry-live?...&kiosk
#   sleep ~14s         -- panels draw well after the load event, so there is no
#                         load event to race
#   Page.captureScreenshot
```

The result is `src/telemetry/docs/stage13-dashboard.png`, and it settles one
thing an API could not: the 10.2 s hole the teardown left in `sim-01` renders as
a **break**, because `custom.insertNulls: 3000` disconnects points more than 3 s
apart. The 1 s normal interval either side stays connected. So a gap on a panel
is a real outage, not a drawing artefact.

```bash
uv run pytest -q     # 114/114, with no broker and no database running
```

---

## Stage 12 verification

Grafana reads the pipeline back. Everything it needs is in version control:
`grafana/provisioning/datasources/influxdb.yaml` for the datasource, and
`influxdb/dbrp.sh` for the DBRP mapping that exposes the bucket to InfluxQL.

Steady state now has **two** `Exited (0)` services, `topology` and `dbrp`, and
Grafana is at `http://localhost:3000`.

### The mapping did not have to be created

```bash
docker compose exec influxdb influx v1 dbrp list
```

Before stage 12 that printed `telemetry` under `VIRTUAL DBRP MAPPINGS
(READ-ONLY)` — **InfluxDB 2.x synthesises a read-only mapping for any bucket
that has no explicit one**, and InfluxQL already worked through it. The plan says
2.x "needs a mapping exposing the bucket under a v1-style database name", which
is true, but it did not need *creating*.

It is declared explicitly anyway, so the repo states the database name rather
than inheriting it from a 2.x convenience InfluxDB 3 will not carry. Verified
both ways: the virtual mapping vanishes from the listing once the explicit one
exists, and comes straight back if it is deleted.

### The Definition of Done

**Grafana starts with the datasource already present, and its connection test
passes.** Neither check involves opening the UI.

```bash
set -a; . ./.env; set +a

curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" \
  http://127.0.0.1:3000/api/datasources

curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" \
  http://127.0.0.1:3000/api/datasources/uid/influxdb-telemetry/health
```

One datasource, uid `influxdb-telemetry`, with `dbName` substituted to
`telemetry` from `$INFLUXDB_BUCKET` — which is what proves the provisioning file
was read rather than a datasource being present by some other route. The health
call returns:

```json
{"message":"datasource is working. 2 measurements found","status":"OK"}
```

Two measurements, not one: `readings`, and the `stage10_check` point written by
hand at stage 10, which is permanent because retention is infinite.

It also comes back `"readOnly": true` — a provisioned datasource cannot be edited
in the UI. That is correct, and it is why stage 13's dashboard references it by
uid instead of adjusting it.

### A bare query, through Grafana rather than around it

Querying InfluxDB directly was already known to work from stage 11. The point
here is the path through Grafana's proxy:

```bash
curl -s -u "$GRAFANA_USERNAME:$GRAFANA_PASSWORD" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:3000/api/ds/query -d '{
  "from":"now-5m","to":"now",
  "queries":[{"refId":"A","datasource":{"uid":"influxdb-telemetry","type":"influxdb"},
    "rawQuery":true,"resultFormat":"time_series",
    "query":"SELECT temp_c, humidity_pct, seq FROM readings WHERE device = '"'"'sim-01'"'"' AND $timeFilter ORDER BY time DESC LIMIT 3"}]}'
```

Three frames come back — `readings.temp_c`, `readings.humidity_pct`,
`readings.seq`. **`readings` is the measurement and `telemetry` is the
database**; the two words are deliberately different so a query never reads as
`FROM telemetry` in database `telemetry`.

Both devices are reachable, and with `device` the only tag a query that filters
neither will interleave them:

```
SELECT count(seq) FROM readings WHERE $timeFilter GROUP BY device
→ sim-01 13187, esp32c3-01 12848   (24 h)
```

**A trap for stage 13:** `SHOW TAG VALUES FROM readings WITH KEY = device` lists
**five** devices. Three of them — `burst-probe`, `int-probe`, `overwrite-probe` —
are stage 11 test artefacts with no recent points, and they are permanent,
because retention is infinite. A template variable built from that query will
show all five.

### Provisioning, not a stale volume

```bash
docker compose down             # NO -v
docker compose up -d --wait
docker volume ls --filter name=thermo-warren --format '{{.Name}}'
```

**Three volumes, none of them Grafana's.** Grafana is given no state volume
deliberately: its database lives in the container's writable layer, so `down`
destroys it and the next boot starts empty. That is what makes the check mean
something — with a named volume, a datasource surviving a restart would only
prove the volume survived. Distinguishing the two would otherwise need `down -v`,
which is all-or-nothing and would destroy the readings the dashboard exists to
show.

The cost, accepted: panel edits made in the UI and Explore history do not survive
`down`. They do survive `stop` and `restart`.

Re-run the two DoD calls above after the restart; both still pass.

### The one-shot is idempotent, and the check is not obvious

```bash
docker compose up -d            # again
docker compose logs dbrp
```

→ `dbrp: explicit mapping for database 'telemetry' already present`, and exactly
one explicit mapping, not two.

**`influx v1 dbrp list --json` includes virtual mappings**, each carrying
`"virtual": true`, while an explicit one carries `"virtual": false`. A check that
matched the database name alone would always find the virtual mapping and so
never create anything at all. `influxdb/dbrp.sh` greps for `"virtual": false`.

The other awkwardness: `influx v1 dbrp create` takes `--bucket-id`, not a bucket
name, so the id is looked up first — and `jq` is not in the InfluxDB image.

### A log line that tells the truth late

Polled across a `docker compose restart grafana`: the HTTP port is closed for
about 2 s (curl exits 000), the datasource answers `"status":"OK"` at about 3 s,
and Docker still reports the container `starting` until about 6 s, because
`start_period: 15s` and `interval: 10s` delay the first probe.

The healthcheck is conservative rather than wrong, but do not race it — a
`curl -s` into a JSON parser during that first window fails on an empty body,
which looks like a datasource fault and is not one.

## Stage 11 verification

The durable consumer writes to InfluxDB **between the parse and the
acknowledgment**. That ordering is the whole stage: acknowledging first loses the
message if the process dies in between, acknowledging after redelivers it, which
is what makes the pipeline at-least-once end to end.

The stage's real content is the failure test, not the happy path.

### The schema

One measurement, one tag, three fields.

```
readings,device=sim-01 temp_c=24.4,humidity_pct=62.5,seq=1234i 1756400000123
```

`seq` is a **field, not a tag**, deliberately. Tag values are indexed and every
distinct tag set is a series; `seq` is monotonic and unbounded, so tagging it
would create one series per message — about 86,400 a day at 1 Hz — against a
bucket whose retention is infinite. `device` is the only tag, which is what keeps
the simulator and the MCU in separate series with independent `seq` counters.

### The Definition of Done

```bash
docker compose build            # pyproject.toml changed -- `up -d` will NOT do this
docker compose up -d --wait

docker compose logs consumer-store --tail 5      # "stored: seq=N"

docker compose exec influxdb influx query '
from(bucket: "telemetry") |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "readings")
  |> last()'
```

Check the `_time` column reads as **now, to the millisecond**. If every point
landed in January 1970, the write precision was left at the library default —
see below.

Then compare what the consumer says it stored against what is actually stored:

```bash
docker compose logs consumer-store --since 150s | grep -oE 'stored: seq=[0-9]+'
```

Measured over a 150 s window: 119 logged, 119 stored, sets identical, no gaps.

### The failure test — stop the storage container

```bash
docker compose stop influxdb
# watch for ~45s, then:
docker compose exec rabbitmq rabbitmqctl list_queues \
  name messages_ready messages_unacknowledged
```

Expected, and measured:

| Queue | State during the outage |
| --- | --- |
| `telemetry.store` | ready climbing (486 at the sample), unacked pinned at **10** |
| `telemetry.dlq` | **0** — nothing is dead-lettered |
| `telemetry.observe` | **unaffected**, still draining |

That last row is the practical payoff of the fan-out: the fragile path is
isolated from the observation path.

The consumer log shows three attempts at 0.5 / 1 / 2 s, then a requeue:

```
WARNING storage write failed (attempt 1 of 3) for seq=143: NameResolutionError: ...
WARNING storage write failed (attempt 2 of 3) for seq=143: NameResolutionError: ...
WARNING storage write failed (attempt 3 of 3) for seq=143: NameResolutionError: ...
ERROR   storage unavailable, requeueing: seq=143 delivery_tag=181
```

**Requeue, never dead-letter.** `telemetry.dlq` means one thing — this message
failed the payload contract — and a good reading that happened to arrive during
an outage is not poison. `x-death` records the *broker's* reason and has no room
for ours, so the two would be indistinguishable once mixed.

The final 2 s sleep, after the *last* failed attempt, is what paces the requeue.
Without it the message returns immediately and a storage outage becomes the same
hot redelivery loop `--requeue-poison` exists to demonstrate.

### Restart, and confirm nothing was lost

```bash
docker compose start influxdb
```

Queues drain to zero, `telemetry.dlq` is still empty, and a per-device gap
analysis comes back clean — zero gaps and zero duplicate `seq`, for both
`sim-01` and `esp32c3-01`. You should also see exactly **ten** lines like:

```
WARNING redelivered, point overwritten in place: seq=1436
```

Ten, because `consumer_prefetch_count` is 10 — this is stage 6's "prefetch is
the duplicate window" made visible. The points were overwritten in place rather
than duplicated, because point identity is measurement + tag set + timestamp and
all three are unchanged. **Verified separately**: three writes of one identity
produce one point.

### The other outage shape — pause instead of stop

```bash
docker pause thermo-warren-influxdb-1
```

`stop` closes the listener, so writes fail in microseconds and the timeout never
comes into play. `pause` leaves the socket open and answering nothing, which is
the only case that exercises it:

```
WARNING storage write failed (attempt 1 of 3) for seq=1195: ReadTimeoutError:
        HTTPConnectionPool(host='influxdb', port=8086): Read timed out.
        (read timeout=1.999392556026578)
```

Two s per attempt, ~7.5 s per cycle. Run it for several minutes and confirm **no
heartbeat disconnect** — measured at 4.5 minutes of continuous stalls with zero
connection losses, cancels or reconnects. That is what the explicit
`heartbeat=60` in `amqp.connect()` was added to make sizeable.

### Shutdown during an outage

```bash
docker pause thermo-warren-influxdb-1
time docker compose stop consumer-store
docker inspect -f '{{.State.ExitCode}}' thermo-warren-consumer-store-1
```

Expect **~1 s and exit 0**. If you see 10 s and exit 137, the handler is not
abandoning its work on SIGTERM.

This needed two fixes, and the first was not enough. An interruptible backoff is
the obvious one — but pika still drains its prefetched deliveries through the
callback, and each one paid a full 2 s write timeout *before* the handler could
notice the shutdown. Ten of those against a 10 s grace is a SIGKILL. The second
fix skips the write attempt entirely once storage is already known bad.

Deliberately conditional on that, rather than on "are we stopping": a **normal**
shutdown still writes and acks its prefetched batch, or every restart would
return ten perfectly writable messages to ready and print ten `redelivered`
warnings that mean nothing.

### Two traps worth knowing

**`write_precision` defaults to `'ns'`, and the `Point`'s own precision does not
rewrite the number.** An int timestamp is serialized verbatim, so the only thing
that gives `1756400000123` meaning is the query parameter `write()` sends. Left
at the default, every point lands in January 1970 and nothing reports an error.

**A tight burst collapses in storage, and the broker is blameless.**

```bash
DEVICE_ID=burst-probe MQTT_CLIENT_ID=burst-probe \
  python -m telemetry.publisher --burst 200
```

200 messages published in **7 milliseconds**; all 200 delivered, consumed and
written; **7 points** survive — one per distinct millisecond, each holding the
last `seq` written in that millisecond. The queues drain, the DLQ stays empty,
and nothing is lost in the pipeline. The dashboard simply shows a hole that looks
like message loss and is not. At the 1 Hz steady state the margin is 1000x, so
this is a diagnosis to remember rather than a bug to fix — microsecond precision
would change the frozen payload contract and the firmware with it.

Check `count(distinct ts_ms)` against `count(seq)` before blaming the broker.

### Tests

```bash
pytest          # 114 passed, with no broker and no InfluxDB running
```

## Stage 10 verification

Storage. The first stage in six that adds a container rather than changing queue
arguments — no `--recreate`, no 406, no dead-lettering. Nothing wrote to
InfluxDB at this stage; `consumer_store` gained that at stage 11.

### The Definition of Done

```bash
docker compose up -d --wait     # exit 0; influxdb reports healthy
docker compose ps               # influxdb  Up (healthy)

# The bucket exists -- and note there is no --token here. The entrypoint wrote
# the CLI's host and admin token into /etc/influxdb2, which is a named volume
# for exactly this reason. If this ever asks for a token, that volume is what
# to check.
docker compose exec influxdb influx bucket list
```

Expect `telemetry` with retention printed as `infinite`, alongside the
`_monitoring` and `_tasks` buckets InfluxDB creates for itself.

### Write one point by hand and read it back

```bash
docker compose exec influxdb influx write \
  --bucket telemetry --precision s 'stage10_check,src=hand value=1'

docker compose exec influxdb influx query \
  'from(bucket:"telemetry") |> range(start:-1h)
     |> filter(fn:(r) => r._measurement == "stage10_check")'
```

Two things about that snippet are deliberate.

**The measurement is `stage10_check`, not `readings` — the telemetry measurement
stage 11 chose.** Retention is infinite, so this point is permanent; a throwaway
name keeps it from ever being mistaken for real data. `influx delete
--predicate` removes it if you want it gone.

**The read-back is Flux, not InfluxQL**, even though the project chose InfluxQL.
InfluxQL needs a DBRP mapping exposing the bucket under a v1-style database
name, and the plan puts that at stage 12. This is staging, not drift.

> **Corrected at stage 12.** That reasoning was sound but its premise was not
> checked: InfluxDB 2.x synthesises a read-only *virtual* DBRP mapping for any
> bucket without an explicit one, so **InfluxQL would have worked here at stage
> 10** without anything being created. The Flux read-back above is left as it
> was written; see "Stage 12 verification" for what was actually measured.

### Provisioning, not a stale volume

```bash
docker compose down             # NO -v
docker compose up -d --wait
docker compose logs influxdb | grep -i "skipping setup wrapper"
docker compose logs influxdb | grep -icE "user-provided scripts|initialization complete"
```

The bucket and the hand-written point must both still be there.

**The trap:** `found existing boltdb file, skipping setup wrapper` appears on a
genuine first boot too, so it is not the marker it looks like. The entrypoint
ends with `exec setpriv --reuid=influxdb ... "$BASH_SOURCE"` to drop from root,
and that re-exec re-enters `main()` after setup has already created the bolt
file. Count the setup markers instead:

| | `skipping setup wrapper` | `initialization complete` |
| --- | --- | --- |
| First boot | 1 | present |
| Every later start | 2 | absent |

### Why the healthcheck hits `/health`

```bash
docker compose logs influxdb | grep -ic "unauthorized\|401"     # expect 0
```

The plan warns that a healthcheck needing a token logs an authentication failure
every interval forever. `/health` needs none, and returns 200 healthy / 503
unhealthy so `curl -f` maps straight onto it. `/ready` has no failure status
code in the OSS API spec and cannot express "not ready" at all.

Unlike the broker's check, this one is *readiness*: `curl` proves the HTTP API
answers, not merely that a socket accepts. Stage 11 gates `consumer-store` on it.
(One consequence: right after a `docker unpause`, the gate refuses to start
`consumer-store` until the healthcheck recovers, reporting `dependency failed to
start: container ... is unhealthy`. Wait for health rather than retrying.)
The `start_period` covers a two-phase startup rather than a slow boot — on first
start the entrypoint runs a temporary `influxd` on port 9999, sets up against
it, kills it, and only then binds 8086.

## Stage 9 verification

The third and last dead-letter trigger. **The cap is not in the committed
configuration** — the plan calls for returning the queues to steady-state
settings once the lesson is recorded, so `telemetry.store` ends the stage with a
dead-letter exchange and nothing else. To re-run either experiment, edit
`STORE_ARGS` in `src/telemetry/topology_spec.py`:

```python
    "x-dead-letter-exchange": DLX,
    "x-max-length": STORE_MAX_LENGTH,               # add
    "x-overflow": STORE_OVERFLOW_OLDEST_OUT,        # or ..._NEWEST_OUT
```

Then, as at stage 8, the next plain declare exits 4 and you apply it with
`uv run python -m telemetry.topology --recreate`.

### The Definition of Done

```bash
docker compose stop consumer-store publisher
# purge all three queues
docker compose run --rm publisher python -m telemetry.publisher --burst 40
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready
```

`telemetry.store` holds steady at exactly **20** while `telemetry.dlq` grows.
The `x-death` reason reads **`maxlen`** — the third distinct value, after
`rejected` (stage 7) and `expired` (stage 8).

### Which end survives, and who finds out

Run the burst under each overflow mode and read the simulator's `seq` out of
each queue. Measured here:

| `x-overflow` | Survived | Dead-lettered | Publisher told? |
|---|---|---|---|
| `drop-head` | **31-40** (newest) | 1-30 (oldest) | **No.** Every PUBACK success |
| `reject-publish-dlx` | **1-18** (oldest) | 19-40 (newest) | **Yes.** Reason code 151 |

The second column is the expected lesson. **The fourth is the better one**: the
two modes differ not only in which end is sacrificed, but in whether the
producer is told anything at all. `drop-head` evicts a message that was already
accepted, so there is nothing to report; `reject-publish-dlx` refuses the
incoming publish, and the refusal travels back up the protocol:

```
ERROR publish not accepted: seq=19 reason_code=151 (Quota exceeded)
```

That error branch was written at stage 5 and this is the first time anything
made it fire.

> **Never use plain `reject-publish`.** It discards without dead-lettering, so
> the evidence never appears — and the name reads like the safer of the two.

### Three fates from one burst

Stop both consumers, leave `telemetry.observe` at its cap of 100 with
`drop-head` and no DLX, and `telemetry.store` at 20 with `reject-publish-dlx`:

```bash
docker compose stop consumer-observe consumer-store
docker compose run --rm publisher python -m telemetry.publisher --burst 150
```

| Queue | Kept | Lost | Evidence |
|---|---|---|---|
| `telemetry.observe` | 100 | ~53 | **none anywhere** |
| `telemetry.store` | 20 | 133 | 133 in the DLQ, and 133 errors at the publisher |

`telemetry.observe` has been shedding its head silently since stage 4 and
nothing has ever caught it. That is the lossy-by-design policy working as
intended, and it is only visible by contrast.

### Return to steady state

```bash
# STORE_ARGS back to the dead-letter exchange only
uv run python -m telemetry.topology --recreate
docker compose up -d --wait
```

## Stage 8 verification

> **Reverted at stage 9**, which returned `telemetry.store` to a dead-letter
> exchange and nothing else. To follow this section, add
> `"x-message-ttl": STORE_MESSAGE_TTL_MS` back to `STORE_ARGS` in
> `src/telemetry/topology_spec.py` first.

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

Expected steady state **as of stage 6**: `rabbitmq` healthy and `publisher`,
`consumer-observe` and `consumer-store` all Up, with only `topology` showing
`Exited (0)`. From stage 10 `influxdb` joins, and from stage 12 `grafana` joins
and a second service exits — see "Stage 12 verification" for the current shape.

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

> **Briefly changed at stage 8, restored at stage 9.** While the 30 s TTL was
> live, the *ready* messages this procedure accumulates began dead-lettering
> once they aged past it. `telemetry.store` carries no TTL again, so this
> section works as originally written. Worth knowing if you re-enable the stage
> 8 experiment: the unacknowledged 10 are unaffected either way, because a TTL
> never applies to a delivered message. `telemetry.observe` stays at **0** unacknowledged throughout, which
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
