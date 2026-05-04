"""Serve the temporary Nodus OTA HTTP transfer endpoints.

This module runs only in OTA mode, after MQTT has been stopped and the device
has rebooted into a reduced runtime. It accepts a manifest, stages files under
`_ota/stage`, supports 1024-byte chunk uploads to avoid large heap
allocations, verifies SHA256 while streaming from disk, backs up live files,
applies staged files, and schedules the final reboot.
"""

import hashlib
import json
import os
import time

from cpynodus_ii.ota.state import FwUpdateState, load_ota_state, save_ota_state

_HTTP_STATUS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}


class OtaHttpController:
    """Bridge OTA state and file handlers onto an HTTP server instance.

    The controller owns the OTA HTTP lifecycle for one temporary runtime:
    status, manifest begin, file staging, commit, abort, and delayed reboot.
    It keeps state compact and streams staged-file verification so larger
    Python modules can be updated on Pico2 W without full-file allocations.
    """

    def __init__(
        self,
        runtime_config,
        network_stack,
        ota_state,
        *,
        settings_root=".",
        version="",
        server_module=None,
        reboot_callback=None,
        reboot_delay_s=5,
        time_module=None,
        log_fn=None,
    ):
        self.runtime_config = runtime_config
        self.network_stack = network_stack
        self.ota_state = ota_state if isinstance(ota_state, FwUpdateState) else None
        self.settings_root = settings_root or "."
        self.version = str(version or "")
        self.server_module = server_module
        self.reboot_callback = reboot_callback
        self.reboot_delay_s = max(0, min(9, int(reboot_delay_s or 0)))
        self.time_module = time_module or time
        self.log_fn = log_fn
        self.server = None
        self.phase = "new"
        self.errors = ()
        self._route_paths = ()
        self._reboot_due_at = -1.0
        self._reboot_requested = False

    def start(self):
        """Initialize the backing HTTP server and register OTA routes."""
        if getattr(self.network_stack, "socket_pool", None) is None:
            self.phase = "unavailable"
            self.errors = ("ota_socket_pool_unavailable",)
            return self
        module = self._resolve_server_module()
        if module is None:
            self.phase = "unavailable"
            self.errors = ("adafruit_httpserver_unavailable",)
            return self
        self.server_module = module
        server = self._build_server(module)
        if server is None:
            self.phase = "error"
            self.errors = ("ota_server_init_failed",)
            return self
        self.server = server
        self._register_routes()
        self.phase = "ready"
        self._log("phase=ready routes={}".format(",".join(self._route_paths)))
        return self

    def poll(self):
        """Poll the OTA server once and run any due scheduled reboot."""
        if self.phase != "ready" or self.server is None:
            return self
        poll = getattr(self.server, "poll", None)
        if not callable(poll):
            self.phase = "error"
            self.errors = ("ota_server_poll_unavailable",)
            return self
        try:
            poll()
        except Exception as exc:
            self.phase = "error"
            self.errors = ("ota_poll_failed", str(exc))
            self._log("poll failed error={}".format(_exception_text(exc)))
            self._maybe_reboot()
            return self
        self._maybe_reboot()
        return self

    @property
    def route_paths(self):
        return self._route_paths

    def status_payload(self):
        """Return the current compact OTA status payload."""
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        return build_ota_status_payload(
            self.runtime_config,
            self.network_stack,
            state,
            version=self.version,
            http_phase=self.phase,
            errors=self.errors,
        )

    def _resolve_server_module(self):
        if self.server_module is not None:
            return self.server_module
        try:
            import adafruit_httpserver as module  # type: ignore
        except ImportError:
            return None
        return module

    def _build_server(self, module):
        server_cls = getattr(module, "Server", None)
        if server_cls is None:
            return None
        try:
            server = server_cls(self.network_stack.socket_pool, debug=False)
        except TypeError:
            server = server_cls(self.network_stack.socket_pool)
        if hasattr(server, "headers"):
            server.headers = {
                "Access-Control-Allow-Origin": "*",
                "Connection": "close",
                "Cache-Control": "no-store",
            }
        if hasattr(server, "socket_timeout"):
            server.socket_timeout = 1
        if hasattr(server, "request_buffer_size"):
            server.request_buffer_size = 1024
        start = getattr(server, "start", None)
        if callable(start):
            start("0.0.0.0", int(self.runtime_config.network.http_port or 8000))
        return server

    def _register_routes(self):
        self._route_paths = (
            "/ota/status",
            "/ota/begin",
            "/ota/file",
            "/ota/file/begin",
            "/ota/file/chunk",
            "/ota/file/end",
            "/ota/commit",
            "/ota/abort",
        )
        route = getattr(self.server, "route", None)
        if not callable(route):
            self.phase = "error"
            self.errors = ("ota_route_registration_unavailable",)
            return

        @route("/ota/status", methods=["GET"])
        def _status(request):
            return self._json_response(request, self.status_payload())

        @route("/ota/begin", methods=["POST"])
        def _begin(request):
            manifest = _parse_json_body(request)
            if manifest is None:
                return self._json_response(
                    request,
                    _error_payload("invalid_json", "ready"),
                    status_code=400,
                )
            payload, status_code = self._handle_begin(manifest)
            return self._json_response(request, payload, status_code=status_code)

        @route("/ota/file", methods=["PUT"])
        def _file(request):
            try:
                path = _request_path_arg(request)
                body = _request_body_bytes(request)
                self._log("file received path={} bytes={}".format(path, len(body)))
                payload, status_code = self._handle_file(path, body)
                return self._json_response(request, payload, status_code=status_code)
            except Exception as exc:
                self._log("file exception error={}".format(_exception_text(exc)))
                return self._json_response(
                    request,
                    _error_payload("file_handler_exception", "staging"),
                    status_code=500,
                )

        @route("/ota/file/begin", methods=["POST"])
        def _file_begin(request):
            path = _request_path_arg(request)
            payload, status_code = self._handle_file_begin(path)
            return self._json_response(request, payload, status_code=status_code)

        @route("/ota/file/chunk", methods=["PUT"])
        def _file_chunk(request):
            try:
                path = _request_path_arg(request)
                offset = _request_int_arg(request, "offset", -1)
                body = _request_body_bytes(request)
                payload, status_code = self._handle_file_chunk(path, offset, body)
                return self._json_response(request, payload, status_code=status_code)
            except Exception as exc:
                self._log("chunk exception error={}".format(_exception_text(exc)))
                return self._json_response(
                    request,
                    _error_payload("chunk_handler_exception", "staging"),
                    status_code=500,
                )

        @route("/ota/file/end", methods=["POST"])
        def _file_end(request):
            path = _request_path_arg(request)
            payload, status_code = self._handle_file_end(path)
            return self._json_response(request, payload, status_code=status_code)

        @route("/ota/commit", methods=["POST"])
        def _commit(request):
            payload, status_code = self._handle_commit()
            return self._json_response(request, payload, status_code=status_code)

        @route("/ota/abort", methods=["POST"])
        def _abort(request):
            state = (
                load_ota_state(_ota_state_path(self.settings_root))
                or self.ota_state
            )
            aborted = FwUpdateState(
                prior_profile=getattr(state, "prior_profile", "") or "",
                package_id=getattr(state, "package_id", "") or "",
                phase="aborted",
            )
            _remove_tree(_ota_stage_path(self.settings_root))
            _remove_ota_tmp_files(self.settings_root)
            save_ota_state(aborted, _ota_state_path(self.settings_root))
            self.ota_state = aborted
            return self._json_response(
                request,
                {
                    "accepted": True,
                    "phase": "aborted",
                    "package_id": aborted.package_id,
                },
            )

    def _handle_begin(self, manifest):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        error = _validate_manifest_for_begin(manifest, state)
        if error:
            self._log("begin rejected error={}".format(error))
            return _error_payload(error, getattr(state, "phase", "ready")), 400
        next_state = FwUpdateState(
            prior_profile=getattr(state, "prior_profile", "") or "",
            package_id=str(manifest.get("package_id", "") or ""),
            phase="staging",
        )
        _cleanup_package_workspace(self.settings_root)
        save_ota_state(next_state, _ota_state_path(self.settings_root))
        _write_json_file(_ota_manifest_path(self.settings_root), manifest)
        self.ota_state = next_state
        self._log(
            "begin accepted package={} files={}".format(
                next_state.package_id,
                len(manifest.get("files", ()) or ()),
            )
        )
        return (
            {
                "accepted": True,
                "phase": "staging",
                "package_id": next_state.package_id,
                "files": len(manifest.get("files", ()) or ()),
            },
            200,
        )

    def _handle_file(self, path, body):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        if getattr(state, "phase", "") != "staging":
            self._log("file rejected path={} error=ota_not_staging".format(path))
            return _error_payload("ota_not_staging", getattr(state, "phase", "")), 400
        safe_path = _normalize_package_path(path)
        if not safe_path:
            self._log("file rejected path={} error=file_path_invalid".format(path))
            return _error_payload("file_path_invalid", "staging"), 400
        manifest = _read_json_file(_ota_manifest_path(self.settings_root))
        if manifest is None:
            self._log("file rejected path={} error=manifest_missing".format(safe_path))
            return _error_payload("manifest_missing", "staging"), 400
        entry = _manifest_file_entry(manifest, safe_path)
        if entry is None:
            self._log(
                "file rejected path={} error=file_not_in_manifest".format(safe_path)
            )
            return _error_payload("file_not_in_manifest", "staging"), 400
        expected_size = int(entry.get("size", -1) or -1)
        expected_sha = str(entry.get("sha256", "") or "")
        actual_size = len(body)
        actual_sha = _sha256_hex(body)
        if actual_size != expected_size:
            self._log(
                "file rejected path={} error=file_size_mismatch".format(safe_path)
            )
            return _error_payload("file_size_mismatch", "staging"), 400
        if actual_sha != expected_sha:
            self._log("file rejected path={} error=sha256_mismatch".format(safe_path))
            return _error_payload("sha256_mismatch", "staging"), 400
        stage_path = _join_root(self.settings_root, "_ota/stage/{}".format(safe_path))
        _ensure_parent_dirs(stage_path)
        try:
            with open(stage_path, "wb") as handle:
                handle.write(body)
        except OSError:
            self._log("file rejected path={} error=file_stage_failed".format(safe_path))
            return _error_payload("file_stage_failed", "staging"), 503
        self._log("file accepted path={} bytes={}".format(safe_path, actual_size))
        return (
            {
                "accepted": True,
                "phase": "staging",
                "path": safe_path,
                "size": actual_size,
                "sha256": actual_sha,
            },
            200,
        )

    def _handle_file_begin(self, path):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        if getattr(state, "phase", "") != "staging":
            return _error_payload("ota_not_staging", getattr(state, "phase", "")), 400
        safe_path = _normalize_package_path(path)
        if not safe_path:
            return _error_payload("file_path_invalid", "staging"), 400
        manifest = _read_json_file(_ota_manifest_path(self.settings_root))
        if manifest is None:
            return _error_payload("manifest_missing", "staging"), 400
        entry = _manifest_file_entry(manifest, safe_path)
        if entry is None:
            return _error_payload("file_not_in_manifest", "staging"), 400
        stage_path = _join_root(self.settings_root, "_ota/stage/{}".format(safe_path))
        _ensure_parent_dirs(stage_path)
        try:
            with open(stage_path, "wb"):
                pass
        except OSError:
            return _error_payload("file_stage_failed", "staging"), 503
        self._log("file begin path={} bytes={}".format(safe_path, entry.get("size", 0)))
        return (
            {
                "accepted": True,
                "phase": "staging",
                "path": safe_path,
                "offset": 0,
                "size": int(entry.get("size", 0) or 0),
            },
            200,
        )

    def _handle_file_chunk(self, path, offset, body):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        if getattr(state, "phase", "") != "staging":
            return _error_payload("ota_not_staging", getattr(state, "phase", "")), 400
        safe_path = _normalize_package_path(path)
        if not safe_path:
            return _error_payload("file_path_invalid", "staging"), 400
        if int(offset) < 0:
            return _error_payload("chunk_offset_invalid", "staging"), 400
        manifest = _read_json_file(_ota_manifest_path(self.settings_root))
        if manifest is None:
            return _error_payload("manifest_missing", "staging"), 400
        entry = _manifest_file_entry(manifest, safe_path)
        if entry is None:
            return _error_payload("file_not_in_manifest", "staging"), 400
        stage_path = _join_root(self.settings_root, "_ota/stage/{}".format(safe_path))
        current_size = _file_size(stage_path)
        if current_size != int(offset):
            return _error_payload("chunk_offset_mismatch", "staging"), 409
        expected_size = int(entry.get("size", -1) or -1)
        next_offset = current_size + len(body)
        if next_offset > expected_size:
            return _error_payload("chunk_size_exceeds_file", "staging"), 400
        try:
            with open(stage_path, "ab") as handle:
                handle.write(body)
        except OSError:
            return _error_payload("chunk_stage_failed", "staging"), 503
        return (
            {
                "accepted": True,
                "phase": "staging",
                "path": safe_path,
                "offset": next_offset,
                "chunk": len(body),
            },
            200,
        )

    def _handle_file_end(self, path):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        if getattr(state, "phase", "") != "staging":
            return _error_payload("ota_not_staging", getattr(state, "phase", "")), 400
        safe_path = _normalize_package_path(path)
        if not safe_path:
            return _error_payload("file_path_invalid", "staging"), 400
        manifest = _read_json_file(_ota_manifest_path(self.settings_root))
        if manifest is None:
            return _error_payload("manifest_missing", "staging"), 400
        entry = _manifest_file_entry(manifest, safe_path)
        if entry is None:
            return _error_payload("file_not_in_manifest", "staging"), 400
        stage_path = _join_root(self.settings_root, "_ota/stage/{}".format(safe_path))
        verify_started = self._monotonic()
        size, actual_sha = _file_size_sha256(stage_path)
        verify_elapsed = self._elapsed_s(verify_started)
        expected_size = int(entry.get("size", -1) or -1)
        expected_sha = str(entry.get("sha256", "") or "")
        if size < 0:
            return _error_payload("staged_file_missing", "staging"), 400
        if size != expected_size:
            return _error_payload("file_size_mismatch", "staging"), 400
        if actual_sha != expected_sha:
            return _error_payload("sha256_mismatch", "staging"), 400
        self._log(
            "file accepted path={} bytes={} verify_s={:.1f}".format(
                safe_path,
                size,
                verify_elapsed,
            )
        )
        return (
            {
                "accepted": True,
                "phase": "staging",
                "path": safe_path,
                "size": size,
                "sha256": actual_sha,
            },
            200,
        )

    def _handle_commit(self):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        if getattr(state, "phase", "") != "staging":
            self._log("commit rejected error=ota_not_staging")
            return _error_payload("ota_not_staging", getattr(state, "phase", "")), 400
        manifest = _read_json_file(_ota_manifest_path(self.settings_root))
        if manifest is None:
            self._log("commit rejected error=manifest_missing")
            return _error_payload("manifest_missing", "staging"), 400
        verify_started = self._monotonic()
        error = _verify_staged_manifest_files(self.settings_root, manifest)
        verify_elapsed = self._elapsed_s(verify_started)
        if error:
            self._log("commit rejected error={}".format(error))
            return _error_payload(error, "staging"), 400
        self._log("commit verify complete elapsed_s={:.1f}".format(verify_elapsed))
        apply_started = self._monotonic()
        error = _apply_staged_manifest_files(self.settings_root, manifest)
        apply_elapsed = self._elapsed_s(apply_started)
        if error:
            self._log("commit rejected error={}".format(error))
            return _error_payload(error, "staging"), 503
        self._log("commit apply complete elapsed_s={:.1f}".format(apply_elapsed))
        _remove_tree(_ota_stage_path(self.settings_root))
        _remove_ota_tmp_files(self.settings_root)
        applied_state = FwUpdateState(
            prior_profile=getattr(state, "prior_profile", "") or "",
            package_id=getattr(state, "package_id", "") or "",
            phase="applied_pending_boot",
        )
        save_ota_state(applied_state, _ota_state_path(self.settings_root))
        self.ota_state = applied_state
        self._schedule_reboot()
        self._log(
            "commit accepted package={} files={} verify_s={:.1f} apply_s={:.1f} "
            "reboot_delay_s={}".format(
                applied_state.package_id,
                len(manifest.get("files", ()) or ()),
                verify_elapsed,
                apply_elapsed,
                self.reboot_delay_s,
            )
        )
        return (
            {
                "accepted": True,
                "phase": "applied_pending_boot",
                "package_id": applied_state.package_id,
                "files": len(manifest.get("files", ()) or ()),
                "rebooting": bool(self.reboot_callback is not None),
                "reboot_delay_s": self.reboot_delay_s,
            },
            200,
        )

    def _schedule_reboot(self):
        if self.reboot_callback is None:
            return
        try:
            now = float(self.time_module.monotonic())
        except Exception:
            now = 0.0
        self._reboot_due_at = now + float(self.reboot_delay_s)
        self._reboot_requested = True
        self._log("reboot scheduled delay_s={}".format(self.reboot_delay_s))

    def _maybe_reboot(self):
        if not self._reboot_requested or self.reboot_callback is None:
            return
        try:
            now = float(self.time_module.monotonic())
        except Exception:
            now = self._reboot_due_at
        if now < float(self._reboot_due_at):
            return
        self._reboot_requested = False
        self._log("reboot executing")
        self.reboot_callback()

    def _log(self, message):
        if callable(self.log_fn):
            self.log_fn("ota_http", message)

    def _monotonic(self):
        try:
            return float(self.time_module.monotonic())
        except Exception:
            return 0.0

    def _elapsed_s(self, started):
        now = self._monotonic()
        elapsed = now - float(started or 0)
        return elapsed if elapsed >= 0 else 0.0

    def _json_response(self, request, payload, *, status_code=200):
        response_cls = getattr(self.server_module, "JSONResponse", None)
        status = (status_code, _HTTP_STATUS.get(status_code, "OK"))
        if response_cls is not None:
            return response_cls(request, payload, status=status)
        response_cls = getattr(self.server_module, "Response", None)
        return response_cls(
            request,
            json.dumps(payload),
            content_type="application/json",
            status=status,
        )


