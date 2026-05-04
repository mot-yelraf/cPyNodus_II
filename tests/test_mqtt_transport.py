"""Tests for the lightweight MQTT transport facade."""

from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.settings import Settings


def _run_direct_tests():
    tests = (
        test_transport_reads_target_from_settings,
        test_transport_tracks_connect_request_without_feature_logic,
        test_transport_subscribe_deduplicates_topics,
        test_transport_compact_discards_synced_entries,
    )
    for test in tests:
        test()
    print("test_mqtt_transport: {} tests passed".format(len(tests)))


def test_transport_reads_target_from_settings():
    settings = Settings(
        active_profile="sensorius",
        mqtt_broker="mqtt.example.internal",
        mqtt_port=1884,
    )
    transport = MQTTTransport.from_settings(settings)
    assert transport.target() == ("mqtt.example.internal", 1884)


def test_transport_tracks_connect_request_without_feature_logic():
    transport = MQTTTransport("broker.local", 1883)
    assert transport.connect_requested is False
    transport.mark_connect_requested()
    assert transport.connect_requested is True


def test_transport_subscribe_deduplicates_topics():
    transport = MQTTTransport("broker.local", 1883)

    assert transport.subscribe("nodus/a/set") == "nodus/a/set"
    assert transport.subscribe("nodus/a/set") == "nodus/a/set"

    assert transport.subscriptions == ["nodus/a/set"]


def test_transport_compact_discards_synced_entries():
    transport = MQTTTransport("broker.local", 1883)
    transport.publish("nodus/a", {"ok": 1}, retain=False)
    transport.publish("nodus/b", {"ok": 2}, retain=False)
    transport.subscribe("nodus/a/set")
    transport.subscribe("nodus/b/set")

    transport.compact(published_keep_from=1, subscriptions_keep_from=1)

    assert len(transport.published_messages) == 1
    assert transport.published_messages[0].topic == "nodus/b"
    assert transport.subscriptions == ["nodus/b/set"]


if __name__ == "__main__":
    _run_direct_tests()
