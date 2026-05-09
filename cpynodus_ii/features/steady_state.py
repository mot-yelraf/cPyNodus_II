"""Run one iteration of the normal steady-state firmware loop.

The helpers here coordinate inbound command handling, sensor reads, switch
updates, publish work, and background maintenance once the device has finished
bootstrapping.
"""

from dataclasses import dataclass

from cpynodus_ii.features.command_intake import (
    process_inbound_messages,
    process_soil_calibration_session,
    subscribe_device_runtime_topics,
    subscribe_runtime_topics,
)
from cpynodus_ii.features.publish_cycle import (
    publish_availability_refresh_cycle,
    publish_ota_completion_report,
    publish_sensor_cycle,
    publish_startup_cycle,
)
from cpynodus_ii.features.sensor_service import read_sensor_snapshot
from cpynodus_ii.features.web_services import load_onboarding_state


@dataclass(frozen=True)
class SteadyState:
    """Track loop-local state across steady-state iterations."""

    connection_generation: int = 0
    sensor_interval_s: float = 60.0
    availability_interval_s: float = 120.0
    last_sensor_publish_at: float = -1.0
    last_availability_publish_at: float = -1.0
    handled_message_ids: tuple = ()
    handled_message_id_limit: int = 64


@dataclass(frozen=True)
class SteadyStateResult:
    """Describe the work completed during one loop iteration."""

    state: SteadyState
    runtime_config: object
    subscribed_topics: tuple
    startup_publish_phase: str
    startup_published_count: int
    command_results: tuple
    sensor_publish_phase: str
    sensor_published_count: int
    availability_refresh_phase: str
    availability_refresh_published_count: int
    ota_status_phase: str
    ota_status_published_count: int
    command_published_count: int
    calibration_session_phase: str
    calibration_session_published_count: int
    total_published_count: int
    errors: tuple = ()


@dataclass(frozen=True)
class _SkippedPublishResult:
    """Represent a skipped publish cycle without dynamic type creation."""

    phase: str = "skipped"
    published_count: int = 0
    errors: tuple = ()


