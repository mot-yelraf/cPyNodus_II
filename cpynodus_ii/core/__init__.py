"""Expose the core runtime primitives used across cPyNodus_II.

The core package contains low-level configuration, network, MQTT, time sync,
recovery, reboot logging, and persistence helpers that higher-level feature
modules build on.
"""

from cpynodus_ii.core.mqtt_client import (
    MQTTClientAdapter,
    MQTTClientSyncResult,
    build_mqtt_client_adapter,
    close_mqtt_client,
    connect_mqtt_client,
    disconnect_mqtt_client,
    poll_mqtt_client,
    preflight_mqtt_broker,
    preflight_mqtt_broker_connect,
    preflight_mqtt_broker_tcp,
    sync_transport_to_client,
)
from cpynodus_ii.core.network import (
    NetworkStack,
    build_network_stack,
    network_error_signature,
    network_link_is_ready,
    reconnect_network_stack,
    refresh_network_stack,
    teardown_network_stack,
)
from cpynodus_ii.core.ntp import NTPResult, NTPState, maybe_sync_ntp
from cpynodus_ii.core.recovery import (
    RecoveryDecision,
    RecoveryPolicy,
    RecoveryState,
    advance_recovery_state,
)

__all__ = [
    "MQTTClientAdapter",
    "MQTTClientSyncResult",
    "NetworkStack",
    "NTPResult",
    "NTPState",
    "RecoveryDecision",
    "RecoveryPolicy",
    "RecoveryState",
    "advance_recovery_state",
    "build_mqtt_client_adapter",
    "build_network_stack",
    "close_mqtt_client",
    "connect_mqtt_client",
    "disconnect_mqtt_client",
    "maybe_sync_ntp",
    "network_error_signature",
    "network_link_is_ready",
    "poll_mqtt_client",
    "preflight_mqtt_broker",
    "preflight_mqtt_broker_connect",
    "preflight_mqtt_broker_tcp",
    "reconnect_network_stack",
    "refresh_network_stack",
    "sync_transport_to_client",
    "teardown_network_stack",
]
