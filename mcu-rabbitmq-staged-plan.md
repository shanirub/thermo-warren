# ESP32-C3 → RabbitMQ → InfluxDB → Grafana

A staged plan. Each stage states what it achieves, its Definition of Done, and how to verify it before moving on. Implementation is deliberately left open.

## Locked decisions

| Area | Choice | Reasoning |
|---|---|---|
| Firmware framework | ESP-IDF | Exposes MQTT QoS and connection-state events directly, which feed the ack/DLX material that is the actual learning goal |
| Sensor driver | `esp-idf-lib/dht` | Maintained, documented, stable API across versions |
| MQTT QoS | 1 | QoS 1 publishes become persistent messages inside RabbitMQ; QoS 0 become transient, which would make restart tests lie |
| Payload | JSON with a monotonic sequence number | Forces real parsing in the consumer, so malformed input is a natural poison-message case; `seq` makes at-least-once duplicates detectable |
| Queue type | Classic | Quorum queues don't support the `reject-publish-dlx` overflow behaviour used in stage 9 |
| Topology | Fan-out: two queues, same binding key | See below |
| Consumer language | Python + `pika` | Hand-written AMQP client is the point of the exercise |
| Storage | InfluxDB 2.x, pinned tag | Lowest-friction path from callback to rendered panel; keeps focus on the broker |
| Query language | InfluxQL, not Flux | InfluxQL survives a future move to InfluxDB 3; Flux does not |
| Run modes | Both containerised and locally against exposed ports | Containers for the full stack, local runs for a fast edit-debug loop |

## Topology

One MQTT topic maps to one AMQP routing key on the pre-existing `amq.topic` exchange. Two queues bind to it with the same key, so every message lands in both:

- **Durable path** — dead-letter exchange attached, manual acknowledgment, feeds InfluxDB.
- **Observation path** — no DLX, bounded length, deliberately lossy. Prints only.

This is the sharpest structural difference from Kafka. There, fan-out is a read-side choice (two consumer groups on one topic). In AMQP it is a write-side topology choice, and declaring only one binding means the second consumer receives nothing with no error anywhere in the system.

Giving the two queues different retention policies is intentional. The same message dead-letters on one and is silently dropped on the other, which teaches more than two identical queues would.

## Why this order

The broker is the learning target, so it comes first and the hardware comes last. A software publisher stands in for the MCU from stage 5 onward, which means the whole pipeline — fan-out, all three dead-letter triggers, ack semantics, the write path, the dashboard — is built and proven before any firmware exists. When the ESP32 arrives at stage 17 it only has to satisfy a contract that has already been validated, and a firmware bug can never be mistaken for a broker bug.

Pin identification stays at stage 1 regardless: twenty minutes, blocks nothing, and reveals a dead module early.

---

## Stage 1 — Identify the DHT11 pins

No power applied. An inverted supply on a 3-pin module destroys the sensor and possibly the GPIO. Use two independent methods and require them to agree: trace continuity from the bare sensor's known leg order to the header pins, and cross-check against the onboard pull-up, whose two pads sit on DATA and VCC.

**DoD:** each header pin identified as VCC, DATA, or GND, with both methods agreeing.

**Verify:** with the module disconnected, measure resistance between the identified VCC and DATA pins. Expect the pull-up value. Near-zero or open means the identification is wrong — re-trace rather than guess.

---

## Stage 2 — Project skeleton and both run modes

Establish the repository, dependency management, container image, and orchestration. Every host, port and credential resolves from configuration rather than a literal, because that is what allows one codebase to run both inside the container network and directly on the host.

**DoD:** the image builds; every module runs as a stub both ways.

**Verify:** run the same module in both modes and confirm it resolves a different broker hostname each time. Confirm the secrets file is gitignored.

---

## Stage 3 — Broker running with MQTT enabled

Bring up RabbitMQ with the management and MQTT plugins, a dedicated non-default user, and anonymous MQTT connections disabled. Expose AMQP (needed for local runs), MQTT, and the management UI bound to localhost.

The container healthcheck should validate all listeners, not just that the node process is alive — the MQTT listener comes up after the node, and a check that passes too early causes confusing downstream failures.

**DoD:** the container reports healthy and the management UI is reachable.

**Verify:** list the node's listeners and confirm both AMQP and MQTT appear. Then publish a throwaway MQTT message from a CLI client and confirm the connection appears in the management UI. The message itself goes nowhere yet.

---

## Stage 4 — Declare the topology as a one-shot job

Exchange bindings, both queues, the dead-letter exchange and the dead-letter queue are declared by code run as a job that must complete before anything publishes. This is not merely tidy: unlike Kafka, which auto-creates topics and retains messages regardless of consumers, a RabbitMQ topic exchange discards anything with no matching binding, silently.

Declaration must be idempotent, and must also support a destructive redeclare — stages 8 and 9 change queue arguments, and redeclaring a queue with different arguments is an error rather than an update.

**DoD:** the job succeeds, and succeeds again on a second run.

