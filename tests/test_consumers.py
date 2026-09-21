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
    # Same bytes dead-letter on the durable path from stage 7. Here they are
    # merely noted, because telemetry.observe has no dead-letter exchange.
    observe.main()
    channel = MagicMock()

    with caplog.at_level(logging.WARNING):
        runner["on_message"](channel, delivery(), MagicMock(), MALFORMED)

    assert "unparseable" in caplog.text
    channel.basic_ack.assert_not_called()


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


# --- Decision 4: malformed payloads are acked, not rejected, until stage 7 ---


def test_store_acknowledges_a_well_formed_message(runner):
    store.main([])
    channel = MagicMock()
    method = delivery(tag=11)

    runner["on_message"](channel, method, MagicMock(), WELL_FORMED)

    channel.basic_ack.assert_called_once_with(delivery_tag=11)
    channel.basic_nack.assert_not_called()
    channel.basic_reject.assert_not_called()


def test_store_acks_malformed_rather_than_rejecting_it(runner, caplog):
    # Stage 7 inverts this deliberately: basic_ack becomes
    # basic_nack(requeue=False). Until then, rejecting here would dead-letter
    # a stage ahead of schedule.
    store.main([])
    channel = MagicMock()

    with caplog.at_level(logging.WARNING):
        runner["on_message"](channel, delivery(tag=12), MagicMock(), MALFORMED)

    channel.basic_ack.assert_called_once_with(delivery_tag=12)
    channel.basic_nack.assert_not_called()
    channel.basic_reject.assert_not_called()
    assert "unparseable" in caplog.text


def test_store_does_not_leave_a_poison_message_unacknowledged(runner):
    # Leaving it unacked holds a prefetch slot forever; consumer_prefetch_count
    # of them wedge the consumer permanently.
    store.main([])
    channel = MagicMock()

    for tag in range(settings.consumer_prefetch_count + 1):
        runner["on_message"](channel, delivery(tag=tag), MagicMock(), MALFORMED)

    assert channel.basic_ack.call_count == settings.consumer_prefetch_count + 1


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