def build_ota_status_payload(
    runtime_config,
    network_stack,
    ota_state,
    *,
    version="",
    http_phase="",
    errors=(),
):
    """Build the compact JSON payload returned by `/ota/status`."""
    state = ota_state if isinstance(ota_state, FwUpdateState) else FwUpdateState()
    return {
        "schema": "nodus-ota-status/v1",
        "version": str(version or ""),
        "phase": state.phase,
        "package_id": state.package_id,
        "prior_profile": state.prior_profile,
        "http": {
            "phase": str(http_phase or ""),
            "routes": [
                "/ota/status",
                "/ota/begin",
                "/ota/file",
                "/ota/file/begin",
                "/ota/file/chunk",
                "/ota/file/end",
                "/ota/commit",
                "/ota/abort",
            ],
        },
        "network": {
            "hostname": runtime_config.network.hostname,
            "ssid": getattr(network_stack, "ssid", "") or "",
            "ipv4addr": getattr(network_stack, "ip_address", "") or "",
        },
        "errors": list(errors or ()),
    }


def _validate_manifest_for_begin(manifest, ota_state):
    if manifest.get("schema") != "nodus-ota/v1":
        return "manifest_schema_invalid"
    package_id = str(manifest.get("package_id", "") or "").strip()
    if not package_id:
        return "package_id_missing"
    expected_package_id = str(getattr(ota_state, "package_id", "") or "").strip()
    if expected_package_id and package_id != expected_package_id:
        return "package_id_mismatch"
    files = manifest.get("files", ())
    if not isinstance(files, list):
        return "manifest_files_invalid"
    for entry in files:
        if not isinstance(entry, dict):
            return "manifest_file_invalid"
        if not str(entry.get("path", "") or "").strip():
            return "manifest_file_path_missing"
        if not _normalize_package_path(entry.get("path", "")):
            return "manifest_file_path_invalid"
        if int(entry.get("size", -1) or -1) < 0:
            return "manifest_file_size_invalid"
        if len(str(entry.get("sha256", "") or "")) != 64:
            return "manifest_file_sha256_invalid"
    return ""


