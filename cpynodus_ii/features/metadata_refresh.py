"""Refresh saved metadata one serialized snapshot at a time.

A small dirty-topic mask coalesces mutations until command replies drain.
Build, collect, and serialize at this shallow boundary, then release the
payload dictionary before the MQTT adapter allocates its packet buffer.
"""

import gc
import json

from cpynodus_ii.features.payloads import (
    build_configuration_meta_payload,
    build_runtime_meta_payload,
    build_switch_meta_payload,
)
from cpynodus_ii.features.publish_cycle import PublishCycleResult
from cpynodus_ii.features.topics import mqtt_topic

# Bits represent pending compact, configuration, and switch snapshots.
_META = 1
_CONFIG = 2
_SWITCH = 4


def refresh_saved_metadata(
    transport,
    config,
    results=(),
    *,
    settings_root,
    version,
    active_broker="",
    ip_address="",
    switch_service=None,
    defer_build=False,
):
    """Queue at most one serialized snapshot after prior sends have drained."""
    required = _META | _CONFIG | (_SWITCH if config.switch.present else 0)
    pending = int(getattr(transport, "_metadata_refresh_pending", 0)) & required
    for result in results:
        if (
            result.command_type
            in ("config", "switch", "calibration", "calibration_session")
            and getattr(result, "persistence_mode", "") == "persisted"
            and not getattr(result, "duplicate", False)
            and result.phase == "published"
        ):
            pending |= required
    transport._metadata_refresh_pending = pending
    if (
        defer_build
        or not pending
        or not transport.connected
        or transport.published_messages
        or transport.subscriptions
        or settings_root is None
    ):
        return PublishCycleResult(phase="skipped", published_count=0, topics=())

    # Avoid retaining command/parser garbage while constructing this one payload.
    gc.collect()
    bit = _META if pending & _META else _CONFIG if pending & _CONFIG else _SWITCH
    try:
        device_id = (
            config.sensor.sensor_id
            or config.switch.device_id
            or config.network.hostname
        )
        if bit == _META:
            topic = mqtt_topic(config, device_id, "meta")
            payload = build_runtime_meta_payload(
                config,
                version=version,
                active_broker=active_broker,
                ip_address=ip_address,
                settings_root=settings_root,
            )
        elif bit == _CONFIG:
            topic = mqtt_topic(config, device_id, "meta", "config")
            payload = build_configuration_meta_payload(
                config, settings_root=settings_root
            )
        else:
            switch_snapshot = None
            if switch_service is not None:
                from cpynodus_ii.features.switch_service import snapshot_switch_states

                switch_snapshot = snapshot_switch_states(switch_service)
            topic = mqtt_topic(config, device_id, "meta", "switch")
            payload = build_switch_meta_payload(
                config, switch_snapshot, settings_root=settings_root
            )
            del switch_snapshot

        # Match the existing CircuitPython serializer, but allocate JSON and UTF-8
        # here rather than repeatedly in the deeper packet-building call chain.
        gc.collect()
        serialized = json.dumps(payload, separators=(",", ":"))
        del payload
        gc.collect()
        encoded = serialized.encode("utf-8")
        del serialized
        gc.collect()
        transport.publish(topic, encoded, retain=True)
    except (OSError, ValueError, MemoryError, RuntimeError) as exc:
        if (
            isinstance(exc, RuntimeError)
            and "pystack exhausted" not in str(exc).lower()
        ):
            raise
        return PublishCycleResult(
            phase="error",
            published_count=0,
            topics=(),
            errors=("metadata_snapshot_failed",),
        )
    # The adapter owns retry of this queued buffer. Remaining bits wait until
    # the queue is empty; a later edit re-dirties all relevant snapshots.
    transport._metadata_refresh_pending = pending & ~bit
    return PublishCycleResult(phase="published", published_count=1, topics=(topic,))