**Verify — this is the fan-out proof:** publish **one** MQTT message and confirm **both** queues show a depth of one. If only one received it, the second binding is wrong — and note that nothing reported an error. That silence is the lesson.

---

## Stage 5 — Software publisher standing in for the MCU

A publisher emitting the payload contract at roughly 1 Hz over MQTT at QoS 1. It needs controls for rate, periodic malformed output, and burst publishing, because later stages depend on all three.

This defines the contract stage 17's firmware must match.

**DoD:** both queue depths climb in lockstep with no consumers running.

**Verify:** inspect a message in the management UI — well-formed payload, and delivery mode showing persistent. That confirms QoS 1 mapped to a persistent AMQP message as expected. Purge both queues afterwards.

---

## Stage 6 — Both consumers, with manual acknowledgment

Two consumers: one on the observation queue that logs, one on the durable queue that uses manual acknowledgment and a bounded prefetch. The durable consumer's callback at this stage parses and acknowledges; the storage write arrives at stage 11, and where it gets inserted is the point of that stage.

**DoD:** both queues drain while the publisher runs, and both consumers report the same sequence numbers.

**Verify — draining is not the test.** Add a delay before the acknowledgment, confirm the unacknowledged count sits at the prefetch limit, then kill the consumer uncleanly. Those messages must return to ready, not vanish. If they vanish, automatic acknowledgment is on somewhere.

**Second check, for the fan-out:** stop only the observation consumer. The durable queue keeps draining while the observation queue grows. The two are genuinely independent.

---

## Stage 7 — Dead-lettering trigger 1: rejection

The durable consumer rejects unparseable messages without requeueing them.

**DoD:** malformed payloads reach the dead-letter queue and do not loop.

**Verify:** run the publisher in periodic-corruption mode and inspect a dead-lettered message's death header — it records the reason, the originating queue, and a count. Note that the observation consumer sees the same bytes and simply logs them, because that queue has no dead-letter exchange.

> Requeueing an unparseable message instead creates an infinite redelivery loop that will pin a CPU core. Worth reproducing once, deliberately.

---

## Stage 8 — Dead-lettering trigger 2: expiry

Add a short message time-to-live to the durable queue and redeclare.

**DoD:** messages sitting unconsumed past the TTL move to the dead-letter queue rather than being dropped.

**Verify:** stop the durable consumer, publish for a while, and watch the queue drain into the DLQ with no consumer running at all. The death reason should now read as expiry rather than rejection.

> Queue-level and per-message TTL behave differently: per-message TTL only takes effect once the message reaches the head of the queue, so an expired message queued behind a long-lived one lingers past its deadline. Worth demonstrating.

---

## Stage 9 — Dead-lettering trigger 3: overflow

Clear the TTL, add a small maximum queue length, and keep the default overflow behaviour, which dead-letters from the front — the oldest messages.

**DoD:** at the cap, further publishes overflow into the dead-letter queue.

**Verify:** stop the durable consumer, publish a burst, and confirm the main queue holds steady while the DLQ grows. Then switch to the overflow mode that rejects the *newest* publishes into the DLQ instead, and repeat. The contrast is the lesson: check which sequence numbers survive each time. Oldest-out versus newest-out is a real design decision in any bounded system.

Meanwhile the observation queue has been silently dropping its own head all along, with nothing catching it — the lossy-by-design policy working as intended.

Return both queues to steady-state settings afterwards, keeping the dead-letter exchange.

---

## Stage 10 — Storage running

Bring up InfluxDB with initial organisation, bucket, and token provisioned at first start. Use a healthcheck that needs no token, or every interval logs an authentication failure.

**DoD:** the container reports healthy and the bucket exists.

**Verify:** write one point by hand and query it back.

> Capture the admin token into your secrets file before first startup. From version 2.9 onward tokens are hashed on disk and the plaintext cannot be recovered afterwards.

---

## Stage 11 — Durable consumer writes to storage

Insert the write between the parse and the acknowledgment. The position is the whole lesson: acknowledging before the write means a crash between the two loses the message permanently; acknowledging after means a crash causes redelivery, giving at-least-once end to end.

**DoD:** points appear in near-real-time, and the acknowledgment strictly follows a confirmed write.

**Verify — the failure test is the point.** Stop the storage container. Writes now fail; decide and implement what happens — retry with backoff, leaving messages unacknowledged, or reject to the DLQ after a bounded number of attempts. Either is defensible; pick one and be able to say why. Confirm nothing is silently lost: every message should be visibly unacknowledged, back in ready, or in the DLQ. Restart and confirm recovery.

Note that the observation consumer is unaffected throughout. Isolating the fragile path from the observation path is the practical payoff of the fan-out.

> Two things to decide here rather than default into: a long write timeout can block the AMQP client's I/O loop and cause a heartbeat timeout, which presents as an unexplained broker disconnect; and since QoS 1 is at-least-once, duplicates are possible by design, so deduplicating versus letting storage overwrite on identical timestamps is a conscious choice.

---

## Stage 12 — Visualization connected

Because queries use InfluxQL rather than Flux, InfluxDB 2.x needs a mapping exposing the bucket under a v1-style database name. Grafana's datasource should be provisioned from version-controlled configuration rather than clicked in.

