"""Core runtime primitives for cPyNodus_II."""

from cpynodus_ii.core.mqtt_client import (
    MQTTClientAdapter,
    MQTTClientSyncResult,
    build_mqtt_client_adapter,
    connect_mqtt_client,
    disconnect_mqtt_client,
    poll_mqtt_client,
    sync_transport_to_client,
)
from cpynodus_ii.core.network import (
    NetworkStack,
    build_network_stack,
    network_link_is_ready,
    reconnect_network_stack,
    refresh_network_stack,
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
    "connect_mqtt_client",
    "disconnect_mqtt_client",
    "maybe_sync_ntp",
    "network_link_is_ready",
    "poll_mqtt_client",
    "reconnect_network_stack",
    "refresh_network_stack",
    "sync_transport_to_client",
]
