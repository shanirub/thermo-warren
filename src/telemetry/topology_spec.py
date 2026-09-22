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

# --- Stage 8's and stage 9's experiments -----------------------------------
#
# Both are DEFINED but NOT in STORE_ARGS below: stage 9 returned telemetry.store
# to its baseline of "dead-letter exchange and nothing else". They are kept
# here, with their measured behaviour, so either experiment is one edit away --
# see the comment block in STORE_ARGS.

STORE_MESSAGE_TTL_MS = 30_000          # 30s. With no consumer the ready count
                                       # plateaus at roughly publish rate x TTL
                                       # while the DLQ grows linearly, which
                                       # makes the result a number you can
                                       # predict rather than "stuff moved".
                                       # An order of magnitude above the 2-4s
                                       # consumer restarts measured at stages
                                       # 6-7, so a routine restart never
                                       # dead-letters live data. Measured at
                                       # stage 8: ready plateaued at 59-61 with
                                       # both publishers running (~2/s x 30s)
                                       # while the DLQ grew linearly.

STORE_MAX_LENGTH = 20                  # Stage 9. Small on purpose: every
                                       # surviving seq fits on one line, which
                                       # is what makes the oldest-out vs
                                       # newest-out contrast readable, and
                                       # `--burst 40` is exactly twice the cap.
                                       # Deliberately NOT 100 like the
                                       # observation queue -- two bounded queues
                                       # running one number would look like a
                                       # convention rather than two policies.

# The stage 9 comparison. At the cap a bounded queue must sacrifice one end or
# the other, and which end is a real design decision in any bounded system.
STORE_OVERFLOW_OLDEST_OUT = "drop-head"            # dead-letters the OLDEST
STORE_OVERFLOW_NEWEST_OUT = "reject-publish-dlx"   # dead-letters the NEWEST
#
# NEVER plain "reject-publish" here: it discards the message WITHOUT
# dead-lettering, so the evidence this whole project is built to read simply
# does not appear. The trap is that the name looks like the safer of the two.
#
# reject-publish-dlx is classic-queue only, which is why QUEUE_TYPE is classic.

OBSERVE_MAX_LENGTH = 100               # ~100s of publishing at 1 Hz: long
                                       # enough to watch the cap arrive, short
                                       # enough to eyeball which seq survived
OBSERVE_OVERFLOW = "drop-head"         # evict oldest; explicit because plain
                                       # reject-publish discards without
                                       # dead-lettering, which is a trap

STORE_ARGS: dict[str, object] = {
    "x-queue-type": QUEUE_TYPE,
    # The one permanent argument. Stage 7 gave the consumer a reason to reject
    # into it, stages 8 and 9 gave the broker two more, and it outlives all of
    # them.
    "x-dead-letter-exchange": DLX,
    #
    # BASELINE: dead-letter exchange and nothing else. Stages 8 and 9 each added
    # an argument here, demonstrated it, and took it out again -- the plan calls
    # for returning to steady-state settings once the lesson is recorded, and
    # stages 10-13 want data reaching InfluxDB unimpeded.
    #
    # To re-run stage 8's expiry experiment, add:
    #     "x-message-ttl": STORE_MESSAGE_TTL_MS,
    #
    # To re-run stage 9's overflow experiment, add BOTH:
    #     "x-max-length": STORE_MAX_LENGTH,
    #     "x-overflow": STORE_OVERFLOW_NEWEST_OUT,   # or ..._NEWEST_OUT
    #
    # Either way it is a queue-argument change, so the next plain declare exits
    # 4 and you need `python -m telemetry.topology --recreate`.
}

OBSERVE_ARGS: dict[str, object] = {
    "x-queue-type": QUEUE_TYPE,
    "x-max-length": OBSERVE_MAX_LENGTH,
    "x-overflow": OBSERVE_OVERFLOW,
    # No x-message-ttl here, deliberately. This queue already sheds its head at
    # the cap, and adding a second reason for a message to vanish would make it
    # impossible to say which one acted.
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
