"""The frozen payload contract: what the publisher writes and consumers read.

A third category, alongside the two the project already has:

    config.py         what differs between run modes
    topology_spec.py  what the broker enforces
    payload.py        what publisher and consumers must agree on

The broker is the reason this needs its own module rather than joining
`topology_spec.py`: **it never inspects the message body.** It routes on the
key alone, so nothing in the broker will ever reject a payload for being the
wrong shape. That makes a contract mismatch a *silent* failure of a different
kind from a wrong routing key, and it makes this file the only enforcement
point there is.

Extracted at stage 7, when `consumer_store` began validating the whole contract
rather than reading one field. `publisher.py` carried a note to revisit this
"when consumer_observe.py or consumer_store.py exist" -- they now do, and both
parse, which is the same trigger that produced `amqp.py` at stage 6.

Frozen at stage 5 and satisfied by stage 17's firmware. Not open to
renegotiation without changing both halves of the project together.
"""

from __future__ import annotations

import json
import math

from telemetry.config import settings

# --- The five fields --------------------------------------------------------

FIELD_SEQ = "seq"
FIELD_DEVICE = "device"
FIELD_TEMP_C = "temp_c"
FIELD_HUMIDITY_PCT = "humidity_pct"
FIELD_TS_MS = "ts_ms"

CONTENT_TYPE_JSON = "application/json"


class ContractViolation(ValueError):
    """Valid JSON that does not satisfy the contract.

    Distinct from json.JSONDecodeError on purpose. Both dead-letter at stage 7,
    but they are different failures and the log line says which -- see parse().
    """


def build_payload(seq: int, temp_c: float, humidity_pct: float, ts_ms: int) -> dict:
    """The publisher's side of the contract.

    Reads settings.device_id, so this module imports config -- deliberately
    unlike topology_spec.py, which refuses to. The justification is specific:
    `device` is the one contract field whose *value* legitimately differs
    between the simulator and the MCU, which is exactly what config.py is for.
    The field *names* below are not configurable and never will be.
    """
    return {
        FIELD_SEQ: seq,
        FIELD_DEVICE: settings.device_id,
        # float(): dht_read_float_data() on the firmware side returns floats,
        # so the contract does not force a type conversion at stage 17.
        FIELD_TEMP_C: float(temp_c),
        FIELD_HUMIDITY_PCT: float(humidity_pct),
        FIELD_TS_MS: ts_ms,
    }


# --- Validation -------------------------------------------------------------
#
# Each check returns None when the value is acceptable, or a reason string.
# Reasons end up in the consumer log, because the DLQ cannot carry them: x-death
# records the *broker's* reason ("rejected"), and RabbitMQ offers no way to
# attach the application's.


def _integer(value: object) -> str | None:
    # `not isinstance(value, bool)` is load-bearing: bool subclasses int in
    # Python, so isinstance(True, int) is True and a JSON `true` would sail
    # through a bare int check.
    if isinstance(value, bool) or not isinstance(value, int):
        return f"expected an integer, got {type(value).__name__}"
    return None


def _reading(value: object) -> str | None:
    """A sensor reading: any finite JSON number.

    Accepts int as well as float, deliberately. JSON has a single number type,
    so whether a reading arrives as `29` or `29.0` depends entirely on the
    publisher's formatting -- json.loads('29') is an int and fails a bare
    isinstance(x, float). Insisting on float would enforce an artifact of the
    deserializer rather than anything the contract says, and a firmware
    format-string change would start dead-lettering valid readings that look
    perfectly correct when you read them out of the DLQ.

    isfinite() is the check that actually matters downstream: Python's json
    accepts NaN and Infinity as an extension to the spec, and a NaN passes every
    isinstance check before poisoning the InfluxDB write at stage 11. Keeping it
    out of that stage is why this validation exists at all.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"expected a number, got {type(value).__name__}"
    if not math.isfinite(value):
        return f"expected a finite number, got {value}"
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return f"expected a string, got {type(value).__name__}"
    return None


CONTRACT = {
    FIELD_SEQ: _integer,
    FIELD_DEVICE: _text,
    FIELD_TEMP_C: _reading,
    FIELD_HUMIDITY_PCT: _reading,
    FIELD_TS_MS: _integer,
}


def parse(body: bytes) -> dict:
    """Decode and validate one message body.

    Raises:
        json.JSONDecodeError  -- not JSON at all (what --corrupt-every produces)
        ContractViolation     -- JSON, but not this contract

    Both consumers call this, so both consider the same messages bad. Only what
    they *do* about it differs: consumer_store dead-letters, consumer_observe
    logs and moves on, because its queue has no dead-letter exchange. That is
    the fan-out asymmetry, and keeping the parsing identical is what makes the
    comparison honest.

    Extra fields are allowed on purpose. Rejecting them would make adding a
    field a breaking change across the hardware boundary -- the firmware and the
    consumers are deployed separately and cannot be updated together.
    """
    payload = json.loads(body)

    # json.loads happily returns a list, a string or a number for valid JSON
    # that is not an object; indexing those raises TypeError rather than
    # anything self-describing, so the check is explicit.
    if not isinstance(payload, dict):
        raise ContractViolation(
            f"expected a JSON object, got {type(payload).__name__}"
        )

    for field, check in CONTRACT.items():
        if field not in payload:
            raise ContractViolation(f"missing field {field!r}")
        reason = check(payload[field])
        if reason is not None:
            raise ContractViolation(f"field {field!r}: {reason}")

    return payload