def _parse_json_body(request):
    try:
        raw = getattr(request, "body", b"")
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        text = str(raw or "").strip()
        if not text:
            return {}
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _request_body_bytes(request):
    raw = getattr(request, "body", b"")
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    return str(raw or "").encode("utf-8")


def _request_path_arg(request):
    query_params = getattr(request, "query_params", None)
    query_get = getattr(query_params, "get", None)
    if callable(query_get):
        try:
            value = str(query_get("path", "") or "")
        except Exception:
            value = ""
        if value:
            return value
    for attr_name in ("query", "query_string"):
        query = str(getattr(request, attr_name, "") or "")
        value = _query_arg(query, "path")
        if value:
            return value
    header_value = _request_header(request, "X-Nodus-File-Path")
    if header_value:
        return header_value
    return ""


def _request_int_arg(request, key, default):
    value = ""
    query_params = getattr(request, "query_params", None)
    query_get = getattr(query_params, "get", None)
    if callable(query_get):
        try:
            value = str(query_get(key, "") or "")
        except Exception:
            value = ""
    if not value:
        for attr_name in ("query", "query_string"):
            query = str(getattr(request, attr_name, "") or "")
            value = _query_arg(query, key)
            if value:
                break
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _request_header(request, name):
    headers = getattr(request, "headers", None)
    header_get = getattr(headers, "get", None)
    if callable(header_get):
        for key in (name, str(name or "").lower(), str(name or "").title()):
            try:
                value = str(header_get(key, "") or "")
            except Exception:
                value = ""
            if value:
                return value
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key or "").lower() == str(name or "").lower():
                return str(value or "")
    return ""


