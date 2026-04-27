"""Handle best-effort RTC synchronization through NTP.

NTP is treated as an opportunistic service rather than a hard dependency, so
these helpers report sync state and transient errors without destabilizing the
rest of the runtime.
"""

from dataclasses import dataclass


DEFAULT_NTP_SERVER = "us.pool.ntp.org"
DEFAULT_NTP_MAX_DNS_FAILURES = 3
DEFAULT_NTP_COOLDOWN_S = 3600.0


@dataclass(frozen=True)
class NTPState:
    """Track NTP sync progress across loop iterations."""

    phase: str = "idle"
    server: str = ""
    datetime_text: str = ""
    last_attempt_at: float = -1.0
    last_sync_at: float = -1.0
    failure_count: int = 0
    failure_window: int = 1
    cooldown_until: float = -1.0
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
    max_dns_failures=DEFAULT_NTP_MAX_DNS_FAILURES,
    cooldown_interval_s=DEFAULT_NTP_COOLDOWN_S,
    modules=None,
):
    """Attempt an NTP sync when enabled, due, and hostname resolution is ready."""
    state = state or NTPState()
    server = str(runtime_config.time.ntp_server or "").strip() or DEFAULT_NTP_SERVER
    targets = _ntp_targets(runtime_config.time)
    now_value = float(now_monotonic or 0.0)
    same_server = state.server in targets
    if not runtime_config.ntp_enabled:
        return NTPResult(phase="skipped", state=state, errors=("ntp_disabled",))
    if (
        getattr(network_stack, "phase", "") != "ready"
        or network_stack.socket_pool is None
    ):
        return NTPResult(phase="skipped", state=state, errors=("network_not_ready",))
    if same_server and state.phase == "cooldown":
        if float(now_value) < float(state.cooldown_until):
            return NTPResult(
                phase="skipped",
                state=state,
                errors=("ntp_cooldown_active",),
            )
        state = NTPState(
            phase="idle",
            server=state.server,
            datetime_text=state.datetime_text,
            last_attempt_at=state.last_attempt_at,
            last_sync_at=state.last_sync_at,
            failure_count=0,
            failure_window=2,
            cooldown_until=-1.0,
            errors=state.errors,
        )
    if same_server and state.phase == "disabled":
        return NTPResult(
            phase="skipped",
            state=state,
            errors=("ntp_disabled_after_dns_failures",),
        )
    if not _sync_due(state, now_value, retry_interval_s, sync_interval_s):
        return NTPResult(phase="skipped", state=state, errors=("ntp_not_due",))

    failure_count = state.failure_count if same_server else 0
    failure_window = state.failure_window if same_server else 1
    errors = []
    for target in targets:
        resolve_error = _preflight_hostname(network_stack.socket_pool, target)
        if resolve_error:
            errors.append(resolve_error)
            continue

        try:
            ntp_client = _build_ntp_client(
                network_stack.socket_pool,
                server=target,
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
            error = "ntp_sync_failed:{}:{}".format(target, exc)
            errors.append(error)
            continue

        return NTPResult(
            phase="synced",
            state=NTPState(
                phase="synced",
                server=target,
                datetime_text=datetime_text,
                last_attempt_at=now_value,
                last_sync_at=now_value,
                failure_count=0,
                failure_window=1,
                cooldown_until=-1.0,
                errors=(),
            ),
            errors=(),
        )

    failure_count = int(failure_count or 0) + 1
    if int(max_dns_failures or 0) > 0 and failure_count >= int(max_dns_failures or 0):
        if int(failure_window or 1) <= 1:
            return NTPResult(
                phase="cooldown",
                state=NTPState(
                    phase="cooldown",
                    server=server,
                    datetime_text=state.datetime_text,
                    last_attempt_at=now_value,
                    last_sync_at=state.last_sync_at,
                    failure_count=0,
                    failure_window=2,
                    cooldown_until=now_value + float(cooldown_interval_s or 0.0),
                    errors=tuple(errors),
                ),
                errors=tuple(errors),
            )
        return NTPResult(
            phase="disabled",
            state=NTPState(
                phase="disabled",
                server=server,
                datetime_text=state.datetime_text,
                last_attempt_at=now_value,
                last_sync_at=state.last_sync_at,
                failure_count=failure_count,
                failure_window=failure_window,
                cooldown_until=-1.0,
                errors=tuple(errors),
            ),
            errors=tuple(errors),
        )

    phase = (
        "deferred" if _all_dns_not_found(errors) else _failure_phase(errors)
    )
    return NTPResult(
        phase=phase,
        state=NTPState(
            phase=phase,
            server=server,
            datetime_text=state.datetime_text,
            last_attempt_at=now_value,
            last_sync_at=state.last_sync_at,
            failure_count=failure_count,
            failure_window=failure_window,
            cooldown_until=-1.0,
            errors=tuple(errors),
        ),
        errors=tuple(errors),
    )


def _ntp_targets(time_config):
    primary = str(getattr(time_config, "ntp_server", "") or "").strip() or DEFAULT_NTP_SERVER
    fallback = str(getattr(time_config, "ntp_server_ip", "") or "").strip()
    if fallback and fallback != primary:
        return (primary, fallback)
    return (primary,)


def _failure_phase(errors):
    for error in errors:
        if not str(error or "").startswith("ntp_dns_unready:"):
            return "error"
    return "deferred"


def _all_dns_not_found(errors):
    if not errors:
        return False
    for error in errors:
        if not _is_dns_not_found_error(error):
            return False
    return True


def _is_dns_not_found_error(error):
    text = str(error or "")
    return ":-2" in text or "Errno -2" in text or text.strip() == "-2"


def _sync_due(state, now_monotonic, retry_interval_s, sync_interval_s):
    if state.last_sync_at >= 0.0:
        return (float(now_monotonic) - float(state.last_sync_at)) >= float(
            sync_interval_s
        )
    if state.last_attempt_at >= 0.0:
        return (float(now_monotonic) - float(state.last_attempt_at)) >= float(
            retry_interval_s
        )
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
