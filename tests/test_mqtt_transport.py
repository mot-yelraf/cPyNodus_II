"""Tests for the lightweight MQTT transport facade."""

from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.settings import Settings


def _run_direct_tests():
    tests = (
        test_transport_reads_target_from_settings,
        test_transport_tracks_connect_request_without_feature_logic,
        test_transport_subscribe_deduplicates_topics,
        test_transport_records_publish_diagnostic,
        test_transport_records_loop_diagnostic,
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


def test_transport_records_publish_diagnostic():
    transport = MQTTTransport("broker.local", 1883)

    transport.record_publish_success(
        "nodus/a/data",
        payload_bytes=123,
        retain=True,
        socket_state="wrapped",
        client_connected="1",
        backcompat=0,
        now_monotonic=10.0,
    )

    diagnostic = transport.publish_diagnostic(now_monotonic=15.0)

    assert "last_pub_topic=nodus/a/data" in diagnostic
    assert "last_pub_age_s=5" in diagnostic
    assert "last_pub_bytes=123" in diagnostic
    assert "last_pub_retain=1" in diagnostic
    assert "last_pub_sock=wrapped" in diagnostic
    assert "last_pub_client_connected=1" in diagnostic
    assert "last_pub_backcompat=0" in diagnostic


def test_transport_records_loop_diagnostic():
    transport = MQTTTransport("broker.local", 1883)

    transport.record_loop_success(
        received_count=2,
        timeout=1.0,
        socket_state="wrapped",
        client_connected="1",
        backcompat=0,
        now_monotonic=20.0,
    )

    diagnostic = transport.loop_diagnostic(now_monotonic=25.0)

    assert "last_loop_age_s=5" in diagnostic
    assert "last_loop_received=2" in diagnostic
    assert "last_loop_timeout=1.0" in diagnostic
    assert "last_loop_sock=wrapped" in diagnostic
    assert "last_loop_client_connected=1" in diagnostic
    assert "last_loop_backcompat=0" in diagnostic


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
