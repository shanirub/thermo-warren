"""One logging configuration, shared by every module.

Exists as a shared module for one concrete reason: pika logs a line per AMQP
frame at DEBUG and will bury your own output. Pinning it has to happen in the
declarer, the publisher and both consumers, and four copies of that line would
drift. Everything else here is convenience.

Console only -- no file handlers. Docker captures stdout and stderr, and the
Dockerfile sets PYTHONUNBUFFERED=1 so container output streams immediately
rather than appearing only when the process exits.
"""

import logging

from telemetry.config import settings

# asctime is left at its default format so it keeps milliseconds: ordering two
# log lines that arrive in the same second is a real need once a publisher and
# two consumers are running at once.
_FORMAT = "%(asctime)s %(levelname)-8s %(name)-26s %(message)s"


def configure_logging() -> None:
    """Configure the root logger. Call once, at the top of main()."""
    logging.basicConfig(
        level=settings.log_level,
        format=_FORMAT,
        # basicConfig is a no-op if the root logger already has handlers, which
        # silently ignores the second call under pytest. force=True makes the
        # call authoritative instead.
        force=True,
    )

    # Raise with LOG_LEVEL=DEBUG plus this line edited, when you need to see
    # the frames themselves. Not something to leave on.
    logging.getLogger("pika").setLevel(logging.WARNING)
