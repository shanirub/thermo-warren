"""What the broker enforces: names and policy.

Everything here is identical in every run mode, and that is deliberate. The
declarer, the publisher and both consumers must agree byte-for-byte, and a
disagreement produces no error anywhere in the system -- a queue bound with the
wrong routing key simply stays empty forever. Values that can drift silently
do not belong in the environment.

Contrast with `config.py`, which holds what *does* differ between host mode and
container mode: endpoints, credentials, and each process's own behaviour.

    If the broker enforces it, it belongs here.
    If it describes how this process reaches the broker, it belongs in config.

Changing anything in this module requires redeclaring the affected queue.
RabbitMQ treats a declare with different arguments as an error rather than an
update, so that means `python -m telemetry.topology --recreate`.
"""

# --- The message path ------------------------------------------------------

# Pre-existing topic exchange that the MQTT plugin publishes into. Never
# declared by us; declaring an `amq.` name is refused with ACCESS_REFUSED (403).
SOURCE_EXCHANGE = "amq.topic"

# MQTT uses "/" as its topic level separator and AMQP 0-9-1 uses "."; the
# plugin translates between them. Dots in MQTT topics and slashes in routing
# keys are documented as not working as expected, so neither appears here.
# No leading slash either -- it would translate to an empty leading word.
MQTT_TOPIC = "sensors/esp32c3/telemetry"

# Derived, not written twice. These two must never drift apart, and a drift
# between what the publisher sends and what the binding matches is precisely
# the silent-discard failure stage 4 exists to prevent.
ROUTING_KEY = MQTT_TOPIC.replace("/", ".")

# --- Queues ----------------------------------------------------------------
#
# Named after what their consumer does, not after their durability: both are
# declared durable, so "durable" in a name would be noise at best. The names
# match the module names, so a log line maps to a `list_queues` row with no
# translation. The shared prefix keeps them together in that listing, where the
# MQTT plugin's own `mqtt-subscription-*` queues also appear.

QUEUE_STORE = "telemetry.store"        # durable path: manual ack, dead-letters
QUEUE_OBSERVE = "telemetry.observe"    # observation path: bounded, lossy

# --- Dead-letter side ------------------------------------------------------
#
# A distinct exchange, necessarily: pointing x-dead-letter-exchange back at
# amq.topic would republish each rejected message into the queue it just left.
#
# Topic rather than fanout so that a second dead-letter queue could later be
# bound to a narrower pattern -- dead-lettered messages keep their original
# routing key. The "#" binding matches any key, which is what makes that safe;
# a "*" here would silently stop matching multi-word keys.

DLX = "telemetry.dlx"
DLX_TYPE = "topic"
QUEUE_DLQ = "telemetry.dlq"
DLQ_BINDING_KEY = "#"

# --- Policy ----------------------------------------------------------------
#
# Stated explicitly even where it matches the RabbitMQ default. This is a
# learning project: an argument you can read is worth more than one you have to
# remember, and the redeclare cost of an extra argument is nil.

QUEUE_TYPE = "classic"                 # quorum queues do not support
                                       # reject-publish-dlx, needed at stage 9

OBSERVE_MAX_LENGTH = 100               # ~100s of publishing at 1 Hz: long
                                       # enough to watch the cap arrive, short
                                       # enough to eyeball which seq survived
OBSERVE_OVERFLOW = "drop-head"         # evict oldest; explicit because plain
                                       # reject-publish discards without
                                       # dead-lettering, which is a trap

STORE_ARGS: dict[str, object] = {
    "x-queue-type": QUEUE_TYPE,
    # Inert until stage 7 gives the consumer a reason to reject. Attaching it
    # now costs nothing and keeps stages 7-9 pure consumer changes.
    "x-dead-letter-exchange": DLX,
    # No x-message-ttl (stage 8) and no x-max-length (stage 9), deliberately.
}

OBSERVE_ARGS: dict[str, object] = {
    "x-queue-type": QUEUE_TYPE,
    "x-max-length": OBSERVE_MAX_LENGTH,
    "x-overflow": OBSERVE_OVERFLOW,
    # No x-dead-letter-exchange. The absence is the point: dropped messages
    # leave no trace, which is the contrast stages 7 and 9 demonstrate.
}

DLQ_ARGS: dict[str, object] = {
    "x-queue-type": QUEUE_TYPE,
    # No dead-letter exchange of its own -- a DLQ that dead-letters is a loop.
    # Unbounded on purpose: its contents are the evidence you came to read.
}

# Only these are deleted by --recreate. The dead-letter queue survives, because
# a routine "change the TTL" must not discard the x-death headers you collected.
RECREATABLE = (QUEUE_STORE, QUEUE_OBSERVE)


def describe() -> str:
    """Human-readable topology, for logging next to config.describe()."""
    lines = [
        f"  mqtt topic           = {MQTT_TOPIC}",
        f"  routing key          = {ROUTING_KEY}",
        f"  source exchange      = {SOURCE_EXCHANGE}",
        f"  durable queue        = {QUEUE_STORE}  {STORE_ARGS}",
        f"  observation queue    = {QUEUE_OBSERVE}  {OBSERVE_ARGS}",
        f"  dead-letter exchange = {DLX} (type={DLX_TYPE})",
        f"  dead-letter queue    = {QUEUE_DLQ}  {DLQ_ARGS}",
        f"  dlq binding key      = {DLQ_BINDING_KEY}",
    ]
    return "\n".join(lines)
