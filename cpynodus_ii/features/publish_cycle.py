"""Execute startup and steady-state MQTT publish cycles.

This module coordinates heartbeat, discovery, sensor, switch, and onboarding
publishes so callers can issue a complete publish pass without duplicating
topic-level logic.
"""

from dataclasses import dataclass
from time import time

from cpynodus_ii.features.payloads import (
    build_device_heartbeat_payload,
    build_homeassistant_discovery_plan,
    build_onboarding_hello_payload,
    build_runtime_meta_payload,
    build_sensor_availability_payload,
    build_sensor_data_payload,
    build_switch_event_payload,
    build_switch_state_payload,
    mqtt_topic,
)
from cpynodus_ii.ota.state import load_ota_state


@dataclass(frozen=True)
class PublishCycleResult:
    """Describe the publications emitted by one publish cycle."""

    phase: str
    published_count: int
    topics: tuple
    errors: tuple = ()


def publish_startup_cycle(
    transport,
    runtime_config,
    *,
    version,
    onboarding_state=None,
    sensor_snapshot=None,
    switch_snapshot=None,
    active_broker="",
):
    """Publish retained startup payloads for the current runtime state."""
    topics = []
    device_id = _device_id(runtime_config)
    meta = transport.publish(
        mqtt_topic(runtime_config, device_id, "meta"),
        build_runtime_meta_payload(
            runtime_config,
            version=version,
            active_broker=active_broker,
        ),
        retain=True,
    )
    topics.append(meta.topic)

    if runtime_config.sensor.present:
        if sensor_snapshot is not None and sensor_snapshot.phase == "ready":
            data = transport.publish(
                mqtt_topic(runtime_config, runtime_config.sensor.sensor_id, "data"),
                build_sensor_data_payload(runtime_config, sensor_snapshot),
                retain=False,
            )
            topics.append(data.topic)

    heartbeat = transport.publish(
        mqtt_topic(runtime_config, device_id, "status", "heartbeat"),
        build_device_heartbeat_payload(runtime_config, online=True),
        retain=True,
    )
    topics.append(heartbeat.topic)

    # Publish online availability early so stale retained offline status is
    # cleared even if later startup publishes fail.
    if runtime_config.sensor.present:
        availability = transport.publish(
            mqtt_topic(runtime_config, runtime_config.sensor.sensor_id, "availability"),
            build_sensor_availability_payload(runtime_config, online=True),
            retain=True,
        )
        topics.append(availability.topic)

    if runtime_config.switch.present:
        availability_timestamp = int(time())
        for channel in runtime_config.switch.channels:
            availability = transport.publish(
                mqtt_topic(runtime_config, channel.channel_id, "availability"),
                {
                    "schema": "nodus-availability/v1",
                    "channel_id": channel.channel_id,
                    "status": "online",
                    "timestamp": availability_timestamp,
                },
                retain=True,
            )
            topics.append(availability.topic)

    hello_payload = build_onboarding_hello_payload(
        runtime_config,
        onboarding_state,
        version=version,
    )
    if hello_payload is not None:
        hello = transport.publish(
            mqtt_topic(runtime_config, device_id, "onboard", "hello"),
            hello_payload,
            retain=False,
        )
        topics.append(hello.topic)

    discovery_plan = build_homeassistant_discovery_plan(
        runtime_config,
        sensor_snapshot=sensor_snapshot,
        retain=bool(
            getattr(runtime_config.homeassistant, "publish_discovery_retain", True)
        ),
        previous_topics=getattr(transport, "_ha_last_retained_discovery_topics", ()),
    )
    for topic, payload, retain_flag, is_clear in discovery_plan:
        message = transport.publish(topic, payload, retain=retain_flag)
        topics.append(message.topic)
        if not is_clear:
            continue
    if (
        str(getattr(runtime_config, "active_profile", "") or "").strip().lower()
        == "homeassistant"
    ):
        retained_topics = {
            topic
            for topic, _payload, retain_flag, is_clear in discovery_plan
            if retain_flag and not is_clear
        }
        transport._ha_last_retained_discovery_topics = retained_topics

    if runtime_config.switch.present:
        state_payloads = build_switch_state_payload(
            runtime_config, switch_snapshot or {}
        )
        for channel in runtime_config.switch.channels:
            payload = state_payloads[channel.key]
            message = transport.publish(
                mqtt_topic(runtime_config, channel.channel_id, "state"),
                payload,
                retain=True,
            )
            topics.append(message.topic)

    return PublishCycleResult(
        phase="published",
        published_count=len(topics),
        topics=tuple(topics),
        errors=(),
    )


def publish_sensor_cycle(transport, runtime_config, sensor_snapshot):
    """Publish one non-retained sensor data cycle when a ready snapshot exists."""
    if sensor_snapshot is None or sensor_snapshot.phase != "ready":
        errors = (
            getattr(sensor_snapshot, "errors", ())
            if sensor_snapshot is not None
            else ()
        )
        if not errors:
            errors = ("sensor_snapshot_not_ready",)
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=tuple(errors),
        )
    topic = mqtt_topic(runtime_config, runtime_config.sensor.sensor_id, "data")
    message = transport.publish(
        topic,
        build_sensor_data_payload(runtime_config, sensor_snapshot),
        retain=False,
    )
    return PublishCycleResult(
        phase="published",
        published_count=1,
        topics=(message.topic,),
        errors=(),
    )


