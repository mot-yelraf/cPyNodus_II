"""Expose the feature-layer entry points used by the application runtime.

The feature package groups sensor, switch, web, command, payload, and
steady-state helpers that sit above the core runtime primitives.
"""

from cpynodus_ii.features.sensor import SensorInitialization, plan_sensor_initialization
from cpynodus_ii.features.sensor_runtime import SensorRuntime, build_sensor_runtime
from cpynodus_ii.features.sensor_service import (
    SensorService,
    SensorSnapshot,
    read_sensor_snapshot,
    start_sensor_service,
    stop_sensor_service,
)
from cpynodus_ii.features.payloads import (
    build_calibration_ack_payload,
    build_calibration_result_payload,
    build_calibration_status_payload,
    build_config_ack_payload,
    build_config_result_payload,
    build_device_heartbeat_payload,
    build_meta_patch_payload,
    build_runtime_meta_payload,
    build_sensor_availability_payload,
    build_sensor_data_payload,
    build_switch_event_payload,
    build_switch_state_payload,
)
from cpynodus_ii.features.publish_cycle import (
    PublishCycleResult,
    publish_sensor_cycle,
    publish_shutdown_cycle,
    publish_startup_cycle,
    publish_switch_result,
)
from cpynodus_ii.features.command_intake import (
    CommandResult,
    CalibrationCommand,
    DeviceConfigCommand,
    SoilPhCalibrationSession,
    parse_calibration_command,
    parse_device_config_command,
    SwitchCommand,
    parse_switch_command,
    process_inbound_messages,
    process_calibration_message,
    process_device_config_message,
    process_soil_calibration_session,
    process_switch_command_message,
    subscribe_runtime_topics,
)
from cpynodus_ii.features.steady_state import (
    SteadyState,
    SteadyStateResult,
    run_steady_state_iteration,
)
from cpynodus_ii.features.switch import (
    SwitchChannelInitialization,
    SwitchInitialization,
    plan_switch_initialization,
)
from cpynodus_ii.features.switch_runtime import (
    SwitchChannelRuntime,
    SwitchRuntime,
    build_switch_runtime,
)
from cpynodus_ii.features.switch_service import (
    SwitchApplyResult,
    SwitchChannelService,
    SwitchService,
    apply_switch_state,
    snapshot_switch_states,
    start_switch_service,
    stop_switch_service,
)
from cpynodus_ii.features.web_services import (
    ItaotInitResult,
    apply_itaot_init_payload,
    bootstrap_routes_enabled,
    build_itaot_init_updates,
    build_itaot_meta_payload,
    load_onboarding_state,
    normalize_itaot_init_payload,
)
from cpynodus_ii.features.web_config import (
    WebConfigDecision,
    WebConfigResult,
    apply_web_config_updates,
    apply_web_switch_override,
    classify_web_update,
)
from cpynodus_ii.features.web_routes import WebRoute, build_web_route_table, route_paths
from cpynodus_ii.features.web_handlers import (
    build_setup_payload,
    build_status_payload,
    handle_switch_state_request,
    handle_web_config_request,
)
from cpynodus_ii.features.web_runtime import WebRuntimeController

__all__ = [
    "SensorInitialization",
    "SensorRuntime",
    "SensorService",
    "SensorSnapshot",
    "SwitchChannelInitialization",
    "SwitchChannelRuntime",
    "SwitchChannelService",
    "SwitchInitialization",
    "SwitchRuntime",
    "SwitchApplyResult",
    "SwitchService",
    "PublishCycleResult",
    "CalibrationCommand",
    "CommandResult",
    "DeviceConfigCommand",
    "SoilPhCalibrationSession",
    "apply_switch_state",
    "build_device_heartbeat_payload",
    "build_calibration_ack_payload",
    "build_calibration_result_payload",
    "build_calibration_status_payload",
    "build_config_ack_payload",
    "build_config_result_payload",
    "build_meta_patch_payload",
    "build_sensor_runtime",
    "build_runtime_meta_payload",
    "build_sensor_availability_payload",
    "build_sensor_data_payload",
    "build_switch_event_payload",
    "build_switch_runtime",
    "build_switch_state_payload",
    "build_itaot_init_updates",
    "build_itaot_meta_payload",
    "build_setup_payload",
    "build_status_payload",
    "build_web_route_table",
    "parse_calibration_command",
    "parse_device_config_command",
    "parse_switch_command",
    "plan_sensor_initialization",
    "plan_switch_initialization",
    "publish_sensor_cycle",
    "publish_shutdown_cycle",
    "publish_startup_cycle",
    "publish_switch_result",
    "process_inbound_messages",
    "process_calibration_message",
    "process_device_config_message",
    "process_soil_calibration_session",
    "process_switch_command_message",
    "read_sensor_snapshot",
    "run_steady_state_iteration",
    "snapshot_switch_states",
    "start_sensor_service",
    "start_switch_service",
    "SteadyState",
    "stop_sensor_service",
    "stop_switch_service",
    "SteadyStateResult",
    "subscribe_runtime_topics",
    "SwitchCommand",
    "ItaotInitResult",
    "apply_itaot_init_payload",
    "apply_web_config_updates",
    "apply_web_switch_override",
    "bootstrap_routes_enabled",
    "classify_web_update",
    "handle_switch_state_request",
    "handle_web_config_request",
    "load_onboarding_state",
    "normalize_itaot_init_payload",
    "route_paths",
    "WebConfigDecision",
    "WebConfigResult",
    "WebRuntimeController",
    "WebRoute",
]
