from cpynodus_ii.core.mqtt import MQTTTransport
from cpynodus_ii.core.settings import Settings


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
