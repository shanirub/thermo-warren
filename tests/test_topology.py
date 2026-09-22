"""Unit tests for the declared topology.

No broker involved. declare() takes a channel, so a mock records exactly which
arguments were passed -- and the arguments dicts are the policy, which makes
them the thing worth asserting on.

Each test corresponds to a decision made during stage 4 planning. A test failing
means either the code drifted or the decision changed; in the second case the
test should be updated deliberately, not deleted.
"""

from unittest.mock import MagicMock

from telemetry import topology_spec as spec
from telemetry.topology import declare, drop


def declared_queues(channel) -> dict[str, dict]:
    return {c.kwargs["queue"]: c.kwargs for c in channel.queue_declare.call_args_list}


def bindings(channel) -> dict[str, dict]:
    return {c.kwargs["queue"]: c.kwargs for c in channel.queue_bind.call_args_list}


def test_routing_key_is_the_mqtt_topic_translated():
    # The plugin maps "/" to "." in one direction and back in the other. Dots in
    # MQTT topics and slashes in routing keys are documented as unreliable.
    assert spec.ROUTING_KEY == "sensors.esp32c3.telemetry"
    assert "." not in spec.MQTT_TOPIC
    assert "/" not in spec.ROUTING_KEY
    assert not spec.MQTT_TOPIC.startswith("/")


def test_fan_out_both_queues_share_one_key_on_amq_topic():
    # The single most important assertion here. If these diverge, one consumer
    # receives nothing and no error is raised anywhere in the system.
    channel = MagicMock()
    declare(channel)
    bound = bindings(channel)

    assert bound[spec.QUEUE_STORE]["exchange"] == spec.SOURCE_EXCHANGE
    assert bound[spec.QUEUE_OBSERVE]["exchange"] == spec.SOURCE_EXCHANGE
    assert (
        bound[spec.QUEUE_STORE]["routing_key"]
        == bound[spec.QUEUE_OBSERVE]["routing_key"]
        == spec.ROUTING_KEY
    )


def test_only_the_durable_path_dead_letters():
    channel = MagicMock()
    declare(channel)
    args = {q: kw["arguments"] for q, kw in declared_queues(channel).items()}

    assert args[spec.QUEUE_STORE]["x-dead-letter-exchange"] == spec.DLX
    # Absence is the design, on both counts: the observation queue drops without
    # trace, and a DLQ that dead-letters is a loop.
    assert "x-dead-letter-exchange" not in args[spec.QUEUE_OBSERVE]
    assert "x-dead-letter-exchange" not in args[spec.QUEUE_DLQ]


def test_observation_queue_is_bounded_and_evicts_the_oldest():
    channel = MagicMock()
    declare(channel)
    args = declared_queues(channel)[spec.QUEUE_OBSERVE]["arguments"]

    assert args["x-max-length"] == spec.OBSERVE_MAX_LENGTH
    # drop-head, not reject-publish: the latter discards without dead-lettering.
    assert args["x-overflow"] == "drop-head"


def test_durable_queue_expires_messages_but_has_no_length_cap_yet():
    # Half-inverted at stage 8, the same way stage 7 inverted its own stage 6
    # test. The TTL arrived here; x-max-length is still stage 9's, and stage 9
    # clears the TTL when it lands, so the two never apply at once.
    channel = MagicMock()
    declare(channel)
    args = declared_queues(channel)[spec.QUEUE_STORE]["arguments"]

    assert args["x-message-ttl"] == spec.STORE_MESSAGE_TTL_MS
    assert "x-max-length" not in args


def test_only_the_durable_queue_expires_messages():
    # The asymmetry is the design. telemetry.observe already sheds its head at
    # the cap; a second reason for a message to vanish there would make it
    # impossible to say which one acted. The DLQ must never expire anything --
    # its contents are the evidence.
    channel = MagicMock()
    declare(channel)
    args = {q: kw["arguments"] for q, kw in declared_queues(channel).items()}

    assert "x-message-ttl" in args[spec.QUEUE_STORE]
    assert "x-message-ttl" not in args[spec.QUEUE_OBSERVE]
    assert "x-message-ttl" not in args[spec.QUEUE_DLQ]


def test_the_ttl_is_far_above_a_consumer_restart():
    # 30s was chosen so a routine restart -- measured at 2-4s across stages 6
    # and 7 -- can never dead-letter live data, which keeps the DLQ clean
    # evidence for stages 8 and 9. A value near the restart time would make
    # every dead-letter ambiguous.
    assert spec.STORE_MESSAGE_TTL_MS >= 10_000


def test_every_queue_is_durable_and_explicitly_classic():
    # durable is about the queue object surviving a broker restart, not about
    # message retention -- the lossy queue is durable too.
    channel = MagicMock()
    declare(channel)

    for queue, kwargs in declared_queues(channel).items():
        assert kwargs["durable"] is True, queue
        assert kwargs["auto_delete"] is False, queue
        assert kwargs["arguments"]["x-queue-type"] == "classic", queue


def test_dead_letter_side_exists_before_anything_references_it():
    # Dead-lettering follows normal routing rules, so an unbound DLX discards
    # silently. Order matters.
    channel = MagicMock()
    declare(channel)
    order = [c.kwargs["queue"] for c in channel.queue_declare.call_args_list]

    assert order.index(spec.QUEUE_DLQ) < order.index(spec.QUEUE_STORE)
    channel.exchange_declare.assert_called_once()
    assert channel.exchange_declare.call_args.kwargs["exchange"] == spec.DLX
    assert channel.exchange_declare.call_args.kwargs["durable"] is True


def test_dlq_binding_matches_any_routing_key():
    # "#" matches any key; "*" would silently stop matching multi-word keys.
    channel = MagicMock()
    declare(channel)

    assert bindings(channel)[spec.QUEUE_DLQ]["exchange"] == spec.DLX
    assert bindings(channel)[spec.QUEUE_DLQ]["routing_key"] == "#"


def test_amq_topic_is_never_declared():
    # Declaring a reserved amq.* name is refused with ACCESS_REFUSED (403).
    channel = MagicMock()
    declare(channel)

    declared = [c.kwargs["exchange"] for c in channel.exchange_declare.call_args_list]
    assert spec.SOURCE_EXCHANGE not in declared


def test_recreate_deletes_the_two_main_queues_and_spares_the_dlq():
    channel = MagicMock()
    drop(channel)
    deleted = [c.kwargs["queue"] for c in channel.queue_delete.call_args_list]

    assert deleted == list(spec.RECREATABLE)
    assert spec.QUEUE_DLQ not in deleted
    # No if_empty/if_unused guard: stages 8 and 9 recreate queues that are full.
    for call in channel.queue_delete.call_args_list:
        assert "if_empty" not in call.kwargs
        assert "if_unused" not in call.kwargs
