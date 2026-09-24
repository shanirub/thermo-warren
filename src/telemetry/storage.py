"""Shared InfluxDB plumbing: connecting, and turning a payload into a point.

Added at stage 11. The division of labour mirrors `amqp.py` exactly: this module
knows how to reach the storage and how to write one reading to it. It knows
nothing about queues, acknowledgment or retry policy -- when a write fails it
raises, and `consumer_store.py` decides what that means for the delivery.

**The key names are not spelled here.** Every one is imported from `payload.py`,
which already owns the five field names as the frozen wire contract. Re-spelling
them would let the storage schema drift from the contract silently: a renamed
field would still write, just into a differently-named column, and nothing would
report an error. Only three things are genuinely new and they are the constants
below -- the measurement name, which payload fields are tags rather than fields,
and the write precision.

`config.py` holds the endpoint, org, bucket and token, because those differ
between run modes. These do not.
"""

from __future__ import annotations

import logging

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.exceptions import InfluxDBError
from influxdb_client.client.write_api import SYNCHRONOUS, WriteApi
from urllib3.exceptions import HTTPError

from telemetry.config import settings
from telemetry.payload import (
    FIELD_DEVICE,
    FIELD_HUMIDITY_PCT,
    FIELD_SEQ,
    FIELD_TEMP_C,
    FIELD_TS_MS,
)

log = logging.getLogger(__name__)

# --- The storage schema -----------------------------------------------------

# Not "telemetry", which already names the bucket, the queue prefix and (from
# stage 12) the InfluxQL database exposed by the DBRP mapping. A measurement of
# the same name would read as `FROM telemetry` in database `telemetry` in every
# query. "readings" names what one row is, the same principle that named the
# queues after what their consumers do.
MEASUREMENT = "readings"

# device is the only tag. Tag values are indexed and every distinct tag set is a
# separate series, so the tag set decides cardinality: this one gives exactly one
# series per publisher, forever.
TAG_FIELDS = (FIELD_DEVICE,)

# seq is a FIELD, deliberately. It is monotonic and unbounded, so tagging it
# would create one series per message -- ~86,400 a day at 1 Hz -- against a
# bucket whose retention is infinite. As a field it is still selectable and
# still the gap-and-duplicate evidence stage 18 wants.
VALUE_FIELDS = (FIELD_TEMP_C, FIELD_HUMIDITY_PCT, FIELD_SEQ)

# ts_ms is neither a tag nor a field: it becomes the point's own timestamp.
TIMESTAMP_FIELD = FIELD_TS_MS

# MUST be passed to write(), not merely to Point.time(). **Verified against
# 1.50.0**: WriteApi.write's write_precision parameter defaults to 'ns', and the
# Point's own precision does NOT rewrite the number -- an int timestamp is
# serialized verbatim at every precision, so the only thing that assigns meaning
# to 1756400000123 is the query parameter this value becomes. Left at the
# default it would be read as nanoseconds and every point would land in January
# 1970, with no error anywhere.
WRITE_PRECISION = WritePrecision.MS


class StorageUnconfigured(RuntimeError):
    """No token was supplied, so there is nothing to authenticate with."""


def connect() -> InfluxDBClient:
    """Build the client. Does no I/O -- the first write is the first request.

    Unlike amqp.connect() there is no retry loop here, and deliberately so: the
    consumer's retry budget belongs to the write, which is where a storage
    outage actually shows up. A client constructed against a dead server
    succeeds; it is write() that fails.
    """
    if not settings.influxdb_token:
        # The one place "fail loudly by name" survives for these settings. The
        # fields carry defaults so that topology, publisher and consumer_observe
        # can start without a token they never use, which means an empty token
        # has to be caught here instead of by pydantic -- in the one process
        # that needs it, naming the variable the operator has to set.
        raise StorageUnconfigured(
            "INFLUXDB_TOKEN is empty: consumer_store cannot authenticate to "
            f"{settings.influxdb_url}"
        )

    return InfluxDBClient(
        url=settings.influxdb_url,
        token=settings.influxdb_token,
        org=settings.influxdb_org,
        # Milliseconds, per the constructor's own docstring. Explicit at a fifth
        # of the library default of 10_000, because this blocks the AMQP I/O
        # loop -- see config.influxdb_timeout_ms.
        timeout=settings.influxdb_timeout_ms,
        # Explicit although False is already the default (verified:
        # `self.retries = kwargs.get('retries', False)` in client/_base.py).
        # The retry loop in consumer_store is the only one there should be; a
        # urllib3 policy underneath it would multiply the attempts and bend the
        # backoff curve, with only the outer loop visible in the log. Exactly
        # the argument that pins connection_attempts=1 in amqp.connect().
        retries=False,
    )


