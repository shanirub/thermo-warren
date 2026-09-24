"""Durable-path consumer.

Manual acknowledgment, bounded prefetch, dead-letter exchange attached. Parses
and acknowledges from stage 6; rejects anything that fails the payload contract
from stage 7, without requeueing, so it dead-letters through telemetry.dlx.
From stage 11 it writes to InfluxDB between the parse and the acknowledgment,
and that ordering is the whole lesson of the stage: acknowledging first loses
the message if the process dies in between, acknowledging after redelivers it,
which is what makes the pipeline at-least-once end to end.

Exit codes:
    0  clean exit (SIGTERM/SIGINT received)
    2  could not reach the broker on the first attempt
    3  broker rejected our credentials
    5  no INFLUXDB_TOKEN, so storage cannot be reached at all

5 is local to this module, exactly as topology.py owns EXIT_MISMATCH = 4: no
other consumer touches storage, so the code is meaningless to them. It is a
configuration failure at startup, distinct from 3, which is the *broker*
refusing credentials that were supplied.

Implemented at stage 6, extended at stages 7-9 and 11.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from telemetry import payload as contract
from telemetry import storage
from telemetry import topology_spec as spec
from telemetry.amqp import is_stopping, run_consumer, sleep_unless_stopping
from telemetry.config import settings
from telemetry.logging_setup import configure_logging
from telemetry.payload import FIELD_SEQ, ContractViolation

log = logging.getLogger(__name__)

EXIT_UNCONFIGURED = 5


def build_handler(ack_delay: float | None, requeue_poison: bool, write_api):
    """Return the on_message callback, closed over the flags and the write API.

    A closure rather than module-level globals, so the flags are visible in the
    call chain and the handler stays assertable against a mock channel. The
    write API joins them at stage 11 for the same reason: the tests drive this
    handler with a mock in its place and never open a socket.
    """

    # Whether the last message exhausted its write budget. Consulted in exactly
    # one place -- the shutdown guard in store() -- and the reason it exists is
    # measured, not theoretical.
    #
    # **Verified**: SIGTERM while storage was PAUSED (accepting connections and
    # never answering) was killed at the 10s grace with exit 137. An
    # interruptible backoff is not enough on its own, because pika still drains
    # its prefetched deliveries through this callback and each one pays a full
    # influxdb_timeout_ms before the handler can notice the shutdown -- 10 x 2s
    # against a 10s grace. Skipping the attempt when storage is already known
    # bad takes that to zero.
    #
    # Conditional on this flag rather than on is_stopping() alone, so that a
    # NORMAL shutdown still writes and acks its prefetched batch. Skipping those
    # would return ten perfectly writable messages to ready on every restart and
    # log ten "redelivered" warnings, draining that warning of the meaning it
    # was added to carry.
    storage_down = False

    def reject(method, reason: str, exc: Exception, channel) -> None:
        """Dead-letter one message, or -- with --requeue-poison -- do not.

        basic_reject, not basic_nack. Both dead-letter identically and both
        produce x-death reason "rejected"; the difference is that basic.reject
        is AMQP 0-9-1 core while basic.nack is a RabbitMQ extension, negotiated
        per connection (pika exposes it as channel.basic_nack_supported). The
        only thing nack adds is `multiple`, for rejecting a batch by delivery
        tag -- which this never does, and which under this project's
        explicit-over-inherited rule would have to be spelled out as
        multiple=False for no gain.

        The reason string only ever reaches this log line. x-death records the
        *broker's* reason for the death ("rejected"), and RabbitMQ gives no way
        to attach the application's, so a message sitting in telemetry.dlq does
        not say whether it was truncated or merely off-contract.
        """
        log.warning(
            "poison message, %s: reason=%s delivery_tag=%d redelivered=%s (%s)",
            "REQUEUED (--requeue-poison)" if requeue_poison else "dead-lettered",
            reason,
            method.delivery_tag,
            method.redelivered,
            exc,
        )
        # requeue=False is the whole of stage 7: the message leaves this queue
        # and routes through telemetry.dlx. requeue=True is the pathology the
        # flag exists to demonstrate -- it goes straight back, is redelivered
        # immediately, fails to parse again, and loops as fast as the link
        # allows. Note it leaves NO x-death: that header is written only on an
        # actual dead-letter, so the loop produces nothing to inspect.
        channel.basic_reject(
            delivery_tag=method.delivery_tag, requeue=requeue_poison
        )

    def store(payload: dict, seq: object) -> bool:
        """Write one reading, retrying a bounded number of times.

        Returns True if the point was confirmed written, False if the budget ran
        out or a shutdown interrupted it. Never raises: the caller's job is to
        decide what an unwritten message means for the delivery, and that
        decision is stage 11's.

        The backoff sleeps after EVERY failed attempt, the last one included.
        That final sleep is not decoration -- without it the requeue below comes
        straight back as a redelivery with nothing pacing it, and a storage
        outage becomes the same hot loop --requeue-poison exists to demonstrate.
        """
        nonlocal storage_down

        if storage_down and is_stopping():
            # Shutting down, and the previous message already proved storage is
            # not answering. Trying anyway costs one write timeout per
            # prefetched delivery, which is exactly what overran the grace
            # period when this was measured. Leave it unacknowledged instead.
            log.info(
                "shutting down, storage still down: seq=%s left unacknowledged", seq
            )
            return False

        attempts = settings.influxdb_write_attempts
        delay = settings.influxdb_write_retry_delay

        for attempt in range(1, attempts + 1):
            try:
                storage.write(write_api, storage.to_point(payload))
                storage_down = False
                return True
            except storage.WRITE_FAILURES as exc:
                # Two bases, and the tuple is storage.py's because the reason is
                # a verified library detail: with retries=False a refused
                # connection surfaces as urllib3's own NewConnectionError, not
                # as an InfluxDBError. Catching only the library's exception
                # would miss a stopped container -- the exact case this stage's
                # failure test produces.
                log.warning(
                    "storage write failed (attempt %d of %d) for seq=%s: %s: %s",
                    attempt,
                    attempts,
                    seq,
                    type(exc).__name__,
                    exc,
                )

            if not sleep_unless_stopping(delay):
                # SIGTERM arrived mid-backoff. Give up immediately and leave the
                # delivery unacknowledged: stop_consuming() returns it to ready.
                # This is what keeps shutdown inside compose's 10s grace -- a
                # bare sleep here would cost prefetch x the whole retry budget.
                log.info("shutting down mid-retry, seq=%s left unacknowledged", seq)
                return False
            delay *= 2

        storage_down = True
        return False

    def on_message(channel, method, properties, body: bytes) -> None:
        try:
            payload = contract.parse(body)
        except json.JSONDecodeError as exc:
            # Not JSON at all -- what publisher's --corrupt-every produces.
            reject(method, "malformed JSON", exc, channel)
            return
        except ContractViolation as exc:
            # JSON, but not this contract. Validated here rather than at stage
            # 11 so that stage's failure test stays about the storage container
            # being down, with no second variable.
            reject(method, "off-contract", exc, channel)
            return

        seq = payload[FIELD_SEQ]

        if ack_delay is not None:
            # --ack-delay only. Blocking on purpose: it holds the delivery in
            # the unacknowledged window so the prefetch cap becomes visible.
            #
            # Note what this is doing to the connection -- sleeping here stalls
            # pika's I/O loop, so no heartbeats go out while it sleeps. At the
            # couple of seconds stage 6 needs that is harmless; past the
            # heartbeat interval (explicit at amqp.HEARTBEAT_SECONDS = 60 from
            # stage 11) it reproduces a broker disconnect with no apparent
            # cause -- the hazard the storage write below was designed around.
            time.sleep(ack_delay)

        # Stage 11, and the order of these next few lines IS the stage. The
        # write comes after the parse and strictly before the ack: acknowledging
        # first would lose the message if this process died in between, while
        # acknowledging after means such a death redelivers it. That is what
        # makes the pipeline at-least-once end to end.
        if not store(payload, seq):
            if is_stopping():
                # Leave it unacknowledged and let stop_consuming() put it back.
                # Returning here is also what lets the rest of the prefetched
                # batch fall through in microseconds instead of one retry budget
                # each.
                return

            # Storage is down and the budget is spent. Requeue rather than
            # dead-letter: telemetry.dlq means "this message failed the payload
            # contract", and nothing else. A good reading that happened to
            # arrive during an outage is not poison, x-death has no room to say
            # which it was, and a bounded-then-dead-letter policy would start
            # discarding valid data the moment an outage outlasted the budget.
            #
            # Nothing is lost by requeueing: the message returns to ready, the
            # queue grows as backpressure, and telemetry.observe carries on
            # untouched -- which is the practical payoff of the fan-out.
            log.error(
                "storage unavailable, requeueing: seq=%s delivery_tag=%d",
                seq,
                method.delivery_tag,
            )
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=True)
            return

        if method.redelivered:
            # On the success path deliberately, not before the write. Every
            # requeue above comes back with redelivered set, so logging earlier
            # would fill an outage with lines about writes that never happened.
            # Here it says something precise and useful at stage 18: this point
            # was written on a redelivery, so it may have overwritten an
            # identical one. Harmless -- point identity is measurement + tag set
            # + timestamp, and all three are unchanged -- but it is the only
            # evidence that the duplicate window was exercised.
            log.warning("redelivered, point overwritten in place: seq=%s", seq)

        channel.basic_ack(delivery_tag=method.delivery_tag)

        # seq=<int> as a bare token, matching consumer_observe and the
        # publisher, so `grep -o 'seq=[0-9]*'` compares the paths directly.
        log.info("stored: seq=%s", seq)

    return on_message


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ack-delay",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help=(
            "wait this long before acknowledging each message -- stage 6's "
            "proof that the unacknowledged count pins at the prefetch limit. "
            "A hand-run experiment; not reachable from `docker compose up`"
        ),
    )
    parser.add_argument(
        "--requeue-poison",
        action="store_true",
        help=(
            "requeue unparseable messages instead of dead-lettering them, "
            "reproducing the infinite redelivery loop on purpose. Pins a CPU "
            "core until interrupted, and writes no x-death at all -- the "
            "absence of evidence is the lesson. Ctrl-C to stop"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()

    if args.ack_delay is not None:
        log.warning(
            "--ack-delay %.1fs: expect unacked to pin at %d",
            args.ack_delay,
            settings.consumer_prefetch_count,
        )

    if args.requeue_poison:
        log.error(
            "--requeue-poison: poison messages will be REQUEUED, not "
            "dead-lettered. Expect an unbounded redelivery loop and a pinned "
            "CPU core. telemetry.dlq will stay empty. Ctrl-C to stop."
        )

    try:
        client = storage.connect()
    except storage.StorageUnconfigured as exc:
        log.error("%s", exc)
        return EXIT_UNCONFIGURED

    # Logged at startup for the same reason topology.py logs its two specs: the
    # schema is otherwise invisible until something queries it, and a wrong
    # measurement name produces an empty dashboard rather than an error.
    log.info("storage schema:\n%s", storage.describe())
    log.info("writing to %s, bucket %s", settings.influxdb_url, settings.influxdb_bucket)

    try:
        return run_consumer(
            spec.QUEUE_STORE,
            build_handler(args.ack_delay, args.requeue_poison, storage.write_api(client)),
            auto_ack=False,
            prefetch_count=settings.consumer_prefetch_count,
        )
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