def _query_arg(query, key):
    text = str(query or "").lstrip("?")
    for part in text.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        if name == key:
            return value.replace("%2F", "/").replace("%2f", "/")
    return ""


def _normalize_package_path(path):
    text = str(path or "").replace("\\", "/").strip()
    if not text or text.startswith("/"):
        return ""
    parts = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            return ""
        parts.append(part)
    return "/".join(parts)


def _manifest_file_entry(manifest, path):
    for entry in manifest.get("files", ()) or ():
        if _normalize_package_path(entry.get("path", "")) == path:
            return entry
    return None


def _verify_staged_manifest_files(root, manifest):
    for entry in manifest.get("files", ()) or ():
        safe_path = _normalize_package_path(entry.get("path", ""))
        if not safe_path:
            return "manifest_file_path_invalid"
        staged_path = _join_root(root, "_ota/stage/{}".format(safe_path))
        size, actual_sha = _file_size_sha256(staged_path)
        if size < 0:
            return "staged_file_missing"
        expected_size = int(entry.get("size", -1) or -1)
        if size != expected_size:
            return "staged_file_size_mismatch"
        expected_sha = str(entry.get("sha256", "") or "")
        if actual_sha != expected_sha:
            return "staged_file_sha256_mismatch"
    return ""


def _apply_staged_manifest_files(root, manifest):
    applied = []
    backups = []
    for entry in manifest.get("files", ()) or ():
        safe_path = _normalize_package_path(entry.get("path", ""))
        if not safe_path:
            return "manifest_file_path_invalid"
        staged_path = _join_root(root, "_ota/stage/{}".format(safe_path))
        live_path = _join_root(root, safe_path)
        backup_path = _join_root(root, "_ota/backup/{}".format(safe_path))
        try:
            if _path_exists(live_path):
                _copy_file(live_path, backup_path)
                backups.append((backup_path, live_path))
            _copy_file(staged_path, live_path)
            applied.append(live_path)
        except OSError:
            _restore_backups(backups)
            return "file_apply_failed"
    return ""


