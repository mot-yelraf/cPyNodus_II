"""Minimal NTP sync helpers for CircuitPython runtime."""

from dataclasses import dataclass


DEFAULT_NTP_SERVER = "pool.ntp.org"


@dataclass(frozen=True)
class NTPState:
    """Track NTP sync progress across loop iterations."""

    phase: str = "idle"
    server: str = ""
    datetime_text: str = ""
    last_attempt_at: float = -1.0
    last_sync_at: float = -1.0
    errors: tuple = ()


@dataclass(frozen=True)
class NTPResult:
    """Describe one NTP sync decision or attempt."""

    phase: str
    state: NTPState
    errors: tuple = ()


def maybe_sync_ntp(
    runtime_config,
    network_stack,
    *,
    state=None,
    now_monotonic=0.0,
    retry_interval_s=60.0,
    sync_interval_s=86400.0,
    modules=None,
):
    """Attempt an NTP sync when enabled, due, and hostname resolution is ready."""
    state = state or NTPState()
    server = str(runtime_config.time.ntp_server or DEFAULT_NTP_SERVER).strip()
    now_value = float(now_monotonic or 0.0)
    if not runtime_config.ntp_enabled:
        return NTPResult(phase="skipped", state=state, errors=("ntp_disabled",))
    if getattr(network_stack, "phase", "") != "ready" or network_stack.socket_pool is None:
        return NTPResult(phase="skipped", state=state, errors=("network_not_ready",))
    if not _sync_due(state, now_value, retry_interval_s, sync_interval_s):
        return NTPResult(phase="skipped", state=state, errors=("ntp_not_due",))

    next_state = NTPState(
        phase="pending",
        server=server,
        datetime_text=state.datetime_text,
        last_attempt_at=now_value,
        last_sync_at=state.last_sync_at,
        errors=(),
    )
    resolve_error = _preflight_hostname(network_stack.socket_pool, server)
    if resolve_error:
        return NTPResult(
            phase="deferred",
            state=NTPState(
                phase="deferred",
                server=server,
                datetime_text=state.datetime_text,
                last_attempt_at=now_value,
                last_sync_at=state.last_sync_at,
                errors=(resolve_error,),
            ),
            errors=(resolve_error,),
        )

    try:
        ntp_client = _build_ntp_client(
            network_stack.socket_pool,
            server=server,
            tz_offset=runtime_config.time.tz_offset,
            modules=modules,
        )
        current_datetime = getattr(ntp_client, "datetime", None)
        if callable(current_datetime):
            current_datetime = current_datetime()
        if current_datetime is None:
            raise RuntimeError("ntp_datetime_unavailable")
        _set_rtc_datetime(current_datetime, modules=modules)
        datetime_text = _format_datetime(current_datetime)
    except Exception as exc:
        error = "ntp_sync_failed:{}".format(exc)
        return NTPResult(
            phase="error",
            state=NTPState(
                phase="error",
                server=server,
                datetime_text=state.datetime_text,
                last_attempt_at=now_value,
                last_sync_at=state.last_sync_at,
                errors=(error,),
            ),
            errors=(error,),
        )

    return NTPResult(
        phase="synced",
        state=NTPState(
            phase="synced",
            server=server,
            datetime_text=datetime_text,
            last_attempt_at=now_value,
            last_sync_at=now_value,
            errors=(),
        ),
        errors=(),
    )


def _sync_due(state, now_monotonic, retry_interval_s, sync_interval_s):
    if state.last_sync_at >= 0.0:
        return (float(now_monotonic) - float(state.last_sync_at)) >= float(sync_interval_s)
    if state.last_attempt_at >= 0.0:
        return (float(now_monotonic) - float(state.last_attempt_at)) >= float(retry_interval_s)
    return True


def _preflight_hostname(socket_pool, server):
    if _looks_like_ip_literal(server):
        return ""
    getaddrinfo = getattr(socket_pool, "getaddrinfo", None)
    if not callable(getaddrinfo):
        return ""
    try:
        getaddrinfo(server, 123)
    except Exception as exc:
        return "ntp_dns_unready:{}".format(exc)
    return ""


def _build_ntp_client(socket_pool, *, server, tz_offset, modules=None):
    ntp_cls = None
    if isinstance(modules, dict):
        ntp_cls = modules.get("ntp_cls")
    if ntp_cls is None:
        import adafruit_ntp  # type: ignore

        ntp_cls = adafruit_ntp.NTP
    return ntp_cls(
        socket_pool,
        server=server,
        tz_offset=_tz_offset_hours(tz_offset),
        socket_timeout=1.5,
    )


def _set_rtc_datetime(current_datetime, *, modules=None):
    rtc_factory = None
    if isinstance(modules, dict):
        rtc_factory = modules.get("rtc_factory")
    if rtc_factory is None:
        import rtc  # type: ignore

        rtc_factory = rtc.RTC
    rtc_factory().datetime = current_datetime


def _looks_like_ip_literal(value):
    text = str(value or "").strip()
    if not text:
        return False
    parts = text.split(".")
    if len(parts) == 4:
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False
    if ":" in text:
        return True
    return False


def _format_datetime(current_datetime):
    try:
        members = tuple(current_datetime)
    except Exception:
        return str(current_datetime)
    if len(members) < 6:
        return str(current_datetime)
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
        int(members[0]),
        int(members[1]),
        int(members[2]),
        int(members[3]),
        int(members[4]),
        int(members[5]),
    )


def _tz_offset_hours(tz_offset):
    return float(tz_offset or 0) / 3600.0
