"""One-shot topology declarer.

Declares the dead-letter exchange, all three queues and all three bindings,
then exits. Must complete before anything publishes: a topic exchange with no
matching binding discards messages silently -- no error, no failed PUBACK,
nothing in the broker log. Compose enforces that ordering with
`condition: service_completed_successfully` rather than leaving it to
discipline.

Idempotent: a second run with unchanged arguments is a no-op, because a declare
matching an existing queue succeeds and returns its current state.

Not idempotent across an argument change. RabbitMQ answers a declare with
different arguments with PRECONDITION_FAILED (406) rather than updating the
queue, so stages 8 and 9 need `--recreate`, which deletes and redeclares.

Exit codes:
    0  topology declared
    2  broker unreachable after every attempt
    3  broker rejected our credentials or vhost permissions
    4  a queue exists with different arguments; rerun with --recreate
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import time

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import (
    AMQPConnectionError,
    ChannelClosedByBroker,
    ProbableAccessDeniedError,
    ProbableAuthenticationError,
)

from telemetry import topology_spec as spec
from telemetry.config import settings
from telemetry.logging_setup import configure_logging

log = logging.getLogger(__name__)

PRECONDITION_FAILED = 406

EXIT_OK = 0
EXIT_UNREACHABLE = 2
EXIT_REJECTED = 3
EXIT_MISMATCH = 4


class BrokerUnreachable(RuntimeError):
    """Every connection attempt failed."""


class CredentialsRejected(RuntimeError):
    """The broker refused our credentials or vhost permissions."""


@contextlib.contextmanager
def _quiet_pika_connect_errors():
    """Silence pika's own traceback for one connection attempt.

    A refused connection while the broker is still starting is expected here,
    and the retry loop below already reports it in one line. pika reports the
    same failure at ERROR several times over, across child loggers, with a
    traceback -- which buries the line that matters. Silencing the "pika" parent
    covers the children, since they carry no level of their own. Scoped to the
    attempt, so genuine pika errors elsewhere are still visible.
    """
    logger = logging.getLogger("pika")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def connect() -> pika.BlockingConnection:
    """Open a connection, retrying only on failures that waiting can fix.

    The compose healthcheck (`check_port_connectivity`) is liveness, not
    readiness -- it opens a socket with no handshake and no auth -- and in host
    mode there is no healthcheck gate at all. Hence the loop.
    """
    params = pika.ConnectionParameters(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_amqp_port,
        virtual_host=settings.rabbitmq_vhost,
        credentials=pika.PlainCredentials(
            settings.rabbitmq_user, settings.rabbitmq_password
        ),
        # Retry is owned by the loop below, not by pika. Stated explicitly even
        # though it is the default: raising it later would nest pika's retries
        # inside ours and multiply the attempts, with only the outer loop
        # visible in the log.
        connection_attempts=1,
    )

    attempts = settings.rabbitmq_connect_attempts
    delay = settings.rabbitmq_connect_retry_delay

    for attempt in range(1, attempts + 1):
        log.info(
            "connecting to %s (attempt %d of %d)",
            settings.amqp_url,
            attempt,
            attempts,
        )
        try:
            with _quiet_pika_connect_errors():
                return pika.BlockingConnection(params)
        except (ProbableAuthenticationError, ProbableAccessDeniedError) as exc:
            # Both subclass AMQPConnectionError, so this block must stay above
            # the broad one or it will never be reached. Waiting cannot fix a
            # wrong password or a missing permission, so fail immediately
            # rather than burning the full retry budget.
            raise CredentialsRejected(
                f"broker rejected user {settings.rabbitmq_user!r} on vhost "
                f"{settings.rabbitmq_vhost!r}: {exc}"
            ) from exc
        except AMQPConnectionError as exc:
            # pika often raises this with an empty message; fall back to the
            # class name rather than logging a bare "failed:".
            log.warning(
                "attempt %d of %d failed: %s",
                attempt,
                attempts,
                str(exc) or type(exc).__name__,
            )
            if attempt < attempts:
                time.sleep(delay)

    raise BrokerUnreachable(
        f"no connection to {settings.amqp_url} after {attempts} attempts"
    )


def drop(channel: BlockingChannel) -> None:
    """Delete the queues whose arguments change between stages.

    Deliberately without if_empty/if_unused guards: --recreate is reached for at
    stages 8 and 9, which are exactly the stages where queues have been allowed
    to fill up. A guard would refuse precisely when the operation is needed.
    The safety comes from this being an explicit flag, unreachable from a normal
    `docker compose up`.
    """
    for queue in spec.RECREATABLE:
        log.warning("--recreate: deleting queue %s", queue)
        channel.queue_delete(queue=queue)


def declare(channel: BlockingChannel) -> None:
    """Declare every exchange, queue and binding this project owns.

    Takes a channel rather than opening its own connection, so the whole
    topology can be asserted against a mock. The arguments dicts *are* the
    policy, which makes them the thing worth testing.
    """
    # Dead-letter side first. telemetry.store names this exchange below, and
    # dead-lettering follows normal routing rules -- an exchange with no
    # matching binding discards silently, so an unbound DLX is a hole in the
    # floor rather than a safety net.
    log.info("declaring exchange %s (type=%s)", spec.DLX, spec.DLX_TYPE)
    channel.exchange_declare(
        exchange=spec.DLX,
        exchange_type=spec.DLX_TYPE,
        durable=True,
        auto_delete=False,
    )

    log.info("declaring queue %s args=%s", spec.QUEUE_DLQ, spec.DLQ_ARGS)
    channel.queue_declare(
        queue=spec.QUEUE_DLQ,
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments=spec.DLQ_ARGS,
    )
    channel.queue_bind(
        queue=spec.QUEUE_DLQ,
        exchange=spec.DLX,
        routing_key=spec.DLQ_BINDING_KEY,
    )

    # Durable path.
    log.info("declaring queue %s args=%s", spec.QUEUE_STORE, spec.STORE_ARGS)
    channel.queue_declare(
        queue=spec.QUEUE_STORE,
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments=spec.STORE_ARGS,
    )
    channel.queue_bind(
        queue=spec.QUEUE_STORE,
        exchange=spec.SOURCE_EXCHANGE,
        routing_key=spec.ROUTING_KEY,
    )

    # Observation path. Same exchange, same routing key -- that identity is the
    # fan-out. Different arguments -- that asymmetry is the lesson.
    log.info("declaring queue %s args=%s", spec.QUEUE_OBSERVE, spec.OBSERVE_ARGS)
    channel.queue_declare(
        queue=spec.QUEUE_OBSERVE,
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments=spec.OBSERVE_ARGS,
    )
    channel.queue_bind(
        queue=spec.QUEUE_OBSERVE,
        exchange=spec.SOURCE_EXCHANGE,
        routing_key=spec.ROUTING_KEY,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--recreate",
        action="store_true",
        help=(
            "delete and redeclare "
            + ", ".join(spec.RECREATABLE)
            + " (destructive; the dead-letter queue is left alone)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()

    log.info("resolved configuration:\n%s", settings.describe())
    log.info("topology to declare:\n%s", spec.describe())

    try:
        connection = connect()
    except CredentialsRejected as exc:
        log.error("%s", exc)
        return EXIT_REJECTED
    except BrokerUnreachable as exc:
        log.error("%s", exc)
        return EXIT_UNREACHABLE

    try:
        channel = connection.channel()
        if args.recreate:
            drop(channel)
        declare(channel)
    except ChannelClosedByBroker as exc:
        if exc.reply_code == PRECONDITION_FAILED:
            log.error(
                "a queue already exists with arguments different from the ones "
                "declared here.\n"
                "  RabbitMQ said: %s\n"
                "  Rerun with --recreate to delete and redeclare %s.",
                exc.reply_text,
                ", ".join(spec.RECREATABLE),
            )
            return EXIT_MISMATCH
        log.error("broker closed the channel: %s %s", exc.reply_code, exc.reply_text)
        return EXIT_MISMATCH
    finally:
        if connection.is_open:
            connection.close()

    log.info("topology declared. Rerun is a no-op; --recreate is destructive.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
