"""Unit tests for the software publisher.

No broker involved. build_payload, corrupt, should_corrupt and DriftSimulator
are pure functions/classes, deliberately factored out of publish_one() so the
payload contract and the corruption rule can be asserted without an mqtt
client -- mirroring how test_topology.py asserts declare() against a mock
channel instead of a real broker.

Each test corresponds to a decision made during stage 5 planning. A test
failing means either the code drifted or the decision changed; in the second
case the test should be updated deliberately, not deleted.
"""

import itertools
import json

from telemetry import publisher as pub


def test_payload_has_exactly_the_agreed_keys_and_types():
    payload = pub.build_payload(
        seq=1234, temp_c=24.4, humidity_pct=62.5, ts_ms=1756400000123
    )

    assert set(payload) == {
        pub.FIELD_SEQ,
        pub.FIELD_DEVICE,
        pub.FIELD_TEMP_C,
        pub.FIELD_HUMIDITY_PCT,
        pub.FIELD_TS_MS,
    }
    assert isinstance(payload[pub.FIELD_SEQ], int)
    assert isinstance(payload[pub.FIELD_DEVICE], str)
    assert isinstance(payload[pub.FIELD_TEMP_C], float)
    assert isinstance(payload[pub.FIELD_HUMIDITY_PCT], float)
    assert isinstance(payload[pub.FIELD_TS_MS], int)


def test_seq_increments_monotonically_from_one():
    seqs = list(itertools.islice(pub.sequence_numbers(), 5))
    assert seqs == [1, 2, 3, 4, 5]


def test_ts_ms_is_milliseconds_not_seconds():
    # A second-resolution timestamp near "now" is ~1.7e9; millisecond
    # resolution is ~1.7e12. Three orders of magnitude apart is enough to
    # catch a stray int(time.time()) without pinning an exact value.
    assert pub.now_ms() > 10**12


def test_corrupt_every_n_corrupts_exactly_every_nth_message():
    corrupted = [seq for seq in range(1, 11) if pub.should_corrupt(seq, 3)]
    assert corrupted == [3, 6, 9]


def test_corrupt_every_none_never_corrupts():
    assert all(not pub.should_corrupt(seq, None) for seq in range(1, 11))


def test_corrupted_payload_genuinely_fails_json_loads():
    payload = pub.build_payload(
        seq=1, temp_c=24.4, humidity_pct=62.5, ts_ms=1756400000123
    )
    serialized = json.dumps(payload)
    broken = pub.corrupt(serialized)

    assert broken != serialized
    try:
        json.loads(broken)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("corrupted payload should not be valid JSON")


def test_uncorrupted_messages_still_parse():
    # The rest of a --corrupt-every run must stay well-formed -- corruption
    # is a per-message decision, not a mode that degrades everything.
    payload = pub.build_payload(
        seq=2, temp_c=24.4, humidity_pct=62.5, ts_ms=1756400000123
    )
    serialized = json.dumps(payload)
    assert json.loads(serialized) == payload


def test_drift_simulator_stays_within_dht11_range():
    sim = pub.DriftSimulator()
    for _ in range(500):
        temp_c, humidity_pct = sim.step()
        assert pub.TEMP_MIN_C <= temp_c <= pub.TEMP_MAX_C
        assert pub.HUMIDITY_MIN_PCT <= humidity_pct <= pub.HUMIDITY_MAX_PCT


def test_drift_simulator_moves_by_at_most_one_unit_per_step():
    # "Slow drift" is the design goal, not independent per-sample noise.
    sim = pub.DriftSimulator(temp_c=25.0, humidity_pct=50.0)
    prev_temp, prev_humidity = sim.temp_c, sim.humidity_pct
    for _ in range(200):
        temp_c, humidity_pct = sim.step()
        assert abs(temp_c - prev_temp) <= 1
        assert abs(humidity_pct - prev_humidity) <= 1
        prev_temp, prev_humidity = temp_c, humidity_pct