def _cleanup_package_workspace(root):
    _remove_tree(_ota_stage_path(root))
    _remove_tree(_ota_backup_path(root))
    _remove_ota_tmp_files(root)


def _remove_ota_tmp_files(root):
    ota_dir = _join_root(root, "_ota")
    try:
        names = os.listdir(ota_dir)
    except OSError:
        return
    for name in names:
        text = str(name or "")
        if not text.endswith(".tmp"):
            continue
        try:
            os.remove(_join_root(root, "_ota/{}".format(text)))
        except OSError:
            pass


def _remove_tree(path):
    try:
        names = os.listdir(path)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    for name in names:
        _remove_tree(_join(path, name))
    try:
        os.rmdir(path)
    except OSError:
        pass


def _restore_backups(backups):
    for backup_path, live_path in reversed(tuple(backups or ())):
        try:
            _copy_file(backup_path, live_path)
        except OSError:
            pass


def _path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _file_size(path):
    try:
        stat = os.stat(path)
        return int(stat[6])
    except (OSError, IndexError, TypeError, ValueError):
        return -1


def _file_size_sha256(path):
    hasher = _new_sha256_hasher()
    if hasher is None:
        payload = _read_binary_file(path)
        if payload is None:
            return -1, ""
        return len(payload), _sha256_fallback(payload).hex()
    size = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(512)
                if not chunk:
                    break
                size += len(chunk)
                hasher.update(chunk)
    except OSError:
        return -1, ""
    return size, hasher.hexdigest()


