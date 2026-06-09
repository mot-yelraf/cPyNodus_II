"""Expose core runtime primitives through lazy package-level helpers.

Importing any ``cpynodus_ii.core`` submodule executes this package file first.
Keep this module lightweight so profile-specific runtime paths do not load
MQTT, network, NTP, or recovery helpers before they are actually needed.
"""


_LAZY_ATTRS = {
    "MQTTClientAdapter": ("cpynodus_ii.core.mqtt_client", "MQTTClientAdapter"),
    "MQTTClientSyncResult": (
        "cpynodus_ii.core.mqtt_client",
        "MQTTClientSyncResult",
    ),
    "NetworkStack": ("cpynodus_ii.core.network", "NetworkStack"),
    "NTPResult": ("cpynodus_ii.core.ntp", "NTPResult"),
    "NTPState": ("cpynodus_ii.core.ntp", "NTPState"),
    "RecoveryDecision": ("cpynodus_ii.core.recovery", "RecoveryDecision"),
    "RecoveryPolicy": ("cpynodus_ii.core.recovery", "RecoveryPolicy"),
    "RecoveryState": ("cpynodus_ii.core.recovery", "RecoveryState"),
    "StationConnectivityResult": (
        "cpynodus_ii.core.network",
        "StationConnectivityResult",
    ),
}


def _load_attr(module_name, attr_name):
    module = __import__(module_name, None, None, (attr_name,), 0)
    return getattr(module, attr_name)


def __getattr__(name):
    target = _LAZY_ATTRS.get(name)
    if target is not None:
        value = _load_attr(target[0], target[1])
        globals()[name] = value
        return value
    if name in {
        "autoreload",
        "config",
        "mqtt",
        "mqtt_client",
        "network",
        "ntp",
        "obfuscation",
        "plan",
        "reboot_log",
        "recovery",
        "recovery_log",
        "settings",
        "toml_compat",
    }:
        module_name = "cpynodus_ii.core.{}".format(name)
        module = __import__(module_name, None, None, (name,), 0)
        globals()[name] = module
        return module
    raise AttributeError(name)


def build_mqtt_client_adapter(*args, **kwargs):
    """Build an MQTT client adapter."""
    return _load_attr(
        "cpynodus_ii.core.mqtt_client",
        "build_mqtt_client_adapter",
    )(*args, **kwargs)


def close_mqtt_client(*args, **kwargs):
    """Close an MQTT client adapter."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "close_mqtt_client")(
        *args, **kwargs
    )


def connect_mqtt_client(*args, **kwargs):
    """Connect an MQTT client adapter."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "connect_mqtt_client")(
        *args, **kwargs
    )


def disconnect_mqtt_client(*args, **kwargs):
    """Disconnect an MQTT client adapter."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "disconnect_mqtt_client")(
        *args, **kwargs
    )


def poll_mqtt_client(*args, **kwargs):
    """Poll an MQTT client adapter."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "poll_mqtt_client")(
        *args, **kwargs
    )


def preflight_mqtt_broker(*args, **kwargs):
    """Resolve or probe an MQTT broker target."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "preflight_mqtt_broker")(
        *args, **kwargs
    )


def preflight_mqtt_broker_connect(*args, **kwargs):
    """Run an MQTT CONNACK preflight probe."""
    return _load_attr(
        "cpynodus_ii.core.mqtt_client",
        "preflight_mqtt_broker_connect",
    )(*args, **kwargs)


def preflight_mqtt_broker_tcp(*args, **kwargs):
    """Run a TCP preflight probe for MQTT."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "preflight_mqtt_broker_tcp")(
        *args, **kwargs
    )


def raw_mqtt_connect_enabled(*args, **kwargs):
    """Return whether raw MQTT connect is enabled."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "raw_mqtt_connect_enabled")(
        *args, **kwargs
    )


def sync_transport_to_client(*args, **kwargs):
    """Synchronize queued MQTT transport work to the client."""
    return _load_attr("cpynodus_ii.core.mqtt_client", "sync_transport_to_client")(
        *args, **kwargs
    )


def build_network_stack(*args, **kwargs):
    """Build the network stack."""
    return _load_attr("cpynodus_ii.core.network", "build_network_stack")(
        *args, **kwargs
    )


def network_error_signature(*args, **kwargs):
    """Return a compact network error signature."""
    return _load_attr("cpynodus_ii.core.network", "network_error_signature")(
        *args, **kwargs
    )


def network_link_is_ready(*args, **kwargs):
    """Return whether the network link is ready."""
    return _load_attr("cpynodus_ii.core.network", "network_link_is_ready")(
        *args, **kwargs
    )


def reconnect_network_stack(*args, **kwargs):
    """Reconnect the network stack."""
    return _load_attr("cpynodus_ii.core.network", "reconnect_network_stack")(
        *args, **kwargs
    )


def refresh_network_socket_artifacts(*args, **kwargs):
    """Refresh socket-pool artifacts for the network stack."""
    return _load_attr(
        "cpynodus_ii.core.network",
        "refresh_network_socket_artifacts",
    )(*args, **kwargs)


def refresh_network_stack(*args, **kwargs):
    """Refresh the network stack state."""
    return _load_attr("cpynodus_ii.core.network", "refresh_network_stack")(
        *args, **kwargs
    )


def start_network_mdns(*args, **kwargs):
    """Start mDNS for the network stack."""
    return _load_attr("cpynodus_ii.core.network", "start_network_mdns")(
        *args, **kwargs
    )


def stop_network_mdns(*args, **kwargs):
    """Stop mDNS for the network stack."""
    return _load_attr("cpynodus_ii.core.network", "stop_network_mdns")(
        *args, **kwargs
    )


def teardown_network_stack(*args, **kwargs):
    """Tear down the network stack."""
    return _load_attr("cpynodus_ii.core.network", "teardown_network_stack")(
        *args, **kwargs
    )


def verify_station_connectivity(*args, **kwargs):
    """Verify station connectivity with a bounded probe."""
    return _load_attr("cpynodus_ii.core.network", "verify_station_connectivity")(
        *args, **kwargs
    )


def maybe_sync_ntp(*args, **kwargs):
    """Run a bounded NTP sync attempt."""
    return _load_attr("cpynodus_ii.core.ntp", "maybe_sync_ntp")(*args, **kwargs)


def advance_recovery_state(*args, **kwargs):
    """Advance the recovery state machine."""
    return _load_attr("cpynodus_ii.core.recovery", "advance_recovery_state")(
        *args, **kwargs
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
    "StationConnectivityResult",
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
    "raw_mqtt_connect_enabled",
    "reconnect_network_stack",
    "refresh_network_socket_artifacts",
    "refresh_network_stack",
    "start_network_mdns",
    "stop_network_mdns",
    "sync_transport_to_client",
    "teardown_network_stack",
    "verify_station_connectivity",
]