def write_api(client: InfluxDBClient) -> WriteApi:
    """The synchronous write API.

    SYNCHRONOUS is not a tuning choice, it is the stage's requirement. The
    batching API returns as soon as the point is buffered and flushes later on
    its own thread, so acknowledging after that call would acknowledge before
    any confirmation exists. It would also fail *silently*: points appear
    normally and only go missing during an outage, which is precisely the
    failure this stage exists to make visible.
    """
    return client.write_api(write_options=SYNCHRONOUS)


def to_point(payload: dict) -> Point:
    """Map one validated payload onto one InfluxDB point.

    Takes the output of payload.parse(), so every key is present and every type
    already checked. This does no validation of its own.
    """
    point = Point(MEASUREMENT)

    for name in TAG_FIELDS:
        point.tag(name, payload[name])

    # float() on the readings, int() on seq. **This is what settles the
    # tolerance payload.parse() carries**: parse() accepts a JSON integer for
    # temp_c / humidity_pct, because json.loads("29") is an int and a strict
    # float check would dead-letter good readings after a publisher's
    # format-string change. Verified in 1.50.0 that the tolerance would
    # otherwise reach storage: Point.field("temp_c", 29) serializes as
    # `temp_c=29i`, an *integer* field, while float(29) serializes as `temp_c=29`
    # -- and InfluxDB field types are fixed by first write, so an integer
    # arriving at a float field is rejected for the whole batch.
    #
    # Coercing here keeps that tolerance a dead-lettering decision, where stage 7
    # put it, rather than quietly making it a storage one.
    point.field(FIELD_TEMP_C, float(payload[FIELD_TEMP_C]))
    point.field(FIELD_HUMIDITY_PCT, float(payload[FIELD_HUMIDITY_PCT]))
    point.field(FIELD_SEQ, int(payload[FIELD_SEQ]))

    # Publisher-stamped, in milliseconds, and the reason is point identity:
    # measurement + tag set + timestamp. Two points sharing all three overwrite
    # rather than accumulate, which is exactly what makes an at-least-once
    # redelivery idempotent here. It is also why consumer_store does no
    # deduplication of its own.
    point.time(payload[TIMESTAMP_FIELD], WRITE_PRECISION)

    return point


def write(api: WriteApi, point: Point) -> None:
    """Write one point, or raise.

    Raises on any failure -- there is no return value to check. **Verified
    against 1.50.0**, because the exception types are what the caller's retry
    loop has to catch and they are not all the library's own:

        server refused the connection  -> urllib3.exceptions.NewConnectionError
        accepted but never answered    -> urllib3.exceptions.ReadTimeoutError
        answered with an HTTP error    -> influxdb_client ApiException

    The first two are urllib3's and reach the caller unwrapped, because
    retries=False leaves nothing in between. Both subclass
    urllib3.exceptions.HTTPError; ApiException subclasses InfluxDBError. Hence
    the two-tuple in consumer_store, and hence the note here -- catching only
    InfluxDBError would miss a stopped container, which is the exact case the
    stage's failure test produces.
    """
    api.write(
        bucket=settings.influxdb_bucket,
        record=point,
        # See WRITE_PRECISION: omitting this silently means nanoseconds.
        write_precision=WRITE_PRECISION,
    )


# The two exception bases a caller must catch to cover every write failure.
# Exported so the tuple is written once and cannot drift from the docstring
# above.
WRITE_FAILURES = (InfluxDBError, HTTPError)


def describe() -> str:
    """Human-readable storage schema, for the startup log.

    Mirrors topology_spec.describe() and config.describe(): the schema is
    otherwise invisible until something queries it, and a wrong measurement name
    produces an empty dashboard rather than an error.
    """
    return "\n".join(
        [
            f"  measurement = {MEASUREMENT}",
            f"  tags        = {', '.join(TAG_FIELDS)}",
            f"  fields      = {', '.join(VALUE_FIELDS)}",
            f"  timestamp   = {TIMESTAMP_FIELD} (precision {WRITE_PRECISION})",
        ]
    )