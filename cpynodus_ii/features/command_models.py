"""Shared command data shapes for lazy command handlers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SwitchCommand:
    """Describe a parsed switch command."""

    channel_id: str
    desired_state: bool
    message_id: str = ""


@dataclass(frozen=True)
class DeviceConfigCommand:
    """Describe one parsed device config command."""

    message_id: str
    updates: tuple
    onboard_token: str = ""
    restart_requested: bool = False
    restart_mode: str = "soft"


@dataclass(frozen=True)
class CalibrationCommand:
    """Describe one parsed calibration command."""

    message_id: str
    action: str
    updates: tuple = ()
    reference_ph: object = None
    sample_interval_s: float = 10.0
    sample_count: int = 12


@dataclass(frozen=True)
class FwUpdateCommand:
    """Describe one parsed firmware update control command."""

    message_id: str
    command: str
    package_id: str = ""
    schema: str = ""
    session_id: str = ""
    manifest_sha256: str = ""
    key_id: str = ""


@dataclass(frozen=True)
class SoilPhCalibrationSession:
    """Describe an active soil pH sampling session."""

    message_id: str
    reference_ph: float
    sample_interval_s: float
    sample_count: int
    started_at: float
    next_sample_at: float
    samples: tuple = ()


@dataclass(frozen=True)
class CommandResult:
    """Describe the outcome of processing one inbound command."""

    phase: str
    topic: str
    command_type: str
    published_count: int
    errors: tuple = ()
    runtime_config: object = None
    message_id: str = ""
    duplicate: bool = False
    persistence_mode: str = ""
    requested_state: str = ""
    reboot_requested: bool = False
    reboot_mode: str = ""
    ntp_resync_requested: bool = False
