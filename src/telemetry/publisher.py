"""Software publisher standing in for the MCU.

Emits the payload contract at ~1 Hz over MQTT at QoS 1, with controls for
rate, periodic malformed output, and burst publishing. This is the interface
between the software half of the project and the hardware half: stage 17's
firmware must satisfy the same contract.

Long-lived, unlike topology.py. Reconnection is ordinary steady-state
behaviour here, so it is left to paho's own automatic reconnect rather than a
hand-rolled retry loop -- see connect() below.

Exit codes:
    0  clean exit (burst completed, or SIGTERM/SIGINT received)
    2  could not reach the broker
    3  broker rejected our credentials
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import random
import signal
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from telemetry import topology_spec as spec
from telemetry.config import settings
from telemetry.logging_setup import configure_logging

# --- The payload contract ---------------------------------------------------
# Moved to payload.py at stage 7, when both consumers began validating the whole
# contract rather than reading one field -- the revisit this comment used to ask
# for. Re-exported here rather than referenced through the module, so the names
# stay reachable as publisher.FIELD_SEQ for anything that already used them.
from telemetry.payload import (  # noqa: F401  -- re-exported deliberately
    CONTENT_TYPE_JSON,
    FIELD_DEVICE,
    FIELD_HUMIDITY_PCT,
    FIELD_SEQ,
    FIELD_TEMP_C,
    FIELD_TS_MS,
    build_payload,
)

log = logging.getLogger(__name__)

# DHT11 range and resolution (README / stage 5 planning): roughly 0-50 degC
# and 20-90 %RH, at 1-unit resolution. Values below step by whole units even
# though the field type is float, so the simulation does not imply precision
# the real sensor does not have.
TEMP_MIN_C, TEMP_MAX_C = 0, 50
HUMIDITY_MIN_PCT, HUMIDITY_MAX_PCT = 20, 90

EXIT_OK = 0
EXIT_UNREACHABLE = 2
EXIT_REJECTED = 3

# How long to wait for the CONNACK that confirms or refuses the connection,
# before giving up and reporting EXIT_UNREACHABLE. Same order of magnitude as
# topology.py's retry budget, for a broker that is merely slow to answer.
CONNECT_TIMEOUT_SECONDS = 10.0

# Burst mode: how long to wait for each outstanding PUBACK before giving up
# and logging rather than hanging forever on a broker that stopped answering.
PUBLISH_WAIT_TIMEOUT_SECONDS = 5.0


class BrokerUnreachable(RuntimeError):
    """The initial TCP connect failed, or no CONNACK arrived in time."""


class CredentialsRejected(RuntimeError):
    """The broker's CONNACK carried a failure reason code."""


@dataclass
class ClientState:
    """Shared between the main thread and paho's network thread via userdata."""

    connected: threading.Event = field(default_factory=threading.Event)
    connect_failure: ReasonCode | None = None
    # mid -> seq, so on_publish can name the seq of a failed publish. Entries
    # are popped on PUBACK; QoS 1 guarantees exactly one PUBACK per publish.
    pending: dict[int, int] = field(default_factory=dict)


class DriftSimulator:
    """Plausible, slowly-drifting DHT11 readings.

    A random walk of +/-1 unit per tick, clamped to the sensor's documented
    range. Slow drift rather than independent noise per sample, so a Grafana
    panel at stage 13 shows a slope instead of static.
    """

    def __init__(self, temp_c: float = 22.0, humidity_pct: float = 55.0) -> None:
        self.temp_c = temp_c
        self.humidity_pct = humidity_pct

    def step(self) -> tuple[float, float]:
        self.temp_c = _clamp(
            self.temp_c + random.choice((-1, 0, 1)), TEMP_MIN_C, TEMP_MAX_C
        )
        self.humidity_pct = _clamp(
            self.humidity_pct + random.choice((-1, 0, 1)),
            HUMIDITY_MIN_PCT,
            HUMIDITY_MAX_PCT,
        )
        return self.temp_c, self.humidity_pct


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sequence_numbers() -> Iterator[int]:
    """seq starts at 1 each process run.

    Not globally unique across restarts -- that is accepted. It makes
    at-least-once duplicates detectable within a run and lets stage 9 see
    which messages survived the queue cap; neither needs cross-restart
    uniqueness.
    """
    return itertools.count(1)


def now_ms() -> int:
    """Unix epoch milliseconds, stamped at the moment of measurement.

    Milliseconds, not seconds: InfluxDB point identity is measurement + tag
    set + timestamp, so two points sharing a timestamp overwrite rather than
    accumulate. At second resolution a stage 9 burst would silently collapse
    into a handful of points.
    """
    return time.time_ns() // 1_000_000


def should_corrupt(seq: int, corrupt_every: int | None) -> bool:
    return corrupt_every is not None and seq % corrupt_every == 0


