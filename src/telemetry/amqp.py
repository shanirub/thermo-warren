"""Shared AMQP plumbing: connecting, and running a long-lived consumer.

Split out at stage 6. `connect()` was written for `topology.py` at stage 4 and
both consumers need it unchanged -- along with the auth-error ordering and the
pika-noise suppression wrapped around it. Three copies of that would drift,
which is the same reason `logging_setup.py` is shared rather than duplicated.

The division of labour against `topology.py`: this module knows how to reach
the broker and how to stay attached to a queue. It knows nothing about which
queues exist or what arguments they carry -- that is `topology_spec.py`,
declared by `topology.py`.

Exit codes are defined here because `run_consumer()` returns them. `topology.py`
imports the three shared ones and adds its own `EXIT_MISMATCH = 4`, which is
meaningless for a consumer.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pika
from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection
from pika.exceptions import (
    AMQPConnectionError,
    ChannelClosedByBroker,
    ConnectionWrongStateError,
    ProbableAccessDeniedError,
    ProbableAuthenticationError,
)

from telemetry.config import settings

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNREACHABLE = 2
EXIT_REJECTED = 3

# Ceiling on the reconnect backoff. 30s deliberately mirrors the firmware's
# Wi-Fi reconnect cap (see firmware/CLAUDE.md), so the two halves of the project
# take the same worst-case time to notice a network has come back. The floor is
# settings.rabbitmq_connect_retry_delay, shared with the initial connect.
RECONNECT_MAX_DELAY_SECONDS = 30.0

# Signature pika calls an on_message_callback with. Spelled out because the
# consumers' handlers must match it exactly and a mismatch surfaces only at
# delivery time, not at import.
MessageHandler = Callable[
    [BlockingChannel, pika.spec.Basic.Deliver, pika.spec.BasicProperties, bytes],
    None,
]


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


def connect() -> BlockingConnection:
    """Open a connection, retrying only on failures that waiting can fix.

    The compose healthcheck (`check_port_connectivity`) is liveness, not
    readiness -- it opens a socket with no handshake and no auth -- and in host
    mode there is no healthcheck gate at all. Hence the loop.

    Bounded, deliberately. A consumer that has never connected should fail
    loudly naming the endpoint rather than loop forever on a typo; the unbounded
    part lives in run_consumer(), which only reaches for it after one successful
    connection has proved the configuration is right.
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


@dataclass
class _Session:
    """One connection's worth of state, replaced on every reconnect.

    The signal handler needs whichever connection and channel are live *now*,
    and those change each time the loop goes round, so they cannot be closed
    over directly.
    """

    connection: BlockingConnection | None = None
    channel: BlockingChannel | None = None
    # Set by the on-cancel callback. start_consuming() returns normally when the
    # broker cancels us (see _consume below), so without this flag a deleted
    # queue is indistinguishable from a clean shutdown.
    cancelled_by_broker: bool = False


def _consume(
    session: _Session,
    queue: str,
    on_message: MessageHandler,
    *,
    auto_ack: bool,
    prefetch_count: int | None,
) -> None:
    """Attach to `queue` and block until cancelled, or until the link drops."""
    connection = session.connection
    assert connection is not None
    channel = connection.channel()
    session.channel = channel
    session.cancelled_by_broker = False

    if prefetch_count is None:
        # Not an oversight: basic_qos is IGNORED on a channel using automatic
        # acknowledgment, because the broker considers a message acknowledged
        # the moment it writes it to the socket -- there is no unacknowledged
        # window to bound. Passing None makes "this consumer has no
        # backpressure" a decision visible at the call site.
        log.info("no prefetch limit: %s consumes without flow control", queue)
    else:
        # global_qos=False stated explicitly though it is pika's default: the
        # limit is per-consumer, not shared across the connection.
        channel.basic_qos(prefetch_count=prefetch_count, global_qos=False)
        log.info("prefetch_count=%d on %s", prefetch_count, queue)

    def on_broker_cancel(method_frame) -> None:
        # RabbitMQ sends Basic.Cancel when the queue we are consuming is
        # deleted -- which is exactly what `topology.py --recreate` does at
        # stages 8 and 9, to these queues, while this consumer is attached.
        # Logged at ERROR because the alternative is a consumer that stops
        # receiving with nothing anywhere saying why.
        session.cancelled_by_broker = True
        log.error(
            "broker cancelled our consumer on %s (queue deleted?): %s",
            queue,
            method_frame.method,
        )

    channel.add_on_cancel_callback(on_broker_cancel)
    channel.basic_consume(
        queue=queue,
        on_message_callback=on_message,
        auto_ack=auto_ack,
    )
    log.info("consuming from %s (auto_ack=%s)", queue, auto_ack)

    # Blocks until every consumer on the channel is gone -- either because we
    # cancelled it from the signal handler, or because the broker did.
    channel.start_consuming()


