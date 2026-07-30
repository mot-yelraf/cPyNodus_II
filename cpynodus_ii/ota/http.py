"""Serve the temporary Nodus OTA HTTP transfer endpoints.

This module runs only in OTA mode, after MQTT has been stopped and the device
has rebooted into a reduced runtime. It accepts a manifest, stages files under
`_ota/stage`, supports 1024-byte chunk uploads to avoid large heap
allocations, verifies SHA256 while streaming from disk, backs up live files,
applies staged files, and schedules the final reboot.
"""

import gc
import hashlib
import json
import os
import time

from cpynodus_ii.ota.state import (
    FwUpdateState,
    clear_ota_state,
    load_ota_state,
    save_ota_state,
)

_HTTP_STATUS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}
_SHA256_UNAVAILABLE_SIZE = -2
OTA_WAIT_FOR_BEGIN_TIMEOUT_S = 300
OTA_STAGING_IDLE_TIMEOUT_S = 900
_SHA256_K = (
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
        self._last_activity_at = self._monotonic()
        self.manifest_validator = _validate_manifest_for_begin
        self.platform = "pico2w"
        self.circuitpython = "9.2.8"
        try:
            from cpynodus_ii.core.board_profile import selected_board_profile

            profile = selected_board_profile()
            self.platform = str(getattr(profile, "key", "") or self.platform)
            self.circuitpython = str(
                getattr(profile, "circuitpython_version", "") or self.circuitpython
            )
        except Exception:
            pass

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
        self._maybe_reboot()
        if self.phase != "ready" or self.server is None:
            return self
        poll = getattr(self.server, "poll", None)
        if not callable(poll):
            self.phase = "error"
            self.errors = ("ota_server_poll_unavailable",)
            if not self._reboot_requested:
                state = (
                    load_ota_state(_ota_state_path(self.settings_root))
                    or self.ota_state
                )
                self._abort_transfer_state(state, "ota_server_poll_unavailable")
                self._schedule_reboot()
            return self
        try:
            poll()
        except Exception as exc:
            self.phase = "error"
            self.errors = ("ota_poll_failed", str(exc))
            self._log("poll failed error={}".format(_exception_text(exc)))
            if not self._reboot_requested:
                state = (
                    load_ota_state(_ota_state_path(self.settings_root))
                    or self.ota_state
                )
                self._abort_transfer_state(state, "ota_poll_failed")
                self._schedule_reboot()
            return self
        self._maybe_abort_inactive()
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
            _collect_garbage()
            raw_manifest = _request_body_bytes(request)
            error = self._authorize_begin(request, raw_manifest)
            if error:
                self._log("begin rejected error={} action=abort".format(error))
                payload = self._reject_unauthorized_begin(error)
                self._schedule_reboot()
                return self._json_response(request, payload, status_code=401)
            manifest = _parse_json_bytes(raw_manifest)
            if manifest is None:
                self._log("begin rejected error=invalid_json action=abort")
                payload, status_code = self._abort_transfer("invalid_json")
                return self._json_response(
                    request,
                    payload,
                    status_code=status_code,
                )
            payload, status_code = self._handle_begin(manifest)
            _collect_garbage()
            return self._json_response(request, payload, status_code=status_code)

        @route("/ota/file", methods=["PUT"])
        def _file(request):
            try:
                error = self._session_error(request)
                if error:
                    return self._json_response(
                        request, _error_payload(error, "staging"), status_code=401
                    )
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
            error = self._session_error(request)
            if error:
                return self._json_response(
                    request, _error_payload(error, "staging"), status_code=401
                )
            path = _request_path_arg(request)
            payload, status_code = self._handle_file_begin(path)
            return self._json_response(request, payload, status_code=status_code)

        @route("/ota/file/chunk", methods=["PUT"])
        def _file_chunk(request):
            try:
                error = self._session_error(request)
                if error:
                    return self._json_response(
                        request, _error_payload(error, "staging"), status_code=401
                    )
                path = _request_path_arg(request)
                offset = _request_int_arg(request, "offset", -1)
                body = _request_body_bytes(request)
                payload, status_code = self._handle_file_chunk(path, offset, body)
                try:
                    request.body = b""
                except Exception:
                    pass
                del body
                _collect_garbage()
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
            try:
                error = self._session_error(request)
                if error:
                    return self._json_response(
                        request, _error_payload(error, "staging"), status_code=401
                    )
                _collect_garbage()
                path = _request_path_arg(request)
                payload, status_code = self._handle_file_end(path)
                _collect_garbage()
                return self._json_response(request, payload, status_code=status_code)
            except Exception as exc:
                self._log("file end exception error={}".format(_exception_text(exc)))
                return self._json_response(
                    request,
                    _error_payload("file_end_handler_exception", "staging"),
                    status_code=500,
                )

        @route("/ota/commit", methods=["POST"])
        def _commit(request):
            error = self._session_error(request)
            if error:
                return self._json_response(
                    request, _error_payload(error, "staging"), status_code=401
                )
            _collect_garbage()
            payload, status_code = self._handle_commit()
            _collect_garbage()
            return self._json_response(request, payload, status_code=status_code)

        @route("/ota/abort", methods=["POST"])
        def _abort(request):
            error = self._session_error(request)
            if error:
                return self._json_response(
                    request, _error_payload(error, "aborted"), status_code=401
                )
            state = (
                load_ota_state(_ota_state_path(self.settings_root))
                or self.ota_state
            )
            phase = str(getattr(state, "phase", "") or "")
            if phase in {"applied_pending_boot", "applied"}:
                return self._json_response(
                    request,
                    _error_payload("ota_apply_already_pending", phase),
                    status_code=409,
                )
            aborted = self._abort_transfer_state(state, "client_abort")
            self._schedule_reboot()
            return self._json_response(
                request,
                {
                    "accepted": True,
                    "phase": "aborted",
                    "package_id": aborted.package_id,
                    "rebooting": bool(self.reboot_callback is not None),
                    "reboot_delay_s": self.reboot_delay_s,
                },
            )

    def _authorize_begin(self, request, raw_manifest):
        error = self._session_error(request)
        if error:
            return error
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        from cpynodus_ii.ota.auth import (
            load_public_key,
            sha256_hex,
            verify_manifest_signature,
        )

        self._log_memory("auth_before")
        actual_sha = sha256_hex(raw_manifest)
        expected_sha = str(getattr(state, "manifest_sha256", "") or "").lower()
        if actual_sha != expected_sha:
            return "manifest_sha256_mismatch"
        key_id = _request_header(request, "X-Nodus-OTA-Key-Id")
        signature = _request_header(request, "X-Nodus-OTA-Signature")
        if key_id != str(getattr(state, "key_id", "") or ""):
            return "ota_signing_key_mismatch"
        key_path = _join_root(self.settings_root, "ota-public-key.json")
        error = verify_manifest_signature(
            raw_manifest,
            signature,
            load_public_key(key_path),
            key_id,
        )
        self._log_memory("auth_after")
        return error

    def _session_error(self, request):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        expected = str(getattr(state, "session_id", "") or "")
        supplied = _request_header(request, "X-Nodus-OTA-Session")
        if not expected or not supplied or not _constant_time_text_equal(
            expected, supplied
        ):
            return "ota_session_mismatch"
        return ""

    def _log_memory(self, phase):
        _collect_garbage()
        try:
            free_mem = gc.mem_free()
        except Exception:
            free_mem = "unknown"
        self._log("phase={} free_mem={}".format(phase, free_mem))

    def _handle_begin(self, manifest):
        _collect_garbage()
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        error = self.manifest_validator(
            manifest,
            state,
            version=self.version,
            platform=self.platform,
            circuitpython=self.circuitpython,
        )
        if error:
            self._log("begin rejected error={} action=abort".format(error))
            return self._abort_transfer(error)
        error = _manifest_storage_error(self.settings_root, manifest)
        if error:
            self._log("begin rejected error={} action=abort".format(error))
            return self._abort_transfer(error)
        next_state = FwUpdateState(
            prior_profile=getattr(state, "prior_profile", "") or "",
            package_id=str(manifest.get("package_id", "") or ""),
            session_id=getattr(state, "session_id", "") or "",
            manifest_sha256=getattr(state, "manifest_sha256", "") or "",
            key_id=getattr(state, "key_id", "") or "",
            phase="staging",
        )
        _cleanup_package_workspace(self.settings_root)
        save_ota_state(next_state, _ota_state_path(self.settings_root))
        _write_json_file(_ota_manifest_path(self.settings_root), manifest)
        self.ota_state = next_state
        self._touch_activity()
        self._log(
            "begin accepted package={} files={}".format(
                next_state.package_id,
                len(manifest.get("files", ()) or ()),
            )
        )
        _collect_garbage()
        return (
            {
                "accepted": True,
                "phase": "staging",
                "package_id": next_state.package_id,
                "files": len(manifest.get("files", ()) or ()),
            },
            200,
        )

    def _abort_transfer(self, error):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        self._abort_transfer_state(state, error)
        return _error_payload(error, "aborted"), 400

    def _reject_unauthorized_begin(self, error):
        """Clear the one-use request without touching prior rollback artifacts."""
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        self.ota_state = FwUpdateState(
            prior_profile=getattr(state, "prior_profile", "") or "",
            package_id=getattr(state, "package_id", "") or "",
            phase="aborted",
            error=str(error or "ota_authentication_failed"),
        )
        clear_ota_state(_ota_state_path(self.settings_root))
        self._log(
            "authentication rejected package={} error={} state=cleared".format(
                self.ota_state.package_id or "none",
                self.ota_state.error,
            )
        )
        return _error_payload(error, "aborted")

    def _abort_transfer_state(self, state, error):
        aborted = FwUpdateState(
            prior_profile=getattr(state, "prior_profile", "") or "",
            package_id=getattr(state, "package_id", "") or "",
            session_id=getattr(state, "session_id", "") or "",
            manifest_sha256=getattr(state, "manifest_sha256", "") or "",
            key_id=getattr(state, "key_id", "") or "",
            phase="aborted",
            error=str(error or "ota_aborted"),
        )
        _remove_tree(_ota_stage_path(self.settings_root))
        _remove_tree(_ota_backup_path(self.settings_root))
        _remove_ota_tmp_files(self.settings_root)
        _remove_file(_ota_manifest_path(self.settings_root))
        _remove_file(_ota_transaction_path(self.settings_root))
        clear_ota_state(_ota_state_path(self.settings_root))
        self.ota_state = aborted
        self._log(
            "transfer aborted package={} error={} state=cleared".format(
                aborted.package_id or "none",
                aborted.error or "ota_aborted",
            )
        )
        return aborted

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
        self._touch_activity()
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
        self._touch_activity()
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
            payload = _error_payload("chunk_offset_mismatch", "staging")
            payload["offset"] = current_size
            return payload, 409
        expected_size = int(entry.get("size", -1) or -1)
        next_offset = current_size + len(body)
        if next_offset > expected_size:
            return _error_payload("chunk_size_exceeds_file", "staging"), 400
        try:
            with open(stage_path, "ab") as handle:
                handle.write(body)
        except OSError:
            return _error_payload("chunk_stage_failed", "staging"), 503
        self._touch_activity()
        _collect_garbage()
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
        _collect_garbage()
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
        self._log("Verifying file... path={}".format(safe_path))
        verify_started = self._monotonic()
        size, actual_sha = _file_size_sha256(stage_path)
        verify_elapsed = self._elapsed_s(verify_started)
        expected_size = int(entry.get("size", -1) or -1)
        expected_sha = str(entry.get("sha256", "") or "")
        if size == _SHA256_UNAVAILABLE_SIZE:
            self._log(
                "Verification failed... path={} error=sha256_unavailable".format(
                    safe_path
                )
            )
            return _error_payload("sha256_unavailable", "staging"), 503
        if size < 0:
            self._log(
                "Verification failed... path={} error=staged_file_missing".format(
                    safe_path
                )
            )
            return _error_payload("staged_file_missing", "staging"), 400
        if size != expected_size:
            self._log(
                "Verification failed... path={} error=file_size_mismatch".format(
                    safe_path
                )
            )
            return _error_payload("file_size_mismatch", "staging"), 400
        if actual_sha != expected_sha:
            self._log(
                "Verification failed... path={} error=sha256_mismatch".format(
                    safe_path
                )
            )
            return _error_payload("sha256_mismatch", "staging"), 400
        self._touch_activity()
        self._log("Verified... path={}".format(safe_path))
        self._log(
            "file accepted path={} bytes={} verify_s={:.1f}".format(
                safe_path,
                size,
                verify_elapsed,
            )
        )
        _collect_garbage()
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
        _collect_garbage()
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        if getattr(state, "phase", "") != "staging":
            self._log("commit rejected error=ota_not_staging")
            return _error_payload("ota_not_staging", getattr(state, "phase", "")), 400
        manifest = _read_json_file(_ota_manifest_path(self.settings_root))
        if manifest is None:
            self._log("commit rejected error=manifest_missing")
            return _error_payload("manifest_missing", "staging"), 400
        verify_started = self._monotonic()
        _collect_garbage()
        error = _verify_staged_manifest_files(self.settings_root, manifest)
        verify_elapsed = self._elapsed_s(verify_started)
        if error:
            self._log("commit rejected error={}".format(error))
            return _error_payload(error, "staging"), 400
        self._log("commit verify complete elapsed_s={:.1f}".format(verify_elapsed))
        apply_started = self._monotonic()
        _collect_garbage()
        transaction = _build_apply_transaction(self.settings_root, manifest)
        try:
            _write_json_file(_ota_transaction_path(self.settings_root), transaction)
            applying_state = FwUpdateState(
                prior_profile=getattr(state, "prior_profile", "") or "",
                package_id=getattr(state, "package_id", "") or "",
                session_id=getattr(state, "session_id", "") or "",
                manifest_sha256=getattr(state, "manifest_sha256", "") or "",
                key_id=getattr(state, "key_id", "") or "",
                phase="applying",
            )
            save_ota_state(applying_state, _ota_state_path(self.settings_root))
            self.ota_state = applying_state
            error = _apply_staged_manifest_files(
                self.settings_root,
                manifest,
                transaction,
            )
        except OSError:
            error = "apply_transaction_persist_failed"
        apply_elapsed = self._elapsed_s(apply_started)
        if error:
            self._log("commit rejected error={}".format(error))
            rollback_error = _rollback_apply_transaction(
                self.settings_root,
                manifest,
                transaction,
            )
            if rollback_error:
                self._log("rollback failed error={}".format(rollback_error))
                self._schedule_reboot()
                return _error_payload(rollback_error, "applying"), 503
            staging_state = FwUpdateState(
                prior_profile=getattr(state, "prior_profile", "") or "",
                package_id=getattr(state, "package_id", "") or "",
                session_id=getattr(state, "session_id", "") or "",
                manifest_sha256=getattr(state, "manifest_sha256", "") or "",
                key_id=getattr(state, "key_id", "") or "",
                phase="staging",
            )
            save_ota_state(staging_state, _ota_state_path(self.settings_root))
            self.ota_state = staging_state
            return _error_payload(error, "staging"), 503
        self._log("commit apply complete elapsed_s={:.1f}".format(apply_elapsed))
        _collect_garbage()
        _remove_tree(_ota_stage_path(self.settings_root))
        _remove_ota_tmp_files(self.settings_root)
        applied_state = FwUpdateState(
            prior_profile=getattr(state, "prior_profile", "") or "",
            package_id=getattr(state, "package_id", "") or "",
            session_id=getattr(state, "session_id", "") or "",
            manifest_sha256=getattr(state, "manifest_sha256", "") or "",
            key_id=getattr(state, "key_id", "") or "",
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

    def _touch_activity(self):
        self._last_activity_at = self._monotonic()

    def _maybe_abort_inactive(self):
        if self._reboot_requested:
            return
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        phase = str(getattr(state, "phase", "") or "")
        timeout_s = (
            OTA_WAIT_FOR_BEGIN_TIMEOUT_S
            if phase in {"requested", "ready"}
            else OTA_STAGING_IDLE_TIMEOUT_S
            if phase == "staging"
            else 0
        )
        if timeout_s <= 0:
            return
        if self._elapsed_s(self._last_activity_at) < float(timeout_s):
            return
        error = (
            "ota_begin_timeout"
            if phase in {"requested", "ready"}
            else "ota_staging_timeout"
        )
        self._log(
            "inactivity timeout phase={} error={} action=abort".format(phase, error)
        )
        self._abort_transfer_state(state, error)
        self._schedule_reboot()

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
        "package_id": "",
        "ota_protocol": "v2",
        "authentication": "rsa-pkcs1v15-sha256",
        "key_id": state.key_id,
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


def _validate_manifest_for_begin(
    manifest,
    ota_state,
    *,
    version="",
    platform="",
    circuitpython="",
):
    if manifest.get("schema") != "nodus-ota/v2":
        return "manifest_schema_invalid"
    package_id = str(manifest.get("package_id", "") or "").strip()
    if not package_id:
        return "package_id_missing"
    expected_package_id = str(getattr(ota_state, "package_id", "") or "").strip()
    if expected_package_id and package_id != expected_package_id:
        return "package_id_mismatch"
    target = manifest.get("target")
    if not isinstance(target, dict):
        return "manifest_target_invalid"
    if str(target.get("platform", "") or "") != str(platform or ""):
        return "manifest_platform_mismatch"
    if str(target.get("circuitpython", "") or "") != str(circuitpython or ""):
        return "manifest_circuitpython_mismatch"
    requires = manifest.get("requires")
    if not isinstance(requires, dict):
        return "manifest_requires_invalid"
    if str(requires.get("version", "") or "") != str(version or ""):
        return "manifest_version_mismatch"
    files = manifest.get("files", ())
    if not isinstance(files, list):
        return "manifest_files_invalid"
    preserve_raw = manifest.get("preserve", ())
    if "preserve" in manifest and not isinstance(preserve_raw, list):
        return "manifest_preserve_invalid"
    preserve = set()
    for raw_path in preserve_raw:
        safe_path = _normalize_package_path(raw_path)
        if not safe_path:
            return "manifest_preserve_path_invalid"
        preserve.add(safe_path)
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            return "manifest_file_invalid"
        if not str(entry.get("path", "") or "").strip():
            return "manifest_file_path_missing"
        safe_path = _normalize_package_path(entry.get("path", ""))
        if not safe_path:
            return "manifest_file_path_invalid"
        if not _ota_payload_path_allowed(safe_path):
            return "manifest_file_path_forbidden"
        if safe_path in seen:
            return "manifest_file_duplicate"
        if safe_path in preserve:
            return "manifest_preserve_conflict"
        seen.add(safe_path)
        try:
            size = int(entry.get("size", -1) or -1)
        except (TypeError, ValueError):
            return "manifest_file_size_invalid"
        if size < 0:
            return "manifest_file_size_invalid"
        if not _is_sha256_text(entry.get("sha256", "")):
            return "manifest_file_sha256_invalid"
    delete_paths = manifest.get("delete", ())
    if "delete" in manifest and not isinstance(delete_paths, list):
        return "manifest_delete_invalid"
    delete_seen = set()
    for raw_path in delete_paths:
        safe_path = _normalize_package_path(raw_path)
        if not safe_path:
            return "manifest_delete_path_invalid"
        if not _ota_delete_path_allowed(safe_path):
            return "manifest_delete_path_forbidden"
        if safe_path in delete_seen:
            return "manifest_delete_duplicate"
        if safe_path in seen:
            return "manifest_path_conflict"
        if safe_path in preserve:
            return "manifest_preserve_conflict"
        delete_seen.add(safe_path)
        seen.add(safe_path)
    for entry in files:
        safe_path = _normalize_package_path(entry.get("path", ""))
        if safe_path.startswith("cpynodus_ii/") and safe_path.endswith(".mpy"):
            source_path = "{}.py".format(safe_path[:-4])
            if source_path not in delete_seen:
                return "manifest_source_delete_missing"
    return ""


def _parse_json_bytes(raw):
    try:
        text = bytes(raw).decode("utf-8")
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _ota_payload_path_allowed(path):
    if path in {"boot.py", "code.py", "dataclass.py", "dataclasses.py"}:
        return True
    if path in {
        "settings.toml.def",
        "sensor_i2c.toml.def",
        "sensor_soil.toml.def",
        "switch.toml.def",
    }:
        return True
    if path.startswith("cpynodus_ii/"):
        return path.endswith(".mpy")
    if path.startswith("boards/"):
        return path.endswith(".toml.def")
    return False


def _ota_delete_path_allowed(path):
    if path in {
        "settings.toml.def",
        "sensor_i2c.toml.def",
        "sensor_soil.toml.def",
        "switch.toml.def",
    }:
        return True
    if path.startswith("cpynodus_ii/"):
        return path.endswith(".py") or path.endswith(".mpy")
    if path.startswith("boards/"):
        return path.endswith(".toml.def")
    return False


def _is_sha256_text(value):
    text = str(value or "").lower()
    if len(text) != 64:
        return False
    for character in text:
        if character not in "0123456789abcdef":
            return False
    return True


def _constant_time_text_equal(left, right):
    left_bytes = str(left or "").encode("utf-8")
    right_bytes = str(right or "").encode("utf-8")
    mismatch = len(left_bytes) ^ len(right_bytes)
    limit = min(len(left_bytes), len(right_bytes))
    for index in range(limit):
        mismatch |= left_bytes[index] ^ right_bytes[index]
    return mismatch == 0


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
        if size == _SHA256_UNAVAILABLE_SIZE:
            return "sha256_unavailable"
        if size < 0:
            return "staged_file_missing"
        expected_size = int(entry.get("size", -1) or -1)
        if size != expected_size:
            return "staged_file_size_mismatch"
        expected_sha = str(entry.get("sha256", "") or "")
        if actual_sha != expected_sha:
            return "staged_file_sha256_mismatch"
    return ""


def _manifest_storage_error(root, manifest):
    required = 4096
    paths = []
    for entry in manifest.get("files", ()) or ():
        # Staging and the same-directory apply temp coexist during commit.
        required += 2 * max(0, int(entry.get("size", 0) or 0))
        safe_path = _normalize_package_path(entry.get("path", ""))
        if safe_path and safe_path not in paths:
            paths.append(safe_path)
    for raw_path in manifest.get("delete", ()) or ():
        safe_path = _normalize_package_path(raw_path)
        if safe_path and safe_path not in paths:
            paths.append(safe_path)
    for safe_path in paths:
        live_size = _file_size(_join_root(root, safe_path))
        if live_size > 0:
            required += live_size
    available = _filesystem_free_bytes(root)
    if available >= 0 and available < required:
        return "insufficient_storage"
    return ""


def _filesystem_free_bytes(root):
    statvfs = getattr(os, "statvfs", None)
    if not callable(statvfs):
        return -1
    try:
        values = statvfs(root or ".")
        block_size = int(values[1] or values[0])
        return block_size * int(values[4])
    except (OSError, IndexError, TypeError, ValueError):
        return -1


def _build_apply_transaction(root, manifest):
    paths = []
    existing = []
    for entry in manifest.get("files", ()) or ():
        safe_path = _normalize_package_path(entry.get("path", ""))
        if safe_path and safe_path not in paths:
            paths.append(safe_path)
    for raw_path in manifest.get("delete", ()) or ():
        safe_path = _normalize_package_path(raw_path)
        if safe_path and safe_path not in paths:
            paths.append(safe_path)
    for safe_path in paths:
        if _path_exists(_join_root(root, safe_path)):
            existing.append(safe_path)
    return {"paths": paths, "existing": existing}


def _apply_staged_manifest_files(root, manifest, transaction=None):
    transaction = transaction or _build_apply_transaction(root, manifest)
    existing = set(transaction.get("existing", ()) or ())
    for entry in manifest.get("files", ()) or ():
        safe_path = _normalize_package_path(entry.get("path", ""))
        if not safe_path:
            return "manifest_file_path_invalid"
        staged_path = _join_root(root, "_ota/stage/{}".format(safe_path))
        live_path = _join_root(root, safe_path)
        backup_path = _join_root(root, "_ota/backup/{}".format(safe_path))
        new_path = "{}.ota-new".format(live_path)
        try:
            _copy_file(staged_path, new_path)
            if safe_path in existing:
                _copy_file(live_path, backup_path)
            _remove_file(live_path)
            os.rename(new_path, live_path)
        except OSError:
            _remove_file(new_path)
            return "file_apply_failed"
    for raw_path in manifest.get("delete", ()) or ():
        safe_path = _normalize_package_path(raw_path)
        if not safe_path:
            return "manifest_delete_path_invalid"
        live_path = _join_root(root, safe_path)
        backup_path = _join_root(root, "_ota/backup/{}".format(safe_path))
        try:
            if safe_path in existing and _path_exists(live_path):
                _copy_file(live_path, backup_path)
                _remove_file(live_path)
        except OSError:
            return "file_delete_failed"
    return ""


def _rollback_apply_transaction(root, manifest, transaction):
    existing = set((transaction or {}).get("existing", ()) or ())
    paths = list((transaction or {}).get("paths", ()) or ())
    if not paths:
        paths = list(_build_apply_transaction(root, manifest).get("paths", ()) or ())
    failed = False
    for safe_path in reversed(paths):
        live_path = _join_root(root, safe_path)
        backup_path = _join_root(root, "_ota/backup/{}".format(safe_path))
        _remove_file("{}.ota-new".format(live_path))
        try:
            if safe_path in existing:
                if _path_exists(backup_path):
                    restore_path = "{}.ota-restore".format(live_path)
                    _copy_file(backup_path, restore_path)
                    _remove_file(live_path)
                    os.rename(restore_path, live_path)
            else:
                _remove_file(live_path)
        except OSError:
            failed = True
    if not failed:
        _remove_file(_ota_transaction_path(root))
        _remove_tree(_ota_backup_path(root))
    return "apply_rollback_failed" if failed else ""


def recover_interrupted_ota_apply(root):
    """Restore the pre-update filesystem after a reset during apply."""
    manifest = _read_json_file(_ota_manifest_path(root))
    transaction = _read_json_file(_ota_transaction_path(root))
    if manifest is None or transaction is None:
        return "apply_recovery_metadata_missing"
    error = _rollback_apply_transaction(root, manifest, transaction)
    if error:
        return error
    _remove_tree(_ota_stage_path(root))
    _remove_ota_tmp_files(root)
    _remove_file(_ota_manifest_path(root))
    clear_ota_state(_ota_state_path(root))
    return ""


def cleanup_abandoned_ota_session(root):
    """Remove staging state left by an interrupted or rejected transfer."""
    _remove_tree(_ota_stage_path(root))
    _remove_tree(_ota_backup_path(root))
    _remove_ota_tmp_files(root)
    _remove_file(_ota_manifest_path(root))
    _remove_file(_ota_transaction_path(root))
    clear_ota_state(_ota_state_path(root))


def _cleanup_package_workspace(root):
    _remove_tree(_ota_stage_path(root))
    _remove_tree(_ota_backup_path(root))
    _remove_ota_tmp_files(root)
    _remove_file(_ota_transaction_path(root))


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


def _remove_file(path):
    try:
        os.remove(path)
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
        return _SHA256_UNAVAILABLE_SIZE, ""
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
    digest = _hasher_hexdigest(hasher)
    if not digest:
        return _SHA256_UNAVAILABLE_SIZE, ""
    return size, digest


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


def _collect_garbage():
    try:
        gc.collect()
    except Exception:
        pass


def _sha256_hex(data):
    digest = _hashlib_sha256(data)
    if digest is not None:
        return digest
    return ""


def _hashlib_sha256(data):
    hasher = _new_sha256_hasher()
    if hasher is not None:
        hasher.update(data)
        return _hasher_hexdigest(hasher)
    return None


def _hasher_hexdigest(hasher):
    hexdigest = getattr(hasher, "hexdigest", None)
    if callable(hexdigest):
        try:
            return str(hexdigest())
        except Exception:
            pass
    digest = getattr(hasher, "digest", None)
    if callable(digest):
        try:
            return _bytes_to_hex(digest())
        except Exception:
            pass
    return ""


def _bytes_to_hex(data):
    hex_method = getattr(data, "hex", None)
    if callable(hex_method):
        try:
            return str(hex_method())
        except Exception:
            pass
    chars = "0123456789abcdef"
    return "".join(
        "{}{}".format(chars[(int(byte) >> 4) & 0x0F], chars[int(byte) & 0x0F])
        for byte in bytes(data or b"")
    )


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
    try:
        return _StreamingSha256()
    except Exception:
        return None


class _StreamingSha256:
    def __init__(self):
        self._h = [
            0x6A09E667,
            0xBB67AE85,
            0x3C6EF372,
            0xA54FF53A,
            0x510E527F,
            0x9B05688C,
            0x1F83D9AB,
            0x5BE0CD19,
        ]
        self._buffer = b""
        self._count = 0

    def update(self, data):
        payload = bytes(data or b"")
        self._count += len(payload)
        if self._buffer:
            payload = self._buffer + payload
            self._buffer = b""
        offset = 0
        payload_len = len(payload)
        while payload_len - offset >= 64:
            _sha256_process_block(self._h, payload[offset : offset + 64])
            offset += 64
        if offset < payload_len:
            self._buffer = payload[offset:]

    def digest(self):
        h = list(self._h)
        length_bits = (int(self._count) * 8) & 0xFFFFFFFFFFFFFFFF
        payload = self._buffer + b"\x80"
        pad_len = (56 - (len(payload) % 64)) % 64
        payload += b"\x00" * pad_len
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
        offset = 0
        while offset < len(payload):
            _sha256_process_block(h, payload[offset : offset + 64])
            offset += 64
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

    def hexdigest(self):
        return _bytes_to_hex(self.digest())


def _sha256_process_block(h, block):
    mask = 0xFFFFFFFF
    w = [0] * 64
    for i in range(16):
        j = i * 4
        w[i] = (
            (int(block[j]) << 24)
            | (int(block[j + 1]) << 16)
            | (int(block[j + 2]) << 8)
            | int(block[j + 3])
        )
    for i in range(16, 64):
        s0 = _sha256_rotr(w[i - 15], 7) ^ _sha256_rotr(w[i - 15], 18) ^ (
            w[i - 15] >> 3
        )
        s1 = _sha256_rotr(w[i - 2], 17) ^ _sha256_rotr(w[i - 2], 19) ^ (
            w[i - 2] >> 10
        )
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & mask
    a, b, c, d, e, f, g, hh = h
    for i in range(64):
        s1 = _sha256_rotr(e, 6) ^ _sha256_rotr(e, 11) ^ _sha256_rotr(e, 25)
        ch = (e & f) ^ ((~e) & g)
        temp1 = (hh + s1 + ch + _SHA256_K[i] + w[i]) & mask
        s0 = _sha256_rotr(a, 2) ^ _sha256_rotr(a, 13) ^ _sha256_rotr(a, 22)
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
    h[0] = (h[0] + a) & mask
    h[1] = (h[1] + b) & mask
    h[2] = (h[2] + c) & mask
    h[3] = (h[3] + d) & mask
    h[4] = (h[4] + e) & mask
    h[5] = (h[5] + f) & mask
    h[6] = (h[6] + g) & mask
    h[7] = (h[7] + hh) & mask


def _sha256_rotr(value, bits):
    return ((int(value) >> bits) | (int(value) << (32 - bits))) & 0xFFFFFFFF


def _ota_state_path(root):
    root_text = str(root or ".")
    if root_text == "/":
        return "/_ota/state.json"
    if root_text.endswith("/"):
        return "{}_ota/state.json".format(root_text)
    return "{}/_ota/state.json".format(root_text)


def _ota_manifest_path(root):
    return _join_root(root, "_ota/manifest.json")


def _ota_transaction_path(root):
    return _join_root(root, "_ota/transaction.json")


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