def corrupt(serialized: str) -> str:
    """Truncate valid JSON into syntactically broken JSON.

    Dropping the back half of the string takes the closing braces with it, so
    the result fails json.loads regardless of payload content. Deliberately
    not "valid JSON, wrong fields" -- stage 7's poison-message case is a
    parse failure, not a schema failure.
    """
    return serialized[: len(serialized) // 2]


# --- MQTT client ------------------------------------------------------------


def _on_connect(client, userdata, flags, reason_code, properties) -> None:
    if reason_code.is_failure:
        log.error("broker refused connection: %s", reason_code)
        userdata.connect_failure = reason_code
    else:
        log.info("connected: client_id=%s", settings.mqtt_client_id)
        userdata.connect_failure = None
    userdata.connected.set()


def _on_disconnect(client, userdata, flags, reason_code, properties) -> None:
    userdata.connected.clear()
    if reason_code.is_failure:
        log.warning("disconnected: %s", reason_code)
    else:
        log.info("disconnected")


def _on_publish(client, userdata, mid, reason_code, properties) -> None:
    # Trap: at QoS 0 paho fabricates this callback with an always-success
    # reason code, because MQTT has no reason code on the PUBLISH packet
    # itself. Not reachable here -- every publish() call below is QoS 1 -- but
    # do not let future rate-tuning work quietly drop the QoS and lose this.
    seq = userdata.pending.pop(mid, None)
    if reason_code.value != 0:
        log.error(
            "publish not accepted: seq=%s reason_code=%d (%s)",
            seq,
            reason_code.value,
            reason_code,
        )
    else:
        log.debug("puback ok: seq=%s", seq)


def build_client(state: ClientState) -> mqtt.Client:
    client = mqtt.Client(
        # V1 is deprecated and has different callback signatures; V2 gives
        # on_connect/on_disconnect/on_publish the same shape under MQTT 3 and 5.
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=settings.mqtt_client_id,
        # MQTT 5, not the 3.1.1 default: its PUBACK carries a reason code,
        # which is what makes an unroutable publish observable. Under 3.1.1
        # the broker has no way to report a publish error short of closing
        # the connection, so a wrong topic would fail silently.
        protocol=MQTTProtocolVersion.MQTTv5,
        userdata=state,
    )
    client.username_pw_set(settings.rabbitmq_user, settings.rabbitmq_password)
    # Explicit rather than accepting paho's defaults, even though these values
    # match them -- an argument you can read is worth more than one you have
    # to remember.
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_publish = _on_publish
    return client


def connect(client: mqtt.Client, state: ClientState) -> None:
    """Open the connection and block until the CONNACK confirms or refuses it.

    client.connect() only performs the TCP handshake and sends the CONNECT
    packet -- the CONNACK itself is processed by the network loop, so
    loop_start() must be running before it can arrive. Ongoing reconnection
    after this point is paho's own automatic-reconnect machinery, not this
    function's concern.
    """
    try:
        client.connect(
            settings.rabbitmq_host,
            settings.rabbitmq_mqtt_port,
            keepalive=60,  # explicit; matches paho's own default
        )
    except OSError as exc:
        raise BrokerUnreachable(
            f"could not reach {settings.rabbitmq_host}:{settings.rabbitmq_mqtt_port}: "
            f"{exc}"
        ) from exc

    client.loop_start()

    if not state.connected.wait(timeout=CONNECT_TIMEOUT_SECONDS):
        client.loop_stop()
        raise BrokerUnreachable(
            f"no CONNACK from {settings.rabbitmq_host}:{settings.rabbitmq_mqtt_port} "
            f"within {CONNECT_TIMEOUT_SECONDS}s"
        )

    if state.connect_failure is not None:
        client.loop_stop()
        raise CredentialsRejected(
            f"broker rejected the connection: {state.connect_failure}"
        )


def publish_one(
    client: mqtt.Client,
    state: ClientState,
    seq: int,
    drift: DriftSimulator,
    corrupt_every: int | None,
    message_expiry: int | None = None,
) -> mqtt.MQTTMessageInfo:
    temp_c, humidity_pct = drift.step()
    payload = build_payload(seq, temp_c, humidity_pct, now_ms())
    serialized = json.dumps(payload)

    if should_corrupt(seq, corrupt_every):
        serialized = corrupt(serialized)
        log.warning("emitting malformed payload: seq=%d", seq)

    properties = Properties(PacketTypes.PUBLISH)
    properties.ContentType = CONTENT_TYPE_JSON

    if message_expiry is not None:
        # MQTT 5 Message Expiry Interval, in WHOLE SECONDS. RabbitMQ's MQTT
        # plugin converts it to a per-message TTL in milliseconds -- verified
        # in mc_mqtt (rabbitmq_mqtt-4.3.6), which does
        # `'Message-Expiry-Interval' := Seconds -> ttl => timer:seconds(Seconds)`.
        # So this is the AMQP `expiration` property by another name, and one
        # second is the finest granularity reachable from an MQTT publisher.
        #
        # Per-message TTL is evaluated only when a message reaches the head of
        # the queue, so a short expiry set here can be outlived by whatever sits
        # in front of it. That is stage 8's demonstration, not a bug.
        properties.MessageExpiryInterval = message_expiry
    # Verified at stage 17 against a queued MCU message: RabbitMQ's MQTT plugin
    # does map this through to the AMQP content_type property visible in the
    # management API. Still nothing depends on it -- it is set because it is
    # correct MQTT 5, not because a check reads it.

    info = client.publish(
        spec.MQTT_TOPIC,
        serialized,
        qos=1,  # persistent at RabbitMQ; QoS 0 would be transient and make
                # the later restart tests lie
        properties=properties,
    )
    state.pending[info.mid] = seq
    log.info("published: seq=%d", seq)
    return info


def run_steady(
    client: mqtt.Client,
    state: ClientState,
    stop_event: threading.Event,
    drift: DriftSimulator,
    corrupt_every: int | None,
    message_expiry: int | None = None,
) -> None:
    interval = settings.publish_interval_seconds
    for seq in sequence_numbers():
        if stop_event.is_set():
            break
        publish_one(client, state, seq, drift, corrupt_every, message_expiry)
        # Doubles as the sleep and the early-exit check: a signal during the
        # wait returns immediately instead of finishing out the interval.
        stop_event.wait(interval)


def run_burst(
    client: mqtt.Client,
    state: ClientState,
    drift: DriftSimulator,
    corrupt_every: int | None,
    count: int,
    message_expiry: int | None = None,
) -> None:
    """Publish `count` messages as fast as possible, then wait for delivery.

    At QoS 1 with loop_start(), PUBACKs arrive asynchronously on the network
    thread. Disconnecting right after the last publish() call would race
    those PUBACKs and could drop messages that were never actually lost --
    they just hadn't been confirmed yet. wait_for_publish() closes that gap.
    """
    published = [
        (seq, publish_one(client, state, seq, drift, corrupt_every, message_expiry))
        for seq in range(1, count + 1)
    ]

    timed_out = 0
    for seq, info in published:
        info.wait_for_publish(timeout=PUBLISH_WAIT_TIMEOUT_SECONDS)
        if not info.is_published():
            timed_out += 1
            log.error(
                "PUBACK timed out after %.1fs: seq=%d",
                PUBLISH_WAIT_TIMEOUT_SECONDS,
                seq,
            )

    log.info(
        "burst complete: %d messages published, %d timed out waiting for PUBACK",
        count,
        timed_out,
    )


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--corrupt-every",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            "emit every Nth message as malformed JSON (truncated, not just "
            "wrong fields) -- stage 7's poison-message case"
        ),
    )
    parser.add_argument(
        "--burst",
        type=_positive_int,
        default=None,
        metavar="N",
        help="publish N messages as fast as possible, ignoring the rate "
        "setting, wait for delivery, then exit",
    )
    parser.add_argument(
        "--message-expiry",
        type=_positive_int,
        default=None,
        metavar="SECONDS",
        help=(
            "set the MQTT 5 Message Expiry Interval on every message, which "
            "RabbitMQ turns into a per-message TTL. Whole seconds only. Stage "
            "8 uses it to show that a per-message TTL is evaluated at the head "
            "of the queue, so a short expiry can be outlived by what sits in "
            "front of it"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()

    log.info("resolved configuration:\n%s", settings.describe())

    if args.message_expiry is not None:
        log.warning(
            "--message-expiry %ds: every message carries a per-message TTL. "
            "The queue's own x-message-ttl still applies, and the effective "
            "deadline is expected to be the lower of the two.",
            args.message_expiry,
        )

    state = ClientState()
    client = build_client(state)

    stop_event = threading.Event()

    def handle_signal(signum, _frame) -> None:
        log.info("received signal %d, shutting down", signum)
        stop_event.set()

    # Compose sends SIGTERM on `down`; an unclean disconnect is noise in the
    # broker's connection log, so both signals get a clean MQTT disconnect.
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        connect(client, state)
    except BrokerUnreachable as exc:
        log.error("%s", exc)
        return EXIT_UNREACHABLE
    except CredentialsRejected as exc:
        log.error("%s", exc)
        return EXIT_REJECTED

    drift = DriftSimulator()

    try:
        if args.burst is not None:
            run_burst(
                client, state, drift, args.corrupt_every, args.burst,
                args.message_expiry,
            )
        else:
            run_steady(
                client, state, stop_event, drift, args.corrupt_every,
                args.message_expiry,
            )
    finally:
        client.disconnect()
        client.loop_stop()

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
