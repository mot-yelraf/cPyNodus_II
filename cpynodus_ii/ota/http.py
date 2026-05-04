"""Serve the temporary OTA HTTP control endpoints."""

import hashlib
import json
import os
import time

from cpynodus_ii.ota.state import FwUpdateState, load_ota_state, save_ota_state

_HTTP_STATUS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    501: "Not Implemented",
    503: "Service Unavailable",
}


class OtaHttpController:
    """Bridge OTA state handlers onto an `adafruit_httpserver` server."""

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
        """Poll the OTA server once if active."""
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
            return self
        self._maybe_reboot()
        return self

    @property
    def route_paths(self):
        return self._route_paths

    def status_payload(self):
        """Return a compact OTA status payload."""
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
            path = _request_path_arg(request)
            payload, status_code = self._handle_file(path, _request_body_bytes(request))
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
        actual_sha = hashlib.sha256(body).hexdigest()
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

    def _handle_commit(self):
        state = load_ota_state(_ota_state_path(self.settings_root)) or self.ota_state
        if getattr(state, "phase", "") != "staging":
            self._log("commit rejected error=ota_not_staging")
            return _error_payload("ota_not_staging", getattr(state, "phase", "")), 400
        manifest = _read_json_file(_ota_manifest_path(self.settings_root))
        if manifest is None:
            self._log("commit rejected error=manifest_missing")
            return _error_payload("manifest_missing", "staging"), 400
        error = _verify_staged_manifest_files(self.settings_root, manifest)
        if error:
            self._log("commit rejected error={}".format(error))
            return _error_payload(error, "staging"), 400
        error = _apply_staged_manifest_files(self.settings_root, manifest)
        if error:
            self._log("commit rejected error={}".format(error))
            return _error_payload(error, "staging"), 503
        applied_state = FwUpdateState(
            prior_profile=getattr(state, "prior_profile", "") or "",
            package_id=getattr(state, "package_id", "") or "",
            phase="applied_pending_boot",
        )
        save_ota_state(applied_state, _ota_state_path(self.settings_root))
        self.ota_state = applied_state
        self._schedule_reboot()
        self._log(
            "commit accepted package={} files={} reboot_delay_s={}".format(
                applied_state.package_id,
                len(manifest.get("files", ()) or ()),
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
    """Build the JSON payload returned by `/ota/status`."""
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
    if isinstance(query_params, dict):
        return str(query_params.get("path", "") or "")
    for attr_name in ("query", "query_string"):
        query = str(getattr(request, attr_name, "") or "")
        value = _query_arg(query, "path")
        if value:
            return value
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
        payload = _read_binary_file(staged_path)
        if payload is None:
            return "staged_file_missing"
        expected_size = int(entry.get("size", -1) or -1)
        if len(payload) != expected_size:
            return "staged_file_size_mismatch"
        expected_sha = str(entry.get("sha256", "") or "")
        if hashlib.sha256(payload).hexdigest() != expected_sha:
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


def _ota_state_path(root):
    root_text = str(root or ".")
    if root_text == "/":
        return "/_ota/state.json"
    if root_text.endswith("/"):
        return "{}_ota/state.json".format(root_text)
    return "{}/_ota/state.json".format(root_text)


def _ota_manifest_path(root):
    return _join_root(root, "_ota/manifest.json")


def _join_root(root, path):
    root_text = str(root or ".")
    path_text = str(path or "")
    if root_text == "/":
        return "/{}".format(path_text.lstrip("/"))
    if root_text.endswith("/"):
        return "{}{}".format(root_text, path_text.lstrip("/"))
    return "{}/{}".format(root_text, path_text.lstrip("/"))