**DoD:** Grafana starts with the datasource already present and its connection test passes.

**Verify:** run a bare query and get rows back. Then tear the stack down and bring it up again, confirming the datasource is still configured — that proves provisioning works rather than a stale volume.

---

## Stage 13 — Dashboard

Temperature and humidity panels on a short time range with a fast refresh, so feedback during later stages is immediate.

**DoD:** both panels render live data from the software publisher.

**Verify:** change the publisher's output range and watch the traces respond within one refresh cycle.

The software pipeline is now proven end to end. Everything after this replaces the publisher with real hardware.

---

## Stage 14 — Firmware toolchain and a known-good flash

Sensor still disconnected, so a toolchain problem is never confused with a wiring problem. A minimal project that logs a counter.

The Super Mini has no USB-to-UART bridge — the USB connector wires directly to the chip's internal USB peripheral. Console output must be configured for USB Serial/JTAG rather than UART, the two GPIOs carrying the USB differential pair must be treated as reserved, and a first flash may need download mode entered manually.

**DoD:** build, flash and monitor all succeed against the correct target.

**Verify:** the counter increments, and reset restarts it.

---

## Stage 15 — Read the sensor

Add the driver component, wire per stage 1, and read on a fixed cadence.

Two constraints to design around: the sensor enforces a minimum sampling interval of roughly two seconds, so cache the last good reading and let the faster publish loop read the cache — a tick with no fresh sample is normal, not an error. And since the module carries its own pull-up, the internal pull-up must stay disabled.

**DoD:** plausible readings logged on the sampling cadence.

**Verify:** breathe on the sensor; humidity should climb and then decay. A value that never changes means a stale buffer or a silently accepted checksum failure — check the driver's error return rather than printing the struct.

---

## Stage 16 — Network connectivity

Station mode using the event-driven API, treating connect, disconnect and address-acquired as discrete events with automatic reconnect. Credentials can start in build configuration; moving them to non-volatile storage is a refinement, not a blocker.

**DoD:** associates, obtains an address, and reconnects automatically after the access point is power-cycled.

**Verify:** ping the device from the host, then reboot the access point and confirm it rejoins without a manual reset.

> The ESP32-C3 is 2.4 GHz only. Confirm a 2.4 GHz SSID is actually reachable if the access point is dual-band.

---

## Stage 17 — MCU publishes: swap out the software publisher

Add the MQTT client, handle connection-state events explicitly, and publish the cached reading at 1 Hz, QoS 1, with a monotonic sequence number.

**The contract:** identical topic and payload shape to the software publisher. If either consumer needs a change to accept MCU messages, the firmware is wrong, not the consumer.

**DoD:** with the software publisher stopped, the dashboard shows real room temperature.

**Verify:** stop the publisher and confirm the dashboard goes flat; power the MCU and confirm it resumes. Both consumers should report matching sequence numbers, proving fan-out still works with a real publisher. Diff an MCU payload against a simulated one — same keys, same types. Then breathe on the sensor and watch the dashboard respond.

---

## Stage 18 — End-to-end resilience

Run the full pipeline and work through a failure matrix, writing down each prediction before running it. The value is where prediction and observation diverge.

| Fault | What to observe |
|---|---|
| Kill the durable consumer uncleanly | Unacknowledged returns to ready; no gap in stored data after catch-up |
| Stop the observation consumer for several minutes | Its queue grows then starts dropping at its cap; the durable queue is unaffected |
| Stop and restart the broker | Do queued messages survive? Where QoS 1 persistence earns its keep. MCU reconnect behaviour. |
| Stop and restart storage | Stage 11's policy plays out — never silent loss |
| Power-cycle the MCU | The sequence number restarts; does anything downstream care? |
| Malformed payload in steady state | Dead-lettered on one queue, merely logged on the other |
| Disconnect Wi-Fi for a minute | Does the MCU buffer, drop, or block? |
| Run both publishers at once | Two publishers, one routing key, duplicate sequence numbers. What breaks? |
| Tear the whole stack down and back up | Does the topology survive? It should — confirm rather than assume |

**DoD:** every row has a documented observed outcome, and no row produces unexplained data loss.

## Dependencies and gating

- Stages 1–13 need no hardware at all.
- Stage 4 hard-gates stage 5. Publishing before bindings exist produces silent discards, so orchestration should enforce completion rather than relying on discipline.
- Stage 5 defines the payload contract stage 17 must satisfy — the single interface between the two halves of the project.
- Stages 7–9 are independent of each other but all depend on stage 6's manual-acknowledgment consumer.
- Stage 11 is the only stage where acknowledgment ordering has real consequences.
- Stage 14 can run in parallel with anything from stage 2 onward.

## Deliberately out of scope

TLS on the MQTT listener, broker-side topic authorisation, quorum queues, alerting, and multi-sensor topic hierarchies. Each is a reasonable follow-on once the pipeline is stable; none teaches anything about queues, exchanges, or dead-lettering that stages 4–11 don't already cover.