def _copy_file(src_path, dst_path):
    _ensure_parent_dirs(dst_path)
    with open(src_path, "rb") as source:
        with open(dst_path, "wb") as target:
            while True:
                chunk = source.read(512)
                if not chunk:
                    break
                target.write(chunk)


def _read_binary_file(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _read_json_file(path):
    try:
        with open(path, "r") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_file(path, payload):
    _ensure_parent_dirs(path)
    tmp_path = "{}.tmp".format(path)
    with open(tmp_path, "w") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")))
        handle.write("\n")
    try:
        os.rename(tmp_path, path)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(tmp_path, path)


def _ensure_parent_dirs(path):
    current = ""
    for part in str(path or "").replace("\\", "/").split("/")[:-1]:
        if not part:
            current = "/"
            continue
        current = (
            "{}{}".format(current, part)
            if current == "/"
            else _join(current, part)
        )
        try:
            os.mkdir(current)
        except OSError:
            pass


def _join(left, right):
    if not left:
        return right
    if left.endswith("/"):
        return "{}{}".format(left, right)
    return "{}/{}".format(left, right)


def _error_payload(error, phase):
    return {
        "accepted": False,
        "phase": str(phase or ""),
        "error": str(error or ""),
    }


def _exception_text(exc):
    return "{}:{}".format(type(exc).__name__, exc)


def _sha256_hex(data):
    digest = _hashlib_sha256(data)
    if digest is not None:
        return digest
    return _sha256_fallback(data).hex()


def _hashlib_sha256(data):
    hasher = _new_sha256_hasher()
    if hasher is not None:
        hasher.update(data)
        return hasher.hexdigest()
    return None


def _new_sha256_hasher():
    sha256 = getattr(hashlib, "sha256", None)
    if callable(sha256):
        try:
            return sha256()
        except Exception:
            pass
    new_hash = getattr(hashlib, "new", None)
    if callable(new_hash):
        try:
            return new_hash("sha256")
        except Exception:
            pass
    return None


def _sha256_fallback(data):
    payload = bytes(data or b"")
    length_bits = (len(payload) * 8) & 0xFFFFFFFFFFFFFFFF
    payload += b"\x80"
    while (len(payload) % 64) != 56:
        payload += b"\x00"
    payload += bytes(
        (
            (length_bits >> 56) & 0xFF,
            (length_bits >> 48) & 0xFF,
            (length_bits >> 40) & 0xFF,
            (length_bits >> 32) & 0xFF,
            (length_bits >> 24) & 0xFF,
            (length_bits >> 16) & 0xFF,
            (length_bits >> 8) & 0xFF,
            length_bits & 0xFF,
        )
    )
    h = [
        0x6A09E667,
        0xBB67AE85,
        0x3C6EF372,
        0xA54FF53A,
        0x510E527F,
        0x9B05688C,
        0x1F83D9AB,
        0x5BE0CD19,
    ]
    k = (
        0x428A2F98,
        0x71374491,
        0xB5C0FBCF,
        0xE9B5DBA5,
        0x3956C25B,
        0x59F111F1,
        0x923F82A4,
        0xAB1C5ED5,
        0xD807AA98,
        0x12835B01,
        0x243185BE,
        0x550C7DC3,
        0x72BE5D74,
        0x80DEB1FE,
        0x9BDC06A7,
        0xC19BF174,
        0xE49B69C1,
        0xEFBE4786,
        0x0FC19DC6,
        0x240CA1CC,
        0x2DE92C6F,
        0x4A7484AA,
        0x5CB0A9DC,
        0x76F988DA,
        0x983E5152,
        0xA831C66D,
        0xB00327C8,
        0xBF597FC7,
        0xC6E00BF3,
        0xD5A79147,
        0x06CA6351,
        0x14292967,
        0x27B70A85,
        0x2E1B2138,
        0x4D2C6DFC,
        0x53380D13,
        0x650A7354,
        0x766A0ABB,
        0x81C2C92E,
        0x92722C85,
        0xA2BFE8A1,
        0xA81A664B,
        0xC24B8B70,
        0xC76C51A3,
        0xD192E819,
        0xD6990624,
        0xF40E3585,
        0x106AA070,
        0x19A4C116,
        0x1E376C08,
        0x2748774C,
        0x34B0BCB5,
        0x391C0CB3,
        0x4ED8AA4A,
        0x5B9CCA4F,
        0x682E6FF3,
        0x748F82EE,
        0x78A5636F,
        0x84C87814,
        0x8CC70208,
        0x90BEFFFA,
        0xA4506CEB,
        0xBEF9A3F7,
        0xC67178F2,
    )
    mask = 0xFFFFFFFF
    for offset in range(0, len(payload), 64):
        block = payload[offset : offset + 64]
        w = [0] * 64
        for i in range(16):
            j = i * 4
            w[i] = (
                (block[j] << 24)
                | (block[j + 1] << 16)
                | (block[j + 2] << 8)
                | block[j + 3]
            )
        for i in range(16, 64):
            s0 = _rotr(w[i - 15], 7) ^ _rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
            s1 = _rotr(w[i - 2], 17) ^ _rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
            w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & mask
        a, b, c, d, e, f, g, hh = h
        for i in range(64):
            s1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
            ch = (e & f) ^ ((~e) & g)
            temp1 = (hh + s1 + ch + k[i] + w[i]) & mask
            s0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            temp2 = (s0 + maj) & mask
            hh = g
            g = f
            f = e
            e = (d + temp1) & mask
            d = c
            c = b
            b = a
            a = (temp1 + temp2) & mask
        h = [
            (h[0] + a) & mask,
            (h[1] + b) & mask,
            (h[2] + c) & mask,
            (h[3] + d) & mask,
            (h[4] + e) & mask,
            (h[5] + f) & mask,
            (h[6] + g) & mask,
            (h[7] + hh) & mask,
        ]
    return bytes(
        byte
        for word in h
        for byte in (
            (word >> 24) & 0xFF,
            (word >> 16) & 0xFF,
            (word >> 8) & 0xFF,
            word & 0xFF,
        )
    )


def _rotr(value, bits):
    return ((value >> bits) | (value << (32 - bits))) & 0xFFFFFFFF


def _ota_state_path(root):
    root_text = str(root or ".")
    if root_text == "/":
        return "/_ota/state.json"
    if root_text.endswith("/"):
        return "{}_ota/state.json".format(root_text)
    return "{}/_ota/state.json".format(root_text)


def _ota_manifest_path(root):
    return _join_root(root, "_ota/manifest.json")


def _ota_stage_path(root):
    return _join_root(root, "_ota/stage")


def _ota_backup_path(root):
    return _join_root(root, "_ota/backup")


def _join_root(root, path):
    root_text = str(root or ".")
    path_text = str(path or "")
    if root_text == "/":
        return "/{}".format(path_text.lstrip("/"))
    if root_text.endswith("/"):
        return "{}{}".format(root_text, path_text.lstrip("/"))
    return "{}/{}".format(root_text, path_text.lstrip("/"))