def run_consumer(
    queue: str,
    on_message: MessageHandler,
    *,
    auto_ack: bool,
    prefetch_count: int | None,
) -> int:
    """Consume `queue` until told to stop, reconnecting for as long as it takes.

    pika's BlockingConnection has no automatic reconnect; connection_attempts
    and retry_delay govern only the initial connect. So the loop is ours.

    Unbounded on purpose, and only after the first connection succeeds. A
    consumer that exits on a dropped link would have to be restarted by hand in
    host mode, where there is no compose restart policy to catch it -- and the
    project requires host mode for the fast edit-debug loop.
    """
    stopping = threading.Event()
    session = _Session()

    def handle_signal(signum, _frame) -> None:
        log.info("received signal %d, shutting down", signum)
        stopping.set()
        connection, channel = session.connection, session.channel
        if connection is None or channel is None:
            return
        # NOT channel.stop_consuming() directly. This runs on the main thread
        # between bytecodes, possibly mid-way through pika's own socket I/O,
        # and stop_consuming() issues a Basic.Cancel and flushes output.
        # add_callback_threadsafe only enqueues, and pika dispatches it at a
        # point where re-entering is safe.
        #
        # Note what a clean stop then does, per stop_consuming()'s docstring:
        # pending ackable messages are rejected (back to ready), pending
        # non-ackable ones are lost. Same asymmetry as an unclean kill, for the
        # same reason -- which is the point of the two paths differing.
        try:
            connection.add_callback_threadsafe(channel.stop_consuming)
        except ConnectionWrongStateError:
            # Already closing; the loop below sees `stopping` and exits anyway.
            pass

    # Compose sends SIGTERM on `down`; SIGINT is Ctrl-C in host mode.
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    floor = settings.rabbitmq_connect_retry_delay
    delay = floor
    connected_once = False

    while not stopping.is_set():
        try:
            session.connection = connect()
        except CredentialsRejected as exc:
            log.error("%s", exc)
            return EXIT_REJECTED
        except BrokerUnreachable as exc:
            if not connected_once:
                # Never reached the broker at all: a wrong hostname, a wrong
                # port, or a broker that was never started. Fail loudly naming
                # the endpoint rather than retrying a configuration error
                # forever.
                log.error("%s", exc)
                return EXIT_UNREACHABLE
            log.warning("%s; retrying in %.1fs", exc, delay)
            stopping.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
            continue

        connected_once = True
        delay = floor  # a good connection resets the backoff to the bottom

        try:
            _consume(
                session,
                queue,
                on_message,
                auto_ack=auto_ack,
                prefetch_count=prefetch_count,
            )
        except (ProbableAuthenticationError, ProbableAccessDeniedError) as exc:
            # Above the broad handler, exactly as in connect(): both subclass
            # AMQPConnectionError, and no amount of waiting fixes a permission.
            log.error("broker rejected us mid-session: %s", exc)
            return EXIT_REJECTED
        except AMQPConnectionError as exc:
            if stopping.is_set():
                break  # our own close, on the way out -- not a failure
            log.warning(
                "connection lost: %s; reconnecting in %.1fs",
                str(exc) or type(exc).__name__,
                delay,
            )
            stopping.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
            continue
        except ChannelClosedByBroker as exc:
            # 404 NOT_FOUND on basic_consume: the queue is gone. Expected in the
            # window while `--recreate` is deleting and redeclaring, so retry
            # rather than exit -- but say so, because if the queue never comes
            # back this line repeating is the only evidence.
            log.error(
                "broker closed the channel: %s %s; retrying in %.1fs",
                exc.reply_code,
                exc.reply_text,
                delay,
            )
            stopping.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
            continue
        finally:
            connection = session.connection
            session.channel = None
            session.connection = None
            if connection is not None and connection.is_open:
                with contextlib.suppress(AMQPConnectionError):
                    connection.close()

        # start_consuming() returned without raising. Two ways that happens:
        if session.cancelled_by_broker:
            # The queue was deleted under us. on_broker_cancel already logged
            # it; reattach once it exists again.
            stopping.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
            continue
        # Otherwise we cancelled ourselves from the signal handler.
        break

    log.info("stopped consuming from %s", queue)
    return EXIT_OK
