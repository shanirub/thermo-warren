"""Unit tests for the frozen payload contract.

No broker involved. parse() is a pure function over bytes, which is what makes
the contract assertable without a publisher or a consumer -- the same reasoning
that made declare() take a channel at stage 4.

Each test corresponds to a decision made during stage 7 planning, and several
encode a trap verified against the interpreter rather than assumed. A test
failing means either the code drifted or the decision changed; in the second
case the test should be updated deliberately, not deleted.
"""

import json

import pytest

from telemetry import payload as contract
from telemetry.payload import ContractViolation


def body(**overrides) -> bytes:
    payload = {
        contract.FIELD_SEQ: 42,
        contract.FIELD_DEVICE: "sim-01",
        contract.FIELD_TEMP_C: 24.0,
        contract.FIELD_HUMIDITY_PCT: 55.0,
        contract.FIELD_TS_MS: 1756400000123,
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_a_well_formed_payload_round_trips():
    assert contract.parse(body()) == json.loads(body())


def test_build_payload_output_satisfies_parse():
    # The publisher's side and the consumers' side of one contract. If these
    # ever disagree the pipeline breaks silently, since the broker never looks
    # inside the body.
    built = contract.build_payload(
        seq=1, temp_c=24.0, humidity_pct=55.0, ts_ms=1756400000123
    )
    assert contract.parse(json.dumps(built).encode()) == built


# --- Decision 5: number-shaped, not float-shaped ----------------------------


@pytest.mark.parametrize("value", [29, 29.0, -5, 0, 0.5])
def test_a_reading_may_be_an_int_or_a_float(value):
    # JSON has one number type: json.loads("29") is an int and json.loads("29.0")
    # is a float, so which arrives depends purely on publisher formatting.
    # Requiring float would dead-letter valid readings after a firmware
    # format-string change, and they would look perfectly correct in the DLQ.
    assert contract.parse(body(temp_c=value))[contract.FIELD_TEMP_C] == value


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_a_reading_must_be_finite(raw):
    # Python's json accepts these as an extension to the spec, and they pass
    # every isinstance check before poisoning the InfluxDB write at stage 11.
    # Keeping them out of that stage is why this validation exists.
    malformed = b'{"seq": 1, "device": "d", "temp_c": %s, "humidity_pct": 1.0, "ts_ms": 1}' % raw.encode()
    with pytest.raises(ContractViolation, match="finite"):
        contract.parse(malformed)


@pytest.mark.parametrize("field", [contract.FIELD_SEQ, contract.FIELD_TS_MS])
def test_a_bool_is_not_an_integer(field):
    # bool subclasses int in Python, so isinstance(True, int) is True and a bare
    # int check would let a JSON `true` through.
    with pytest.raises(ContractViolation, match="integer"):
        contract.parse(body(**{field: True}))


@pytest.mark.parametrize("field", [contract.FIELD_TEMP_C, contract.FIELD_HUMIDITY_PCT])
def test_a_bool_is_not_a_reading(field):
    # Same trap on the (int, float) check: isinstance(True, (int, float)) is True.
    with pytest.raises(ContractViolation, match="number"):
        contract.parse(body(**{field: True}))


def test_a_reading_may_not_be_a_numeric_string():
    with pytest.raises(ContractViolation, match="number"):
        contract.parse(body(temp_c="24.0"))


def test_seq_may_not_be_a_float():
    # A counter, not a measurement.
    with pytest.raises(ContractViolation, match="integer"):
        contract.parse(body(seq=42.5))


def test_device_must_be_a_string():
    with pytest.raises(ContractViolation, match="string"):
        contract.parse(body(device=1))


# --- Structure --------------------------------------------------------------


@pytest.mark.parametrize("field", list(contract.CONTRACT))
def test_every_contract_field_is_required_and_named_when_missing(field):
    payload = json.loads(body())
    del payload[field]

    with pytest.raises(ContractViolation, match=field):
        contract.parse(json.dumps(payload).encode())


@pytest.mark.parametrize("raw", [b"[1, 2, 3]", b'"a string"', b"42", b"null"])
def test_valid_json_that_is_not_an_object_is_rejected(raw):
    # Indexing these raises TypeError, which says nothing useful about why the
    # message was wrong, so the check is explicit.
    with pytest.raises(ContractViolation, match="object"):
        contract.parse(raw)


def test_extra_fields_are_allowed():
    # Deliberate. Rejecting them would make adding a field a breaking change
    # across the hardware boundary, where firmware and consumers are deployed
    # separately and cannot be updated together.
    parsed = contract.parse(body(rssi_dbm=-76))
    assert parsed["rssi_dbm"] == -76


def test_truncated_json_raises_a_decode_error_not_a_contract_violation():
    # The two poison classes are distinct: consumer_store names which one in its
    # log line, because x-death records only the broker's reason ("rejected").
    with pytest.raises(json.JSONDecodeError):
        contract.parse(body()[: len(body()) // 2])
