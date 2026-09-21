"""Observation-path consumer.

Reads the bounded, deliberately lossy queue that has no dead-letter exchange.
Logs only -- no acknowledgment, no flow control, no storage, ever.

The asymmetry against `consumer_store.py` is the point of the fan-out, and it
is deliberately expressed in *two* places rather than one:

  * the queue drops its own head at x-max-length (topology_spec.py), and
  * this consumer acknowledges automatically, so it has no backpressure.

That means one unclean kill produces two different outcomes on the same
messages -- `telemetry.store`'s unacknowledged window returns to ready,
`telemetry.observe`'s in-flight deliveries are simply gone.

Exit codes:
    0  clean exit (SIGTERM/SIGINT received)
    2  could not reach the broker on the first attempt
    3  broker rejected our credentials

Implemented at stage 6.
"""

from __future__ import annotations

import json
import logging
import sys

from telemetry import topology_spec as spec
from telemetry.amqp import run_consumer
from telemetry.logging_setup import configure_logging

log = logging.getLogger(__name__)

# Only field this consumer reads. It does not validate the rest of the payload
# contract: this path exists to show what arrived, not to judge it.
FIELD_SEQ = "seq"


def on_message(channel, method, properties, body: bytes) -> None:
    """Log the delivery and return. There is nothing to acknowledge.

    No basic_ack call anywhere in this module, deliberately: the channel uses
    automatic acknowledgment, so the broker considered this message done the
    moment it wrote it to the socket. Adding an ack here would raise a channel
    error rather than being harmlessly redundant.
    """
    try:
        seq = json.loads(body)[FIELD_SEQ]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        # Same bytes that dead-letter on the durable path are merely noted
        # here, because this queue has no dead-letter exchange to send them to.
        # That contrast is stage 7's demonstration; this line is its other half.
        log.warning("unparseable payload, logged and dropped: %s", exc)
        return

    # seq=<int> as a bare token, so `grep -o 'seq=[0-9]*'` over this log and
    # consumer_store's compares the two paths directly.
    log.info("observed: seq=%s", seq)


def main() -> int:
    configure_logging()
    return run_consumer(
        spec.QUEUE_OBSERVE,
        on_message,
        auto_ack=True,
        # None, not 0: "do not call basic_qos at all". basic_qos is ignored on
        # an auto-ack channel anyway -- there is no unacknowledged window to
        # bound -- so calling it would imply a limit that does not exist.
        prefetch_count=None,
    )


if __name__ == "__main__":
    sys.exit(main())