def publish_shutdown_cycle(transport, runtime_config):
    """Publish retained offline status payloads before disconnect/shutdown."""
    topics = []
    device_id = _device_id(runtime_config)
    heartbeat = transport.publish(
        mqtt_topic(runtime_config, device_id, "status", "heartbeat"),
        build_device_heartbeat_payload(runtime_config, online=False),
        retain=True,
    )
    topics.append(heartbeat.topic)

    if runtime_config.sensor.present:
        availability = transport.publish(
            mqtt_topic(runtime_config, runtime_config.sensor.sensor_id, "availability"),
            build_sensor_availability_payload(runtime_config, online=False),
            retain=True,
        )
        topics.append(availability.topic)

    if runtime_config.switch.present:
        for channel in runtime_config.switch.channels:
            availability = transport.publish(
                mqtt_topic(runtime_config, channel.channel_id, "availability"),
                {
                    "schema": "nodus-availability/v1",
                    "channel_id": channel.channel_id,
                    "status": "offline",
                    "timestamp": heartbeat.payload["timestamp"],
                },
                retain=True,
            )
            topics.append(availability.topic)

    return PublishCycleResult(
        phase="published",
        published_count=len(topics),
        topics=tuple(topics),
        errors=(),
    )


def publish_availability_refresh_cycle(transport, runtime_config):
    """Republish retained online heartbeat and availability payloads."""
    topics = []
    device_id = _device_id(runtime_config)
    heartbeat = transport.publish(
        mqtt_topic(runtime_config, device_id, "status", "heartbeat"),
        build_device_heartbeat_payload(runtime_config, online=True),
        retain=True,
    )
    topics.append(heartbeat.topic)

    if runtime_config.sensor.present:
        availability = transport.publish(
            mqtt_topic(runtime_config, runtime_config.sensor.sensor_id, "availability"),
            build_sensor_availability_payload(runtime_config, online=True),
            retain=True,
        )
        topics.append(availability.topic)

    if runtime_config.switch.present:
        timestamp = int(time())
        for channel in runtime_config.switch.channels:
            payload = {
                "schema": "nodus-availability/v1",
                "channel_id": channel.channel_id,
                "status": "online",
                "timestamp": timestamp,
            }
            availability = transport.publish(
                mqtt_topic(runtime_config, channel.channel_id, "availability"),
                payload,
                retain=True,
            )
            topics.append(availability.topic)

    if not topics:
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=("availability_not_supported",),
        )
    return PublishCycleResult(
        phase="published",
        published_count=len(topics),
        topics=tuple(topics),
        errors=(),
    )


def publish_ota_completion_report(transport, runtime_config, *, settings_root=None):
    """Publish the post-reboot OTA result if an applied update is recorded."""
    if not settings_root:
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=(),
        )
    state = load_ota_state(_ota_state_path(settings_root))
    if state is None:
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=(),
        )
    if getattr(state, "phase", "") != "applied":
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=(),
        )
    device_id = _device_id(runtime_config)
    message = transport.publish(
        mqtt_topic(runtime_config, device_id, "fwupdate", "result"),
        {
            "schema": "nodus-fwupdate-result/v1",
            "message_id": "",
            "prepared": True,
            "applied": True,
            "phase": "applied",
            "package_id": str(getattr(state, "package_id", "") or ""),
            "prior_profile": str(getattr(state, "prior_profile", "") or ""),
            "error": "",
            "timestamp": int(time()),
        },
        retain=False,
    )
    return PublishCycleResult(
        phase="published",
        published_count=1,
        topics=(message.topic,),
        errors=(),
    )


def publish_switch_result(transport, runtime_config, apply_result, *, message_id=""):
    """Publish a compact switch apply result and retained state update."""
    if apply_result.phase != "ready":
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=apply_result.errors,
        )
    channel = None
    for item in runtime_config.switch.channels:
        if item.channel_id == apply_result.channel_id:
            channel = item
            break
    if channel is None:
        return PublishCycleResult(
            phase="error",
            published_count=0,
            topics=(),
            errors=("switch_channel_not_found",),
        )
    result_topic = mqtt_topic(runtime_config, channel.channel_id, "config", "result")
    event_topic = mqtt_topic(runtime_config, channel.channel_id, "event")
    state_topic = mqtt_topic(runtime_config, channel.channel_id, "state")
    result_payload = {
        "message_id": str(message_id or ""),
        "applied": True,
        "updated": 1,
        "duplicate": False,
        "error": "",
    }
    state_payload = "ON" if apply_result.applied_state else "OFF"
    result_message = transport.publish(result_topic, result_payload, retain=False)
    event_message = transport.publish(
        event_topic,
        build_switch_event_payload(
            runtime_config,
            channel,
            bool(apply_result.applied_state),
            message_id=message_id,
        ),
        retain=False,
    )
    state_message = transport.publish(state_topic, state_payload, retain=True)
    return PublishCycleResult(
        phase="published",
        published_count=3,
        topics=(result_message.topic, event_message.topic, state_message.topic),
        errors=(),
    )


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )


def _ota_state_path(root):
    root_text = str(root or ".")
    if root_text == "/":
        return "/_ota/state.json"
    if root_text.endswith("/"):
        return "{}_ota/state.json".format(root_text)
    return "{}/_ota/state.json".format(root_text)
