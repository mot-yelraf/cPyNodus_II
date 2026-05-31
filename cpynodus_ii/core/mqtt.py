"""Provide a small MQTT transport facade for runtime coordination.

The transport model keeps publish, subscribe, and connection state simple so
feature modules can exchange MQTT work without depending directly on the vendor
client implementation.
"""

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PublishedMessage:
    """Capture one publish request for host-testable runtime integration."""

    topic: str
    payload: object
    retain: bool = False


class ReceivedMessage:
    """Capture one received message for host-testable command intake."""

    __slots__ = ("topic", "payload_text")

    def __init__(self, topic, payload_text):
        self.topic = topic
        self.payload_text = payload_text


class MQTTTransport:
    """Capture transport intent without embedding feature logic."""

    def __init__(self, broker="", port=1883):
        self.broker = str(broker or "").strip()
        self.port = int(port or 1883)
        self.connect_requested = False
        self.connected = False
        self.connection_generation = 0
        self.last_connected_at = -1.0
        self.last_success_at = -1.0
        self.last_disconnected_at = -1.0
        self.last_disconnect_reason = ""
        self.last_publish_at = -1.0
        self.last_publish_topic = ""
        self.last_publish_bytes = -1
        self.last_publish_retain = 0
        self.last_publish_socket_state = ""
        self.last_publish_client_connected = ""
        self.last_publish_backcompat = -1
        self.last_loop_at = -1.0
        self.last_loop_received = -1
        self.last_loop_timeout = -1.0
        self.last_loop_socket_state = ""
        self.last_loop_client_connected = ""
        self.last_loop_backcompat = -1
        self.last_ack_at = -1.0
        self.last_ack_kind = ""
        self.last_ack_topic = ""
        self.last_ack_stage = ""
        self.last_ack_packet_id = -1
        self.last_ack_elapsed_ms = -1
        self.last_ack_timeout_s = -1.0
        self.last_ack_timeout_set = -1
        self.last_ack_socket_state = ""
        self.last_ack_socket_caps = ""
        self.published_messages = []
        self.subscriptions = []
        self.received_messages = []
        self.subscription_retry_after = 0.0

    @classmethod
    def from_settings(cls, settings):
        config = settings.mqtt_config()
        return cls(
            broker=config.get("BROKER", ""),
            port=config.get("PORT", 1883),
        )

    def mark_connect_requested(self):
        self.connect_requested = True

    def mark_connected(self, now_monotonic=None):
        self.connected = True
        self.connection_generation += 1
        now_value = _monotonic_value(now_monotonic)
        self.last_connected_at = now_value
        self.last_success_at = now_value
        return self.connection_generation

    def mark_disconnected(self, now_monotonic=None, reason=""):
        self.connected = False
        self.last_disconnected_at = _monotonic_value(now_monotonic)
        if reason:
            self.last_disconnect_reason = str(reason or "").strip()

    def mark_success(self, now_monotonic=None):
        """Record a successful MQTT client operation."""
        self.last_success_at = _monotonic_value(now_monotonic)
        return self.last_success_at

    def record_publish_success(
        self,
        topic,
        *,
        payload_bytes=-1,
        retain=False,
        socket_state="",
        client_connected="",
        backcompat=-1,
        now_monotonic=None,
    ):
        """Record compact diagnostics for the last successful publish."""
        self.last_publish_at = _monotonic_value(now_monotonic)
        self.last_publish_topic = str(topic or "").strip()
        try:
            self.last_publish_bytes = int(payload_bytes)
        except Exception:
            self.last_publish_bytes = -1
        self.last_publish_retain = 1 if retain else 0
        self.last_publish_socket_state = str(socket_state or "").strip()
        self.last_publish_client_connected = str(client_connected or "").strip()
        try:
            self.last_publish_backcompat = int(backcompat)
        except Exception:
            self.last_publish_backcompat = -1

    def publish_diagnostic(self, now_monotonic=None):
        """Return compact last-publish diagnostics for recovery logs."""
        if not self.last_publish_topic:
            return ""
        age = "unknown"
        now_value = _monotonic_value(now_monotonic)
        if now_value >= 0.0 and self.last_publish_at >= 0.0:
            age = str(int(max(0.0, now_value - self.last_publish_at)))
        socket_state = self.last_publish_socket_state or "unknown"
        client_connected = self.last_publish_client_connected or "unknown"
        return (
            "last_pub_topic={} last_pub_age_s={} last_pub_bytes={} "
            "last_pub_retain={} last_pub_sock={} last_pub_client_connected={} "
            "last_pub_backcompat={}"
        ).format(
            self.last_publish_topic,
            age,
            self.last_publish_bytes,
            self.last_publish_retain,
            socket_state,
            client_connected,
            self.last_publish_backcompat,
        )

    def record_loop_success(
        self,
        *,
        received_count=0,
        timeout=-1.0,
        socket_state="",
        client_connected="",
        backcompat=-1,
        now_monotonic=None,
    ):
        """Record compact diagnostics for the last successful MQTT loop."""
        self.last_loop_at = _monotonic_value(now_monotonic)
        try:
            self.last_loop_received = int(received_count)
        except Exception:
            self.last_loop_received = -1
        try:
            self.last_loop_timeout = float(timeout)
        except Exception:
            self.last_loop_timeout = -1.0
        self.last_loop_socket_state = str(socket_state or "").strip()
        self.last_loop_client_connected = str(client_connected or "").strip()
        try:
            self.last_loop_backcompat = int(backcompat)
        except Exception:
            self.last_loop_backcompat = -1

    def loop_diagnostic(self, now_monotonic=None):
        """Return compact last-loop diagnostics for recovery logs."""
        if self.last_loop_at < 0.0:
            return ""
        age = "unknown"
        now_value = _monotonic_value(now_monotonic)
        if now_value >= 0.0:
            age = str(int(max(0.0, now_value - self.last_loop_at)))
        socket_state = self.last_loop_socket_state or "unknown"
        client_connected = self.last_loop_client_connected or "unknown"
        return (
            "last_loop_age_s={} last_loop_received={} last_loop_timeout={} "
            "last_loop_sock={} last_loop_client_connected={} "
            "last_loop_backcompat={}"
        ).format(
            age,
            self.last_loop_received,
            self.last_loop_timeout,
            socket_state,
            client_connected,
            self.last_loop_backcompat,
        )

    def record_ack_read(
        self,
        kind,
        topic,
        *,
        stage="",
        packet_id=-1,
        elapsed_ms=-1,
        timeout_s=-1.0,
        timeout_set=False,
        socket_state="",
        socket_caps="",
        now_monotonic=None,
    ):
        """Record compact diagnostics for the last raw MQTT ACK read."""
        self.last_ack_at = _monotonic_value(now_monotonic)
        self.last_ack_kind = str(kind or "").strip()
        self.last_ack_topic = str(topic or "").strip()
        self.last_ack_stage = str(stage or "").strip()
        try:
            self.last_ack_packet_id = int(packet_id)
        except Exception:
            self.last_ack_packet_id = -1
        try:
            self.last_ack_elapsed_ms = int(elapsed_ms)
        except Exception:
            self.last_ack_elapsed_ms = -1
        try:
            self.last_ack_timeout_s = float(timeout_s)
        except Exception:
            self.last_ack_timeout_s = -1.0
        self.last_ack_timeout_set = 1 if timeout_set else 0
        self.last_ack_socket_state = str(socket_state or "").strip()
        self.last_ack_socket_caps = str(socket_caps or "").strip()

    def ack_diagnostic(self, now_monotonic=None):
        """Return compact last-ACK diagnostics for recovery logs."""
        if not self.last_ack_kind:
            return ""
        age = "unknown"
        now_value = _monotonic_value(now_monotonic)
        if now_value >= 0.0 and self.last_ack_at >= 0.0:
            age = str(int(max(0.0, now_value - self.last_ack_at)))
        socket_state = self.last_ack_socket_state or "unknown"
        socket_caps = self.last_ack_socket_caps or "unknown"
        return (
            "last_ack_kind={} last_ack_topic={} last_ack_age_s={} "
            "last_ack_stage={} last_ack_packet_id={} "
            "last_ack_elapsed_ms={} last_ack_timeout_s={} "
            "last_ack_timeout_set={} last_ack_sock={} last_ack_caps={}"
        ).format(
            self.last_ack_kind,
            self.last_ack_topic,
            age,
            self.last_ack_stage or "unknown",
            self.last_ack_packet_id,
            self.last_ack_elapsed_ms,
            _timeout_text(self.last_ack_timeout_s),
            self.last_ack_timeout_set,
            socket_state,
            socket_caps,
        )

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
        if topic in self.subscriptions:
            return topic
        self.subscriptions.append(topic)
        return topic

    def defer_subscription_retry(self, now_monotonic, delay_s):
        self.subscription_retry_after = float(now_monotonic or 0.0) + float(
            delay_s or 0.0
        )

    def subscription_retry_ready(self, now_monotonic):
        return float(now_monotonic or 0.0) >= float(
            self.subscription_retry_after or 0.0
        )

    def clear_subscription_retry(self):
        self.subscription_retry_after = 0.0

    def receive(self, topic, payload_text):
        if topic is None:
            topic_text = ""
        elif isinstance(topic, str):
            topic_text = topic.strip()
        else:
            topic_text = str(topic or "").strip()
        if payload_text is None:
            payload_value = ""
        elif isinstance(payload_text, str):
            payload_value = payload_text
        else:
            payload_value = str(payload_text or "")
        message = ReceivedMessage(topic_text, payload_value)
        self.received_messages.append(message)
        return message

    def drain_received(self):
        messages = list(self.received_messages)
        self.received_messages.clear()
        return messages


def _monotonic_value(now_monotonic=None):
    if now_monotonic is not None:
        try:
            return float(now_monotonic)
        except Exception:
            pass
    try:
        return float(time.monotonic())
    except Exception:
        return -1.0


def _timeout_text(value):
    try:
        timeout = float(value)
    except Exception:
        return "unknown"
    if timeout < 0.0:
        return "unknown"
    int_timeout = int(timeout)
    if timeout == int_timeout:
        return str(int_timeout)
    return str(timeout)
