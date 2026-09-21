"""Durable-path consumer.

Manual acknowledgment, bounded prefetch, dead-letter exchange attached. Parses
and acknowledges from stage 6; the InfluxDB write is inserted between the parse
and the acknowledgment at stage 11, and where exactly it goes is the whole
lesson of that stage.

Exit codes:
    0  clean exit (SIGTERM/SIGINT received)
    2  could not reach the broker on the first attempt
    3  broker rejected our credentials

Implemented at stage 6, extended at stages 7-9 and 11.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from telemetry import topology_spec as spec
from telemetry.amqp import run_consumer
from telemetry.config import settings
from telemetry.logging_setup import configure_logging

log = logging.getLogger(__name__)

FIELD_SEQ = "seq"


def build_handler(ack_delay: float | None):
    """Return the on_message callback, closed over the ack delay.

    A closure rather than a module-level global, so the delay is visible in the
    call chain and the handler stays assertable against a mock channel.
    """

    def on_message(channel, method, properties, body: bytes) -> None:
        try:
            payload = json.loads(body)
            seq = payload[FIELD_SEQ]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # Stage 7 turns this single call into
            # basic_nack(delivery_tag, requeue=False), which routes the message
            # to telemetry.dlx. Until then the message is consumed and
            # discarded, and this line is its only trace -- acknowledged here
            # rather than left unacknowledged, because an unacknowledged poison
            # message holds a prefetch slot forever and ten of them wedge this
            # consumer permanently.
            log.warning(
                "unparseable payload, acked and dropped: delivery_tag=%d (%s)",
                method.delivery_tag,
                exc,
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        if ack_delay is not None:
            # --ack-delay only. Blocking on purpose: it holds the delivery in
            # the unacknowledged window so the prefetch cap becomes visible.
            #
            # Note what this is doing to the connection -- sleeping here stalls
            # pika's I/O loop, so no heartbeats go out while it sleeps. At the
            # couple of seconds stage 6 needs that is harmless; past the
            # negotiated heartbeat interval it reproduces a broker disconnect
            # with no apparent cause, which is the hazard stage 11 has to design
            # the storage write around.
            time.sleep(ack_delay)

        # Stage 11 inserts the InfluxDB write HERE -- after the parse, before
        # the ack. Acknowledging first means a crash between the two loses the
        # message; acknowledging after means a crash redelivers it, which is
        # what makes the pipeline at-least-once end to end.
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

    return run_consumer(
        spec.QUEUE_STORE,
        build_handler(args.ack_delay),
        auto_ack=False,
        prefetch_count=settings.consumer_prefetch_count,
    )


if __name__ == "__main__":
    sys.exit(main())
