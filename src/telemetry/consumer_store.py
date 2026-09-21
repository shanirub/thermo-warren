"""Durable-path consumer.

Manual acknowledgment, bounded prefetch, dead-letter exchange attached. Parses
and acknowledges from stage 6; rejects anything that fails the payload contract
from stage 7, without requeueing, so it dead-letters through telemetry.dlx. The
InfluxDB write is inserted between the parse and the acknowledgment at stage 11,
and where exactly it goes is the whole lesson of that stage.

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

from telemetry import payload as contract
from telemetry import topology_spec as spec
from telemetry.amqp import run_consumer
from telemetry.config import settings
from telemetry.logging_setup import configure_logging
from telemetry.payload import FIELD_SEQ, ContractViolation

log = logging.getLogger(__name__)


def build_handler(ack_delay: float | None, requeue_poison: bool):
    """Return the on_message callback, closed over the experiment flags.

    A closure rather than module-level globals, so the flags are visible in the
    call chain and the handler stays assertable against a mock channel.
    """

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

    return run_consumer(
        spec.QUEUE_STORE,
        build_handler(args.ack_delay, args.requeue_poison),
        auto_ack=False,
        prefetch_count=settings.consumer_prefetch_count,
    )


if __name__ == "__main__":
    sys.exit(main())
