"""Unit tests for both consumers.

No broker involved. The message handlers take a channel as their first
argument, exactly as pika calls them, so a MagicMock records which of
basic_ack / basic_nack / basic_reject was called and with what -- mirroring how
test_topology.py asserts declare() against a mock channel.

Each test corresponds to a decision made during stage 6 planning. A test
failing means either the code drifted or the decision changed; in the second
case the test should be updated deliberately, not deleted.
"""

import json
import logging
import re
from unittest.mock import MagicMock

import pytest

from telemetry import amqp
from telemetry import consumer_observe as observe
from telemetry import consumer_store as store
from telemetry import topology_spec as spec
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

# Truncated JSON, the same shape publisher.corrupt() produces: a genuine parse
# failure, not valid JSON with the wrong fields.
MALFORMED = WELL_FORMED[: len(WELL_FORMED) // 2]

# Parses cleanly; violates the contract by omitting humidity_pct. The other
# poison class, and the one that would otherwise have surfaced at stage 11.
OFF_CONTRACT = json.dumps(
    {"seq": 43, "device": "sim-01", "temp_c": 24.0, "ts_ms": 1756400000123}
).encode()

# Every way a payload can be off-contract, as whole message bodies rather than
# as parse() inputs. test_payload.py already proves parse() rejects each of
# these; these exist to prove the *consumer* dead-letters what parse() rejects,
# which is a separate claim -- the handler catches ContractViolation broadly, so
# without these the link is inferred rather than asserted.
OFF_CONTRACT_BODIES = {
    "missing field": OFF_CONTRACT,
    "non-finite reading": b'{"seq": 1, "device": "d", "temp_c": NaN, '
    b'"humidity_pct": 55.0, "ts_ms": 1}',
    "bool where an int belongs": json.dumps(
        {"seq": True, "device": "d", "temp_c": 24.0,
         "humidity_pct": 55.0, "ts_ms": 1}
    ).encode(),
    "string where a number belongs": json.dumps(
        {"seq": 1, "device": "d", "temp_c": "24.0",
         "humidity_pct": 55.0, "ts_ms": 1}
    ).encode(),
    "device is not a string": json.dumps(
        {"seq": 1, "device": 7, "temp_c": 24.0,
         "humidity_pct": 55.0, "ts_ms": 1}
    ).encode(),
    "valid JSON but not an object": b"[1, 2, 3]",
}


def delivery(tag: int = 7) -> MagicMock:
    method = MagicMock()
    method.delivery_tag = tag
    return method


@pytest.fixture
def runner(monkeypatch):
    """Capture the arguments main() hands to run_consumer, without connecting.

    Also neutralises configure_logging(). It calls basicConfig(force=True),
    which detaches every existing root handler -- including the one caplog
    installs -- so without this the log assertions below see an empty capture
    while the lines themselves appear on stderr.
    """
    calls = {}

    monkeypatch.setattr(observe, "configure_logging", lambda: None)
    monkeypatch.setattr(store, "configure_logging", lambda: None)

    def fake_run_consumer(queue, on_message, *, auto_ack, prefetch_count):
        calls.update(
            queue=queue,
            on_message=on_message,
            auto_ack=auto_ack,
            prefetch_count=prefetch_count,
        )
        return amqp.EXIT_OK

    monkeypatch.setattr(observe, "run_consumer", fake_run_consumer)
    monkeypatch.setattr(store, "run_consumer", fake_run_consumer)
    return calls


# --- Decision 1: the observation path is lossy in the consumer too -----------


def test_observe_uses_automatic_acknowledgment(runner):
    observe.main()

    assert runner["queue"] == spec.QUEUE_OBSERVE
    assert runner["auto_ack"] is True


def test_observe_asks_for_no_prefetch_at_all(runner):
    # None, not 0. basic_qos is ignored on an auto-ack channel, so calling it
    # would imply a bound that does not exist. The negative half of decision 1.
    observe.main()

    assert runner["prefetch_count"] is None


def test_observe_never_acknowledges(runner):
    observe.main()
    channel = MagicMock()

    runner["on_message"](channel, delivery(), MagicMock(), WELL_FORMED)

    # An ack on an auto-ack channel is a channel error, not a harmless no-op.
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_not_called()
    channel.basic_reject.assert_not_called()


def test_observe_logs_malformed_without_dead_lettering(runner, caplog):
    # The same bytes dead-letter on the durable path. Here they are merely
    # noted, because telemetry.observe has no dead-letter exchange -- the two
    # fates that make the fan-out asymmetry worth having.
    observe.main()
    channel = MagicMock()

    with caplog.at_level(logging.WARNING):
        runner["on_message"](channel, delivery(), MagicMock(), MALFORMED)

    assert "poison message" in caplog.text
    channel.basic_ack.assert_not_called()
    channel.basic_reject.assert_not_called()
    channel.basic_nack.assert_not_called()


def test_observe_and_store_agree_on_what_is_bad(runner):
    # Both call the same payload.parse(), on purpose: "same bytes, two fates"
    # is only an honest comparison if both consider the same messages bad.
    observe.main()
    observe_channel = MagicMock()
    runner["on_message"](observe_channel, delivery(), MagicMock(), OFF_CONTRACT)

    store.main([])
    store_channel = MagicMock()
    runner["on_message"](store_channel, delivery(), MagicMock(), OFF_CONTRACT)

    # Same verdict, different consequence.
    store_channel.basic_reject.assert_called_once()
    observe_channel.basic_reject.assert_not_called()


# --- Decision 2: bounded prefetch on the durable path ------------------------


def test_store_uses_manual_acknowledgment_and_the_configured_prefetch(runner):
    store.main([])

    assert runner["queue"] == spec.QUEUE_STORE
    assert runner["auto_ack"] is False
    assert runner["prefetch_count"] == settings.consumer_prefetch_count


def test_prefetch_default_is_observable_not_just_safe(runner):
    # A cap of 1 would make stage 6's DoD check vacuous: "unacked pinned at 1"
    # looks the same whether basic_qos was called or not.
    assert settings.consumer_prefetch_count > 1


# --- Stage 7: bad payloads are dead-lettered, not acked ---------------------


def test_store_acknowledges_a_well_formed_message(runner):
    store.main([])
    channel = MagicMock()
    method = delivery(tag=11)

    runner["on_message"](channel, method, MagicMock(), WELL_FORMED)

    channel.basic_ack.assert_called_once_with(delivery_tag=11)
    channel.basic_nack.assert_not_called()
    channel.basic_reject.assert_not_called()


def test_store_dead_letters_malformed_without_requeue(runner, caplog):
    # The stage 6 version of this test asserted basic_ack, and was written to be
    # inverted here rather than deleted. requeue=False is the whole of stage 7:
    # it is what routes the message through telemetry.dlx instead of back onto
    # telemetry.store.
    store.main([])
    channel = MagicMock()

    with caplog.at_level(logging.WARNING):
        runner["on_message"](channel, delivery(tag=12), MagicMock(), MALFORMED)

    channel.basic_reject.assert_called_once_with(delivery_tag=12, requeue=False)
    channel.basic_ack.assert_not_called()
    # basic_reject, not basic_nack: core AMQP rather than the RabbitMQ
    # extension, whose only addition is batch rejection we never use.
    channel.basic_nack.assert_not_called()
    assert "dead-lettered" in caplog.text


def test_store_names_the_failure_class_because_x_death_cannot(runner, caplog):
    # x-death records the broker's reason ("rejected") for every rejection, so
    # the DLQ cannot say whether a message was truncated or merely off-contract.
    # This log line is the only place that distinction exists.
    store.main([])

    with caplog.at_level(logging.WARNING):
        runner["on_message"](MagicMock(), delivery(), MagicMock(), MALFORMED)
    assert "malformed JSON" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        runner["on_message"](MagicMock(), delivery(), MagicMock(), OFF_CONTRACT)
    assert "off-contract" in caplog.text


def test_store_dead_letters_an_off_contract_payload(runner):
    # Valid JSON, parses fine, but missing humidity_pct. Validated here rather
    # than at stage 11 so that stage's failure test stays about storage alone.
    store.main([])
    channel = MagicMock()

    runner["on_message"](channel, delivery(tag=13), MagicMock(), OFF_CONTRACT)

    channel.basic_reject.assert_called_once_with(delivery_tag=13, requeue=False)
    channel.basic_ack.assert_not_called()


@pytest.mark.parametrize(
    "case", list(OFF_CONTRACT_BODIES), ids=list(OFF_CONTRACT_BODIES)
)
def test_store_dead_letters_every_off_contract_class(runner, case):
    # Wrong types and out-of-range values must dead-letter exactly as a missing
    # field does. Verified live at stage 7 by injecting these over MQTT; this is
    # the suite's guard on the same behaviour.
    store.main([])
    channel = MagicMock()

    runner["on_message"](
        channel, delivery(tag=21), MagicMock(), OFF_CONTRACT_BODIES[case]
    )

    channel.basic_reject.assert_called_once_with(delivery_tag=21, requeue=False)
    channel.basic_ack.assert_not_called()


@pytest.mark.parametrize(
    "case", list(OFF_CONTRACT_BODIES), ids=list(OFF_CONTRACT_BODIES)
)
def test_observe_only_logs_every_off_contract_class(runner, case, caplog):
    # The other fate, for the same six bodies: telemetry.observe has no DLX, so
    # nothing is rejected and nothing records that the message existed.
    observe.main()
    channel = MagicMock()

    with caplog.at_level(logging.WARNING):
        runner["on_message"](
            channel, delivery(), MagicMock(), OFF_CONTRACT_BODIES[case]
        )

    assert "poison message" in caplog.text
    channel.basic_reject.assert_not_called()
    channel.basic_ack.assert_not_called()


def test_requeue_poison_is_off_by_default():
    assert store.build_parser().parse_args([]).requeue_poison is False


def test_requeue_poison_requeues_instead_of_dead_lettering(runner):
    # The pathology, asserted so it cannot appear by accident. requeue=True
    # sends the message straight back to be redelivered and fail again, which
    # is an unbounded loop -- and it writes no x-death, so it leaves no trace.
    store.main(["--requeue-poison"])
    channel = MagicMock()

    runner["on_message"](channel, delivery(tag=14), MagicMock(), MALFORMED)

    channel.basic_reject.assert_called_once_with(delivery_tag=14, requeue=True)


def test_store_does_not_leave_a_poison_message_unacknowledged(runner):
    # Unchanged property, different call since stage 7: a poison message must
    # never hold a prefetch slot. consumer_prefetch_count of them left
    # outstanding would wedge the consumer permanently. A reject settles the
    # delivery just as an ack does.
    store.main([])
    channel = MagicMock()

    for tag in range(settings.consumer_prefetch_count + 1):
        runner["on_message"](channel, delivery(tag=tag), MagicMock(), MALFORMED)

    assert channel.basic_reject.call_count == settings.consumer_prefetch_count + 1


# --- Decision 3: the ack delay is off unless asked for -----------------------


def test_ack_delay_defaults_to_off():
    assert store.build_parser().parse_args([]).ack_delay is None


def test_ack_delay_rejects_a_non_positive_value():
    with pytest.raises(SystemExit):
        store.build_parser().parse_args(["--ack-delay", "0"])


# --- The grep contract -------------------------------------------------------

SEQ_TOKEN = re.compile(r"seq=(\d+)")


def test_both_consumers_log_seq_as_a_bare_grep_able_token(runner, caplog):
    # `grep -o 'seq=[0-9]*'` over the two logs is the stage 6 and stage 9
    # comparison tool. It only works if the token is bare on both sides.
    observe.main()
    with caplog.at_level(logging.INFO):
        runner["on_message"](MagicMock(), delivery(), MagicMock(), WELL_FORMED)
    assert SEQ_TOKEN.search(caplog.text).group(1) == "42"

    caplog.clear()
    store.main([])
    with caplog.at_level(logging.INFO):
        runner["on_message"](MagicMock(), delivery(), MagicMock(), WELL_FORMED)
    assert SEQ_TOKEN.search(caplog.text).group(1) == "42"


# --- Decision 5: a broker cancel must not be silent --------------------------


def test_run_consumer_registers_an_on_cancel_callback():
    # Under the consume() generator a broker cancel just ends the loop with no
    # trace. This is the callback that replaces that silence, and --recreate
    # triggers it at stages 8 and 9.
    session = amqp._Session(connection=MagicMock())
    channel = session.connection.channel.return_value

    amqp._consume(
        session,
        spec.QUEUE_STORE,
        MagicMock(),
        auto_ack=False,
        prefetch_count=5,
    )

    channel.add_on_cancel_callback.assert_called_once()
    channel.basic_qos.assert_called_once_with(prefetch_count=5, global_qos=False)
    channel.start_consuming.assert_called_once()


def test_run_consumer_skips_basic_qos_when_prefetch_is_none():
    session = amqp._Session(connection=MagicMock())
    channel = session.connection.channel.return_value

    amqp._consume(
        session,
        spec.QUEUE_OBSERVE,
        MagicMock(),
        auto_ack=True,
        prefetch_count=None,
    )

    channel.basic_qos.assert_not_called()
    assert channel.basic_consume.call_args.kwargs["auto_ack"] is True
