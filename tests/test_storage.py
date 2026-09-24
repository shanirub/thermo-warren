"""Unit tests for the storage module.

No InfluxDB involved, and no socket is opened: InfluxDBClient does no I/O at
construction, and where the constructor's arguments matter it is replaced with a
mock so the assertion is about what we asked for rather than about library
internals.

Each test corresponds to a decision made during stage 11 planning, following
test_consumers.py. A test failing means either the code drifted or the decision
changed; in the second case update it deliberately rather than deleting it.
"""

import json
from unittest.mock import MagicMock

import pytest
from influxdb_client import WritePrecision

from telemetry import payload as contract
from telemetry import storage
from telemetry.config import settings

WELL_FORMED = json.dumps(
    {
        "seq": 42,
        "device": "sim-01",
        "temp_c": 24.0,
        "humidity_pct": 55.0,
        "ts_ms": 1756400000123,
    }
).encode()


def parsed(**overrides) -> dict:
    """A payload as parse() would return it, so the mapping is tested on real input."""
    payload = contract.parse(WELL_FORMED)
    payload.update(overrides)
    return payload


# --- The schema is not re-spelled here ---------------------------------------


def test_every_key_name_comes_from_the_payload_contract():
    # The one rule this module has. Re-spelling a name would let the storage
    # schema drift from the wire contract silently -- a renamed field would
    # still write, just into a differently-named column, with no error.
    named = set(storage.TAG_FIELDS) | set(storage.VALUE_FIELDS)
    named.add(storage.TIMESTAMP_FIELD)

    contract_fields = {
        contract.FIELD_SEQ,
        contract.FIELD_DEVICE,
        contract.FIELD_TEMP_C,
        contract.FIELD_HUMIDITY_PCT,
        contract.FIELD_TS_MS,
    }
    assert named == contract_fields


def test_device_is_the_only_tag():
    # Tag values are indexed and every distinct tag set is a series, so this
    # tuple is the cardinality decision: one series per publisher, forever.
    assert storage.TAG_FIELDS == (contract.FIELD_DEVICE,)


def test_seq_is_a_field_and_never_a_tag():
    # seq is monotonic and unbounded. As a tag it would create one series per
    # message -- ~86,400 a day at 1 Hz -- against an infinite-retention bucket.
    assert contract.FIELD_SEQ not in storage.TAG_FIELDS
    assert contract.FIELD_SEQ in storage.VALUE_FIELDS


def test_measurement_is_not_the_bucket_name():
    # "telemetry" already names the bucket, the queue prefix and (from stage 12)
    # the InfluxQL database, which would read as `FROM telemetry` in database
    # `telemetry` in every query.
    assert storage.MEASUREMENT == "readings"
    assert storage.MEASUREMENT != settings.influxdb_bucket


# --- Mapping a payload onto a point ------------------------------------------


def test_to_point_builds_the_agreed_line_protocol():
    line = storage.to_point(parsed()).to_line_protocol()

    assert line.startswith("readings,device=sim-01 ")
    assert line.endswith(" 1756400000123")
    assert "seq=42i" in line


def test_readings_are_written_as_float_fields():
    # payload.parse() tolerates a JSON integer for temp_c / humidity_pct,
    # because json.loads("29") is an int and a strict check would dead-letter
    # good readings after a publisher format-string change. **Verified against
    # influxdb-client 1.50.0**: without the float() coercion,
    # Point.field("temp_c", 29) serializes as `temp_c=29i` -- an integer field.
    # InfluxDB fixes a field's type on first write, so that tolerance reaching
    # storage would be rejected for the whole batch.
    line = storage.to_point(parsed(temp_c=29, humidity_pct=60)).to_line_protocol()

    assert "temp_c=29i" not in line
    assert "humidity_pct=60i" not in line
    assert "temp_c=29" in line
    assert "humidity_pct=60" in line


def test_seq_stays_an_integer_field():
    # The counterpart to the test above: seq is genuinely an integer and the
    # `i` suffix is correct here.
    assert "seq=42i" in storage.to_point(parsed()).to_line_protocol()


def test_the_timestamp_is_the_payloads_and_not_arrival_time():
    # Publisher-stamped, so that a burst or an outage does not smear the
    # timeline across the very demonstrations stages 11 and 18 exist to produce.
    line = storage.to_point(parsed(ts_ms=1700000000042)).to_line_protocol()

    assert line.endswith(" 1700000000042")


# --- Precision, which is silently wrong if left to the library ---------------


def test_write_passes_millisecond_precision_explicitly():
    # **Verified against 1.50.0**: WriteApi.write's write_precision defaults to
    # 'ns', and the Point's own precision does not rewrite the number -- an int
    # timestamp is serialized verbatim. Omitting this would have the server read
    # 1756400000123 as nanoseconds and file every point in January 1970, with no
    # error anywhere.
    api = MagicMock()

    storage.write(api, storage.to_point(parsed()))

    kwargs = api.write.call_args.kwargs
    assert kwargs["write_precision"] == WritePrecision.MS
    assert storage.WRITE_PRECISION == WritePrecision.MS


def test_write_targets_the_configured_bucket():
    api = MagicMock()

    storage.write(api, storage.to_point(parsed()))

    assert api.write.call_args.kwargs["bucket"] == settings.influxdb_bucket


# --- Connecting ---------------------------------------------------------------


def test_connect_refuses_an_empty_token_by_name(monkeypatch):
    # The fields carry defaults so topology, publisher and consumer_observe can
    # start without a token they never use. "Fail loudly by name" therefore has
    # to happen here instead of in pydantic -- in the one process that needs it.
    monkeypatch.setattr(settings, "influxdb_token", "")

    with pytest.raises(storage.StorageUnconfigured) as excinfo:
        storage.connect()

    assert "INFLUXDB_TOKEN" in str(excinfo.value)


def test_connect_states_the_timeout_and_disables_library_retries(monkeypatch):
    # Explicit-over-inherited, and the retry setting is load-bearing: a urllib3
    # policy underneath consumer_store's loop would multiply the attempts and
    # bend the backoff curve, with only the outer loop visible in the log.
    built = MagicMock()
    monkeypatch.setattr(storage, "InfluxDBClient", built)

    storage.connect()

    kwargs = built.call_args.kwargs
    assert kwargs["timeout"] == settings.influxdb_timeout_ms
    assert kwargs["retries"] is False
    assert kwargs["org"] == settings.influxdb_org
    assert kwargs["url"] == settings.influxdb_url


def test_connect_does_not_hide_the_token_from_the_client(monkeypatch):
    built = MagicMock()
    monkeypatch.setattr(storage, "InfluxDBClient", built)

    storage.connect()

    assert built.call_args.kwargs["token"] == settings.influxdb_token


# --- describe() ---------------------------------------------------------------


def test_describe_names_the_measurement_and_the_precision():
    # A wrong measurement name produces an empty dashboard rather than an error,
    # which is why this is logged at startup at all.
    described = storage.describe()

    assert storage.MEASUREMENT in described
    assert contract.FIELD_DEVICE in described
    assert "ms" in described