def run_steady_state_iteration(
    transport,
    runtime_config,
    switch_service,
    sensor_service=None,
    *,
    state=None,
    version="0.0.0",
    now_monotonic=0.0,
    active_broker="",
    settings_root=None,
    subscribe_switch_topics=True,
    publish_switch_startup=True,
    include_switch_meta_channels=True,
):
    """Process reconnect, queued commands, and cadence-gated sensor publish."""
    state = state or SteadyState()
    subscribed_topics = ()
    startup_result = _skipped_publish_result("startup_not_required")
    availability_result = _skipped_publish_result("availability_refresh_not_required")
    ota_status_result = _skipped_publish_result("ota_status_not_required")
    working_state = state

    if (
        transport.connected
        and transport.connection_generation != state.connection_generation
    ):
        sensor_snapshot = None
        switch_snapshot = {}
        onboarding_state = {}
        if sensor_service is not None:
            sensor_snapshot = read_sensor_snapshot(sensor_service, runtime_config)
        if switch_service is not None:
            from cpynodus_ii.features.switch_service import snapshot_switch_states

            switch_snapshot = snapshot_switch_states(switch_service)
        if settings_root is not None:
            onboarding_state = load_onboarding_state(settings_root)
        if subscribe_switch_topics:
            subscribed_topics = subscribe_runtime_topics(transport, runtime_config)
        else:
            subscribed_topics = subscribe_device_runtime_topics(
                transport, runtime_config
            )
        startup_result = publish_startup_cycle(
            transport,
            runtime_config,
            version=version,
            onboarding_state=onboarding_state,
            sensor_snapshot=sensor_snapshot,
            switch_snapshot=switch_snapshot,
            active_broker=active_broker,
            publish_switch_startup=publish_switch_startup,
            include_switch_meta_channels=include_switch_meta_channels,
        )
        ota_status_result = publish_ota_completion_report(
            transport,
            runtime_config,
            settings_root=settings_root,
        )
        last_sensor_publish_at = state.last_sensor_publish_at
        if sensor_snapshot is not None and sensor_snapshot.phase == "ready":
            last_sensor_publish_at = float(now_monotonic)
        last_availability_publish_at = state.last_availability_publish_at
        if startup_result.phase == "published" and (
            runtime_config.sensor.present or runtime_config.switch.present
        ):
            last_availability_publish_at = float(now_monotonic)
        working_state = SteadyState(
            connection_generation=transport.connection_generation,
            sensor_interval_s=state.sensor_interval_s,
            availability_interval_s=state.availability_interval_s,
            last_sensor_publish_at=last_sensor_publish_at,
            last_availability_publish_at=last_availability_publish_at,
            handled_message_ids=state.handled_message_ids,
            handled_message_id_limit=state.handled_message_id_limit,
        )

    command_results = process_inbound_messages(
        transport,
        runtime_config,
        switch_service,
        handled_message_ids=working_state.handled_message_ids,
        settings_root=settings_root,
    )
    updated_runtime_config = runtime_config
    handled_message_ids = list(working_state.handled_message_ids)
    for result in command_results:
        if result.runtime_config is not None:
            updated_runtime_config = result.runtime_config
        if result.message_id and result.message_id not in handled_message_ids:
            handled_message_ids.append(result.message_id)
    if len(handled_message_ids) > int(working_state.handled_message_id_limit or 0):
        handled_message_ids = handled_message_ids[
            -int(working_state.handled_message_id_limit or 0) :
        ]
    command_published_count = sum(
        int(result.published_count or 0) for result in command_results
    )
    errors = []
    errors.extend(startup_result.errors)
    errors.extend(ota_status_result.errors)
    for result in command_results:
        errors.extend(result.errors)
    calibration_session_result = process_soil_calibration_session(
        transport,
        updated_runtime_config,
        sensor_service,
        now_monotonic=now_monotonic,
        settings_root=settings_root,
    )
    if calibration_session_result.runtime_config is not None:
        updated_runtime_config = calibration_session_result.runtime_config
    errors.extend(calibration_session_result.errors)

    sensor_result = _skipped_publish_result("sensor_poll_interval_not_elapsed")
    should_poll_sensor = (
        sensor_service is not None
        and runtime_config.sensor.present
        and transport.connected
        and (
            working_state.last_sensor_publish_at < 0
            or (float(now_monotonic) - float(working_state.last_sensor_publish_at))
            >= float(working_state.sensor_interval_s)
        )
    )
    if should_poll_sensor:
        sensor_snapshot = read_sensor_snapshot(sensor_service, runtime_config)
        sensor_result = publish_sensor_cycle(transport, runtime_config, sensor_snapshot)
        errors.extend(sensor_result.errors)
        if sensor_result.phase == "published":
            working_state = SteadyState(
                connection_generation=working_state.connection_generation,
                sensor_interval_s=working_state.sensor_interval_s,
                availability_interval_s=working_state.availability_interval_s,
                last_sensor_publish_at=float(now_monotonic),
                last_availability_publish_at=working_state.last_availability_publish_at,
                handled_message_ids=tuple(handled_message_ids),
                handled_message_id_limit=working_state.handled_message_id_limit,
            )
    else:
        working_state = SteadyState(
            connection_generation=working_state.connection_generation,
            sensor_interval_s=working_state.sensor_interval_s,
            availability_interval_s=working_state.availability_interval_s,
            last_sensor_publish_at=working_state.last_sensor_publish_at,
            last_availability_publish_at=working_state.last_availability_publish_at,
            handled_message_ids=tuple(handled_message_ids),
            handled_message_id_limit=working_state.handled_message_id_limit,
        )
    should_refresh_availability = (
        transport.connected
        and (
            updated_runtime_config.sensor.present
            or updated_runtime_config.switch.present
        )
        and (
            working_state.last_availability_publish_at < 0
            or (
                float(now_monotonic) - float(working_state.last_availability_publish_at)
            )
            >= float(working_state.availability_interval_s)
        )
    )
    if should_refresh_availability:
        availability_result = publish_availability_refresh_cycle(
            transport,
            updated_runtime_config,
        )
        errors.extend(availability_result.errors)
        if availability_result.phase == "published":
            working_state = SteadyState(
                connection_generation=working_state.connection_generation,
                sensor_interval_s=working_state.sensor_interval_s,
                availability_interval_s=working_state.availability_interval_s,
                last_sensor_publish_at=working_state.last_sensor_publish_at,
                last_availability_publish_at=float(now_monotonic),
                handled_message_ids=working_state.handled_message_ids,
                handled_message_id_limit=working_state.handled_message_id_limit,
            )

    return SteadyStateResult(
        state=working_state,
        runtime_config=updated_runtime_config,
        subscribed_topics=tuple(subscribed_topics),
        startup_publish_phase=startup_result.phase,
        startup_published_count=startup_result.published_count,
        command_results=tuple(command_results),
        sensor_publish_phase=sensor_result.phase,
        sensor_published_count=sensor_result.published_count,
        availability_refresh_phase=availability_result.phase,
        availability_refresh_published_count=availability_result.published_count,
        ota_status_phase=ota_status_result.phase,
        ota_status_published_count=ota_status_result.published_count,
        command_published_count=command_published_count,
        calibration_session_phase=calibration_session_result.phase,
        calibration_session_published_count=calibration_session_result.published_count,
        total_published_count=(
            startup_result.published_count
            + ota_status_result.published_count
            + command_published_count
            + calibration_session_result.published_count
            + sensor_result.published_count
            + availability_result.published_count
        ),
        errors=tuple(errors),
    )


def _skipped_publish_result(error):
    return _SkippedPublishResult(errors=(error,))
