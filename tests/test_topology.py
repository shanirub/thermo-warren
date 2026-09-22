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


def test_durable_queue_is_back_to_its_baseline():
    # Third stage running where a test written to be changed gets changed
    # rather than deleted. Stage 8 put a TTL here and stage 9 a length cap;
    # both were demonstrated and then removed, because the plan calls for
    # returning to steady-state settings once the lesson is recorded. What
    # survives all of them is the dead-letter exchange.
    channel = MagicMock()
    declare(channel)
    args = declared_queues(channel)[spec.QUEUE_STORE]["arguments"]

    assert args["x-dead-letter-exchange"] == spec.DLX
    assert "x-message-ttl" not in args
    assert "x-max-length" not in args


def test_the_overflow_constants_are_the_two_modes_that_dead_letter():
    # The trap this guards: plain "reject-publish" discards WITHOUT
    # dead-lettering, and its name reads like the safer of the two. If someone
    # later "simplifies" either constant to it, the evidence this project is
    # built to read stops appearing and nothing else fails.
    assert spec.STORE_OVERFLOW_OLDEST_OUT == "drop-head"
    assert spec.STORE_OVERFLOW_NEWEST_OUT == "reject-publish-dlx"
    assert "reject-publish" != spec.STORE_OVERFLOW_OLDEST_OUT
    assert "reject-publish" != spec.STORE_OVERFLOW_NEWEST_OUT
    assert spec.OBSERVE_OVERFLOW != "reject-publish"


def test_the_two_bounded_queues_do_not_share_one_cap():
    # Different numbers on purpose: two bounded queues running the same value
    # would read as a project convention rather than two separate policies
    # chosen for two different reasons.
    assert spec.STORE_MAX_LENGTH != spec.OBSERVE_MAX_LENGTH


def test_the_ttl_is_far_above_a_consumer_restart():
    # Retained though the TTL is no longer applied: the value is kept in the
    # spec so stage 8's experiment is one edit away, and the reasoning behind
    # it should not rot. 30s was chosen so a routine restart -- measured at
    # 2-4s across stages 6 and 7 -- can never dead-letter live data.
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
