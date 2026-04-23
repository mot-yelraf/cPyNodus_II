"""Provide a small MQTT transport facade for runtime coordination.

The transport model keeps publish, subscribe, and connection state simple so
feature modules can exchange MQTT work without depending directly on the vendor
client implementation.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PublishedMessage:
    """Capture one publish request for host-testable runtime integration."""

    topic: str
    payload: object
    retain: bool = False


@dataclass(frozen=True)
class ReceivedMessage:
    """Capture one received message for host-testable command intake."""

    topic: str
    payload_text: str


class MQTTTransport:
    """Capture transport intent without embedding feature logic."""

    def __init__(self, broker="", port=1883):
        self.broker = str(broker or "").strip()
        self.port = int(port or 1883)
        self.connect_requested = False
        self.connected = False
        self.connection_generation = 0
        self.published_messages = []
        self.subscriptions = []
        self.received_messages = []

    @classmethod
    def from_settings(cls, settings):
        config = settings.mqtt_config()
        return cls(
            broker=config.get("BROKER", ""),
            port=config.get("PORT", 1883),
        )

    def mark_connect_requested(self):
        self.connect_requested = True

    def mark_connected(self):
        self.connected = True
        self.connection_generation += 1
        return self.connection_generation

    def mark_disconnected(self):
        self.connected = False

    def compact(self, *, published_keep_from=0, subscriptions_keep_from=0):
        """Drop already-synced transport queues to limit long-run heap growth."""
        published_start = max(0, int(published_keep_from or 0))
        if published_start > 0:
            self.published_messages = self.published_messages[published_start:]
        subscriptions_start = max(0, int(subscriptions_keep_from or 0))
        if subscriptions_start > 0:
            self.subscriptions = self.subscriptions[subscriptions_start:]

    def target(self):
        return self.broker, self.port

    def publish(self, topic, payload, *, retain=False):
        normalized_payload = payload
        if isinstance(payload, dict):
            normalized_payload = dict(payload)
        message = PublishedMessage(
            topic=str(topic or "").strip(),
            payload=normalized_payload,
            retain=bool(retain),
        )
        self.published_messages.append(message)
        return message

    def subscribe(self, topic):
        topic = str(topic or "").strip()
        self.subscriptions.append(topic)
        return topic

    def receive(self, topic, payload_text):
        message = ReceivedMessage(
            topic=str(topic or "").strip(),
            payload_text=str(payload_text or ""),
        )
        self.received_messages.append(message)
        return message

    def drain_received(self):
        messages = list(self.received_messages)
        self.received_messages.clear()
        return messages
