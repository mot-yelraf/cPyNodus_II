"""Build and transfer host-side OTA packages from Git revisions.

The public build helpers enforce target-specific MPY packaging and manifest
rules; signing, prepare, transfer, and ``main`` support the complete operator
workflow. This module requires host Python and external Git/MPY tooling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from .ota_signing import (
        OTASigningError,
        generate_signing_key,
        load_signature,
        sign_manifest,
    )
except ImportError:
    from ota_signing import (  # type: ignore
        OTASigningError,
        generate_signing_key,
        load_signature,
        sign_manifest,
    )

SCHEMA = "nodus-ota/v2"
DEFAULT_PLATFORM = "pico2w"
TARGET_CIRCUITPYTHON = {
    "pico2w": "9.2.8",
    "xesp32s3": "10.2.1",
}
MPY_SOURCE_EXCEPTIONS = {
    "boot.py",
    "code.py",
    "safemode.py",
    "dataclass.py",
    "dataclasses.py",
}
PRESERVED_CONFIG_FILES = (
    "settings.toml",
    "sensor_i2c.toml",
    "sensor_soil.toml",
    "switch.toml",
    "ota-public-key.json",
)
EXCLUDED_PREFIXES = (
    ".git/",
    ".pytest_cache/",
    "__pycache__/",
    "build/",
    "docs/",
    "specs/",
    "tests/",
)
EXCLUDED_NAMES = {
    ".DS_Store",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
}
ROOT_DEPLOYABLE = {
    "boot.py",
    "code.py",
    "safemode.py",
    "dataclass.py",
    "dataclasses.py",
    "settings.toml.def",
    "sensor_i2c.toml.def",
    "sensor_soil.toml.def",
    "switch.toml.def",
}
DEPLOYABLE_PREFIXES = (
    "boards/",
    "cpynodus_ii/",
)


class OTAPackageError(ValueError):
    """Raised when an OTA package cannot be created safely."""


class OTATransferError(ValueError):
    """Raised when an OTA package cannot be transferred safely."""


def build_ota_package(
    repo_root,
    from_tag,
    to_tag,
    out_dir,
    *,
    created_at=None,
    include=None,
    exclude=None,
    signing_key="",
    key_id="",
    target=DEFAULT_PLATFORM,
    compiled_root=None,
):
    """Create an OTA package directory and return the manifest dictionary."""
    repo_path = Path(repo_root)
    out_path = Path(out_dir)
    platform = _normalize_target(target)
    circuitpython = TARGET_CIRCUITPYTHON[platform]
    compiled_path = (
        Path(compiled_root)
        if compiled_root
        else repo_path / "build" / "firmware" / platform
    )
    from_ref = str(from_tag)
    to_ref = str(to_tag)
    _require_git_ref(repo_path, from_ref)
    _require_git_ref(repo_path, to_ref)
    changed_paths = _git_lines(
        repo_path,
        "diff",
        "--name-only",
        "{}..{}".format(from_ref, to_ref),
    )
    if include:
        changed_paths.extend(str(path) for path in include)

    selected = []
    deleted = []
    excludes = set(str(path) for path in (exclude or ()))
    for raw_path in changed_paths:
        path = normalize_manifest_path(raw_path)
        if path in excludes:
            continue
        if not _is_deployable_path(path):
            continue
        if _git_path_exists(repo_path, to_ref, path):
            if path not in selected:
                selected.append(path)
        elif _git_path_exists(repo_path, from_ref, path) and path not in deleted:
            deleted.append(path)

    if out_path.exists():
        shutil.rmtree(out_path)
    files_root = out_path / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    file_entries = []
    packaged_paths = set()
    for path in sorted(selected):
        package_path = path
        if _is_python_module_path(path):
            _validate_compiled_root(
                compiled_path,
                platform,
                circuitpython,
                _version_at_ref(repo_path, to_ref),
            )
            package_path = "{}.mpy".format(path[:-3])
            artifact_path = compiled_path / Path(package_path)
            if not artifact_path.is_file():
                raise OTAPackageError(
                    "compiled_artifact_missing:{}".format(package_path)
                )
            payload = artifact_path.read_bytes()
            if path not in deleted:
                deleted.append(path)
        elif path.endswith(".py"):
            if path not in MPY_SOURCE_EXCEPTIONS:
                raise OTAPackageError("source_python_payload_forbidden:{}".format(path))
            payload = _git_file_bytes(repo_path, to_ref, path)
        else:
            payload = _git_file_bytes(repo_path, to_ref, path)
        if package_path in packaged_paths:
            raise OTAPackageError("duplicate_package_path:{}".format(package_path))
        packaged_paths.add(package_path)
        package_target = files_root / Path(package_path)
        package_target.parent.mkdir(parents=True, exist_ok=True)
        package_target.write_bytes(payload)
        file_entries.append(
            {
                "path": package_path,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    for path in tuple(deleted):
        if _is_python_module_path(path):
            compiled_delete = "{}.mpy".format(path[:-3])
            if compiled_delete not in deleted and path not in selected:
                deleted.append(compiled_delete)

    manifest = {
        "schema": SCHEMA,
        "package_id": _package_id(from_ref, to_ref),
        "from_tag": from_ref,
        "to_tag": to_ref,
        "created_at": created_at or _utc_timestamp(),
        "target": {
            "platform": platform,
            "circuitpython": circuitpython,
        },
        "requires": {
            "version": _version_at_ref(repo_path, from_ref),
        },
        "files": file_entries,
        "delete": sorted(deleted),
        "preserve": list(PRESERVED_CONFIG_FILES),
        "post_apply": {
            "reboot": True,
            "resume_profile": "previous",
            "reboot_delay_s": 5,
        },
    }
    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if signing_key:
        sign_manifest(
            out_path / "manifest.json",
            signing_key,
            key_id=key_id,
        )
    return manifest


def build_worktree_ota_package(
    repo_root,
    out_dir,
    *,
    package_id="",
    created_at=None,
    include=None,
    exclude=None,
    signing_key="",
    key_id="",
    target=DEFAULT_PLATFORM,
    compiled_root=None,
):
    """Create an OTA package from files currently present in the worktree."""
    repo_path = Path(repo_root)
    out_path = Path(out_dir)
    platform = _normalize_target(target)
    circuitpython = TARGET_CIRCUITPYTHON[platform]
    compiled_path = (
        Path(compiled_root)
        if compiled_root
        else repo_path / "build" / "firmware" / platform
    )
    selected = []
    excludes = set(str(path) for path in (exclude or ()))
    candidate_paths = include or _git_lines(
        repo_path,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    for raw_path in candidate_paths:
        path = normalize_manifest_path(raw_path)
        if path in excludes:
            continue
        if not _is_deployable_path(path):
            continue
        source = repo_path / Path(path)
        if source.exists() and source.is_file() and path not in selected:
            selected.append(path)

    if out_path.exists():
        shutil.rmtree(out_path)
    files_root = out_path / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    file_entries = []
    deleted = []
    packaged_paths = set()
    for path in sorted(selected):
        package_path = path
        if _is_python_module_path(path):
            _validate_compiled_root(
                compiled_path,
                platform,
                circuitpython,
                _version_from_worktree(repo_path),
            )
            package_path = "{}.mpy".format(path[:-3])
            artifact_path = compiled_path / Path(package_path)
            if not artifact_path.is_file():
                raise OTAPackageError(
                    "compiled_artifact_missing:{}".format(package_path)
                )
            payload = artifact_path.read_bytes()
            deleted.append(path)
        elif path.endswith(".py"):
            if path not in MPY_SOURCE_EXCEPTIONS:
                raise OTAPackageError(
                    "source_python_payload_forbidden:{}".format(path)
                )
            payload = (repo_path / Path(path)).read_bytes()
        else:
            payload = (repo_path / Path(path)).read_bytes()
        if package_path in packaged_paths:
            raise OTAPackageError("duplicate_package_path:{}".format(package_path))
        packaged_paths.add(package_path)
        package_target = files_root / Path(package_path)
        package_target.parent.mkdir(parents=True, exist_ok=True)
        package_target.write_bytes(payload)
        file_entries.append(
            {
                "path": package_path,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    manifest = {
        "schema": SCHEMA,
        "package_id": str(package_id or _worktree_package_id()),
        "from_tag": "",
        "to_tag": "working-tree",
        "source": "working-tree",
        "created_at": created_at or _utc_timestamp(),
        "target": {
            "platform": platform,
            "circuitpython": circuitpython,
        },
        "requires": {
            "version": _version_from_worktree(repo_path),
        },
        "files": file_entries,
        "delete": sorted(deleted),
        "preserve": list(PRESERVED_CONFIG_FILES),
        "post_apply": {
            "reboot": True,
            "resume_profile": "previous",
            "reboot_delay_s": 5,
        },
    }
    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if signing_key:
        sign_manifest(
            out_path / "manifest.json",
            signing_key,
            key_id=key_id,
        )
    return manifest


def push_ota_package(
    package_dir,
    device_url,
    *,
    timeout_s=10,
    opener=None,
    log_fn=None,
    chunk_size=1024,
    ready_timeout_s=60,
    ready_interval_s=2,
    session_id="",
):
    """Transfer an OTA package to one Nodus temporary OTA HTTP endpoint."""
    package_path = Path(package_dir)
    manifest = _read_manifest(package_path)
    raw_manifest = (package_path / "manifest.json").read_bytes()
    try:
        signature = load_signature(package_path)
    except OTASigningError as exc:
        raise OTATransferError(str(exc)) from exc
    if hashlib.sha256(raw_manifest).hexdigest() != signature.get("manifest_sha256"):
        raise OTATransferError("manifest_signature_digest_mismatch")
    ota_session = str(session_id or "").strip()
    if len(ota_session) < 32:
        raise OTATransferError("ota_session_missing")
    auth_headers = {
        "X-Nodus-OTA-Session": ota_session,
    }
    begin_headers = dict(auth_headers)
    begin_headers["X-Nodus-OTA-Key-Id"] = str(signature.get("key_id") or "")
    begin_headers["X-Nodus-OTA-Signature"] = str(signature.get("signature") or "")
    base_url = _normalize_device_url(device_url)
    http = opener or urlopen
    package_started = time.monotonic()
    total_bytes = _manifest_total_bytes(manifest)
    status = _wait_for_ota_ready(
        http,
        base_url,
        manifest,
        timeout_s,
        ready_timeout_s,
        ready_interval_s,
        log_fn=log_fn,
    )
    try:
        _log(
            log_fn,
            "begin package={} files={}".format(
                manifest["package_id"],
                len(manifest.get("files", ()) or ()),
            ),
        )
        begin = _request_json(
            http,
            "POST",
            "{}/ota/begin".format(base_url),
            timeout_s,
            body=raw_manifest,
            content_type="application/json",
            headers=begin_headers,
        )
        if begin.get("accepted") is not True:
            raise OTATransferError("begin_rejected:{}".format(begin.get("error", "")))

        for entry in manifest.get("files", ()) or ():
            path = normalize_manifest_path(entry.get("path", ""))
            file_path = package_path / "files" / Path(path)
            if not file_path.exists():
                raise OTATransferError("package_file_missing:{}".format(path))
            payload = file_path.read_bytes()
            expected_size = int(entry.get("size", -1) or -1)
            expected_sha = str(entry.get("sha256", "") or "")
            actual_sha = hashlib.sha256(payload).hexdigest()
            if len(payload) != expected_size:
                raise OTATransferError("package_file_size_mismatch:{}".format(path))
            if actual_sha != expected_sha:
                raise OTATransferError("package_file_sha256_mismatch:{}".format(path))
            chunk_bytes = int(chunk_size or 0)
            file_started = time.monotonic()
            _log(
                log_fn,
                "file {} bytes={} chunk={}".format(path, len(payload), chunk_bytes),
            )
            if chunk_bytes > 0:
                result = _push_file_chunks(
                    http,
                    base_url,
                    path,
                    payload,
                    timeout_s,
                    chunk_bytes,
                    headers=auth_headers,
                    log_fn=log_fn,
                )
            else:
                result = _request_json(
                    http,
                    "PUT",
                    "{}/ota/file?path={}".format(base_url, quote(path, safe="/")),
                    timeout_s,
                    body=payload,
                    content_type="application/octet-stream",
                    headers=_merge_headers(
                        auth_headers, {"X-Nodus-File-Path": path}
                    ),
                )
            if result.get("accepted") is not True:
                if chunk_bytes > 0 and _file_retryable_rejection(result):
                    _log(
                        log_fn,
                        "file retry {} reason={}".format(
                            path,
                            result.get("error", ""),
                        ),
                    )
                    result = _push_file_chunks(
                        http,
                        base_url,
                        path,
                        payload,
                        timeout_s,
                        chunk_bytes,
                        headers=auth_headers,
                        log_fn=log_fn,
                    )
                if result.get("accepted") is not True:
                    raise OTATransferError(
                        "file_rejected:{}:{}".format(path, result.get("error", ""))
                    )
            file_elapsed = time.monotonic() - file_started
            _log(
                log_fn,
                "file accepted {} elapsed_s={:.1f} rate_Bps={:.0f}".format(
                    path,
                    file_elapsed,
                    _bytes_per_second(len(payload), file_elapsed),
                ),
            )

        _log(log_fn, "commit")
        commit_started = time.monotonic()
        commit = _request_json(
            http,
            "POST",
            "{}/ota/commit".format(base_url),
            timeout_s,
            payload={},
            headers=auth_headers,
        )
        if commit.get("accepted") is not True:
            raise OTATransferError("commit_rejected:{}".format(commit.get("error", "")))
    except OTATransferError:
        _abort_ota_transfer(
            http,
            base_url,
            timeout_s,
            headers=auth_headers,
            log_fn=log_fn,
        )
        raise
    commit_elapsed = time.monotonic() - commit_started
    _log(
        log_fn,
        "committed phase={} rebooting={} delay_s={} elapsed_s={:.1f}".format(
            commit.get("phase", ""),
            commit.get("rebooting", False),
            commit.get("reboot_delay_s", 0),
            commit_elapsed,
        ),
    )
    package_elapsed = time.monotonic() - package_started
    _log(
        log_fn,
        "summary package={} files={} bytes={} elapsed_s={:.1f} rate_Bps={:.0f} "
        "commit_s={:.1f}".format(
            manifest["package_id"],
            len(manifest.get("files", ()) or ()),
            total_bytes,
            package_elapsed,
            _bytes_per_second(total_bytes, package_elapsed),
            commit_elapsed,
        ),
    )
    return {
        "status": status,
        "begin": begin,
        "commit": commit,
        "files": len(manifest.get("files", ()) or ()),
        "bytes": total_bytes,
        "elapsed_s": package_elapsed,
        "commit_s": commit_elapsed,
        "package_id": manifest["package_id"],
    }


def _abort_ota_transfer(http, base_url, timeout_s, *, headers=None, log_fn=None):
    _log(log_fn, "abort reason=transfer_failed")
    try:
        response = _request_json(
            http,
            "POST",
            "{}/ota/abort".format(base_url),
            timeout_s,
            payload={},
            headers=headers,
        )
    except OTATransferError as exc:
        _log(log_fn, "abort failed reason={}".format(exc))
        return
    if response.get("accepted") is not True:
        _log(log_fn, "abort rejected reason={}".format(response.get("error", "")))
        return
    _log(log_fn, "abort complete phase={}".format(response.get("phase", "")))


def _push_file_chunks(
    http,
    base_url,
    path,
    payload,
    timeout_s,
    chunk_size,
    *,
    headers=None,
    log_fn=None,
):
    encoded_path = quote(path, safe="/")
    begin = _request_json(
        http,
        "POST",
        "{}/ota/file/begin?path={}".format(base_url, encoded_path),
        timeout_s,
        payload={},
        headers=_merge_headers(headers, {"X-Nodus-File-Path": path}),
    )
    if begin.get("accepted") is not True:
        return begin
    offset = 0
    payload_size = len(payload)
    started = time.monotonic()
    while offset < payload_size:
        next_offset = min(payload_size, offset + int(chunk_size))
        chunk = payload[offset:next_offset]
        result = _request_json(
            http,
            "PUT",
            "{}/ota/file/chunk?path={}&offset={}".format(
                base_url,
                encoded_path,
                offset,
            ),
            timeout_s,
            body=chunk,
            content_type="application/octet-stream",
            headers=_merge_headers(headers, {"X-Nodus-File-Path": path}),
        )
        if result.get("accepted") is not True:
            return result
        offset = int(result.get("offset", next_offset) or next_offset)
        elapsed = time.monotonic() - started
        _log(
            log_fn,
            "chunk {} offset={}/{} elapsed_s={:.1f} rate_Bps={:.0f}".format(
                path,
                offset,
                payload_size,
                elapsed,
                _bytes_per_second(offset, elapsed),
            ),
        )
    return _request_json_with_retries(
        http,
        "POST",
        "{}/ota/file/end?path={}".format(base_url, encoded_path),
        timeout_s,
        payload={},
        headers=_merge_headers(headers, {"X-Nodus-File-Path": path}),
        retries=2,
        retry_delay_s=2,
        log_fn=log_fn,
        label="file end {}".format(path),
    )


def _file_retryable_rejection(result):
    return str(result.get("error", "") or "") in {
        "file_size_mismatch",
        "sha256_mismatch",
        "staged_file_missing",
    }


def _request_json_with_retries(
    opener,
    method,
    url,
    timeout_s,
    *,
    payload=None,
    body=None,
    content_type="application/json",
    headers=None,
    retries=0,
    retry_delay_s=1,
    log_fn=None,
    label="request",
):
    attempt = 0
    while True:
        attempt += 1
        try:
            return _request_json(
                opener,
                method,
                url,
                timeout_s,
                payload=payload,
                body=body,
                content_type=content_type,
                headers=headers,
            )
        except OTATransferError as exc:
            error = str(exc)
            if attempt > int(retries or 0) or not _ota_status_error_retryable(error):
                raise
            _log(
                log_fn,
                "{} retry attempt={} reason={}".format(label, attempt + 1, error),
            )
            time.sleep(max(0.0, float(retry_delay_s or 0.0)))


def _manifest_total_bytes(manifest):
    total = 0
    for entry in manifest.get("files", ()) or ():
        try:
            total += int(entry.get("size", 0) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _bytes_per_second(byte_count, elapsed_s):
    elapsed = float(elapsed_s or 0)
    if elapsed <= 0:
        return 0.0
    return float(byte_count or 0) / elapsed


def prepare_fwupdate(
    broker,
    device_id,
    package_id,
    *,
    port=1883,
    base_topic="nodus",
    username="",
    password="",
    message_id="",
    session_id="",
    manifest_sha256="",
    key_id="",
    mqtt_client_factory=None,
    log_fn=None,
):
    """Publish one MQTT firmware-update prepare command."""
    topic = _fwupdate_topic(device_id, base_topic=base_topic)
    if not topic:
        raise OTATransferError("fwupdate_topic_invalid")
    payload = {
        "schema": "nodus-fwupdate/v2",
        "message_id": message_id or _message_id(),
        "command": "prepare",
        "package_id": str(package_id or ""),
        "session_id": str(session_id or ""),
        "manifest_sha256": str(manifest_sha256 or ""),
        "key_id": str(key_id or ""),
    }
    if not payload["package_id"]:
        raise OTATransferError("package_id_missing")
    if len(payload["session_id"]) < 32:
        raise OTATransferError("ota_session_missing")
    if len(payload["manifest_sha256"]) != 64:
        raise OTATransferError("manifest_sha256_invalid")
    if not payload["key_id"]:
        raise OTATransferError("ota_signing_key_missing")
    client_factory = mqtt_client_factory or _paho_client_factory
    client = client_factory()
    _log(log_fn, "mqtt connect broker={} port={}".format(broker, int(port or 1883)))
    if username:
        username_pw_set = getattr(client, "username_pw_set", None)
        if callable(username_pw_set):
            username_pw_set(username, password or None)
    connect = getattr(client, "connect", None)
    publish = getattr(client, "publish", None)
    disconnect = getattr(client, "disconnect", None)
    if not callable(connect) or not callable(publish):
        raise OTATransferError("mqtt_client_invalid")
    connect(str(broker or ""), int(port or 1883), 60)
    loop_start = getattr(client, "loop_start", None)
    loop_stop = getattr(client, "loop_stop", None)
    if callable(loop_start):
        loop_start()
    _log(log_fn, "mqtt publish topic={} package={}".format(topic, package_id))
    info = publish(topic, json.dumps(payload, separators=(",", ":")), qos=1)
    wait = getattr(info, "wait_for_publish", None)
    if callable(wait):
        wait()
    if callable(loop_stop):
        loop_stop()
    if callable(disconnect):
        disconnect()
    _log(log_fn, "mqtt published message_id={}".format(payload["message_id"]))
    return {
        "topic": topic,
        "payload": payload,
    }


def normalize_manifest_path(path):
    """Return a safe relative POSIX path for a package manifest entry."""
    value = str(path or "").replace("\\", "/").strip()
    value = posixpath.normpath(value)
    if value in {"", "."}:
        raise OTAPackageError("empty_package_path")
    if value.startswith("../") or value == ".." or value.startswith("/"):
        raise OTAPackageError("unsafe_package_path:{}".format(path))
    return value


def main(argv=None):
    """Run the host-side OTA package command line interface."""
    parser = argparse.ArgumentParser(prog="nodus-ota")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common_package_args = argparse.ArgumentParser(add_help=False)
    common_package_args.add_argument("--out", required=True)
    common_package_args.add_argument("--repo", default=".")
    common_package_args.add_argument("--include", action="append")
    common_package_args.add_argument("--exclude", action="append")
    common_package_args.add_argument("--signing-key", required=True)
    common_package_args.add_argument("--key-id", default="")

    package_parser = subparsers.add_parser("package", parents=[common_package_args])
    package_parser.add_argument("--from", dest="from_tag", required=True)
    package_parser.add_argument("--to", dest="to_tag", required=True)
    package_parser.add_argument(
        "--target",
        choices=tuple(sorted(TARGET_CIRCUITPYTHON)),
        default=DEFAULT_PLATFORM,
    )
    package_parser.add_argument("--compiled-root")
    worktree_parser = subparsers.add_parser(
        "package-worktree", parents=[common_package_args]
    )
    worktree_parser.add_argument("--package-id", default="")
    worktree_parser.add_argument(
        "--target",
        choices=tuple(sorted(TARGET_CIRCUITPYTHON)),
        default=DEFAULT_PLATFORM,
    )
    worktree_parser.add_argument("--compiled-root")
    keygen_parser = subparsers.add_parser("keygen")
    keygen_parser.add_argument("--private-key", required=True)
    keygen_parser.add_argument("--public-key", required=True)
    keygen_parser.add_argument("--openssl", default="openssl")
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("package")
    prepare_parser.add_argument("--broker", required=True)
    prepare_parser.add_argument("--port", type=int, default=1883)
    prepare_parser.add_argument("--device-id", required=True)
    prepare_parser.add_argument("--base-topic", default="nodus")
    prepare_parser.add_argument("--username", default="")
    prepare_parser.add_argument("--password", default="")
    prepare_parser.add_argument("--message-id", default="")
    prepare_parser.add_argument("--session-id", required=True)
    push_parser = subparsers.add_parser("push")
    push_parser.add_argument("package")
    push_parser.add_argument("--device", required=True)
    push_parser.add_argument("--timeout", type=float, default=300.0)
    push_parser.add_argument("--prepare", action="store_true")
    push_parser.add_argument("--broker", default="")
    push_parser.add_argument("--port", type=int, default=1883)
    push_parser.add_argument("--device-id", default="")
    push_parser.add_argument("--base-topic", default="nodus")
    push_parser.add_argument("--username", default="")
    push_parser.add_argument("--password", default="")
    push_parser.add_argument("--message-id", default="")
    push_parser.add_argument("--session-id", default="")
    push_parser.add_argument("--wait-after-prepare", type=float, default=0.0)
    push_parser.add_argument("--ready-timeout", type=float, default=60.0)
    push_parser.add_argument("--ready-interval", type=float, default=2.0)
    push_parser.add_argument("--chunk-size", type=int, default=1024)
    args = parser.parse_args(argv)
    log = timestamp_logger()

    if args.command == "package":
        manifest = build_ota_package(
            args.repo,
            args.from_tag,
            args.to_tag,
            args.out,
            include=args.include or (),
            exclude=args.exclude or (),
            signing_key=args.signing_key,
            key_id=args.key_id,
            target=args.target,
            compiled_root=args.compiled_root,
        )
        log(
            "created {package_id} files={files} delete={delete}".format(
                package_id=manifest["package_id"],
                files=len(manifest["files"]),
                delete=len(manifest["delete"]),
            )
        )
        return 0
    if args.command == "package-worktree":
        manifest = build_worktree_ota_package(
            args.repo,
            args.out,
            package_id=args.package_id,
            include=args.include or (),
            exclude=args.exclude or (),
            signing_key=args.signing_key,
            key_id=args.key_id,
            target=args.target,
            compiled_root=args.compiled_root,
        )
        log(
            "created {package_id} source=working-tree files={files}".format(
                package_id=manifest["package_id"],
                files=len(manifest["files"]),
            )
        )
        return 0
    if args.command == "keygen":
        document = generate_signing_key(
            args.private_key,
            args.public_key,
            openssl=args.openssl,
        )
        log(
            "created OTA signing key key_id={} public_key={}".format(
                document["key_id"],
                args.public_key,
            )
        )
        return 0
    if args.command == "prepare":
        package_path = Path(args.package)
        manifest = _read_manifest(package_path)
        try:
            signature = load_signature(package_path)
        except OTASigningError as exc:
            log("prepare failed: {}".format(exc))
            return 1
        ota_session = args.session_id
        try:
            prepare_fwupdate(
                args.broker,
                args.device_id,
                manifest["package_id"],
                port=args.port,
                base_topic=args.base_topic,
                username=args.username,
                password=args.password,
                message_id=args.message_id,
                session_id=ota_session,
                manifest_sha256=signature["manifest_sha256"],
                key_id=signature["key_id"],
                log_fn=log,
            )
        except OTATransferError as exc:
            log("prepare failed: {}".format(exc))
            return 1
        log("prepare complete package={}".format(manifest["package_id"]))
        return 0
    if args.command == "push":
        try:
            try:
                signature = load_signature(Path(args.package))
            except OTASigningError as exc:
                raise OTATransferError(str(exc)) from exc
            ota_session = args.session_id or (
                secrets.token_hex(16) if args.prepare else ""
            )
            if args.prepare:
                if not args.broker or not args.device_id:
                    raise OTATransferError("prepare_requires_broker_and_device_id")
                manifest = _read_manifest(Path(args.package))
                prepare_fwupdate(
                    args.broker,
                    args.device_id,
                    manifest["package_id"],
                    port=args.port,
                    base_topic=args.base_topic,
                    username=args.username,
                    password=args.password,
                    message_id=args.message_id,
                    session_id=ota_session,
                    manifest_sha256=signature["manifest_sha256"],
                    key_id=signature["key_id"],
                    log_fn=log,
                )
                _sleep_after_prepare(args.wait_after_prepare, log_fn=log)
            result = push_ota_package(
                args.package,
                args.device,
                timeout_s=args.timeout,
                chunk_size=args.chunk_size,
                ready_timeout_s=args.ready_timeout,
                ready_interval_s=args.ready_interval,
                session_id=ota_session,
                log_fn=log,
            )
        except OTATransferError as exc:
            log("push failed: {}".format(exc))
            return 1
        log(
            "pushed {package_id} files={files}".format(
                package_id=result["package_id"],
                files=result["files"],
            )
        )
        return 0
    return 2


def timestamp_logger(stream=None, *, clock=None):
    """Return a console logger that prefixes messages with local timestamps."""
    output = stream or sys.stdout
    clock_fn = clock or datetime.now

    def _logger(message):
        stamp = clock_fn().replace(microsecond=0).isoformat()
        print("[{}] {}".format(stamp, message), file=output)

    return _logger


def _read_manifest(package_path):
    manifest_path = Path(package_path) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OTATransferError("manifest_missing") from exc
    except ValueError as exc:
        raise OTATransferError("manifest_invalid_json") from exc
    if not isinstance(manifest, dict):
        raise OTATransferError("manifest_invalid_shape")
    if manifest.get("schema") != SCHEMA:
        raise OTATransferError("manifest_schema_invalid")
    if not str(manifest.get("package_id", "") or "").strip():
        raise OTATransferError("manifest_package_id_missing")
    if not isinstance(manifest.get("files", ()), list):
        raise OTATransferError("manifest_files_invalid")
    return manifest


def _wait_for_ota_ready(
    http,
    base_url,
    manifest,
    timeout_s,
    ready_timeout_s,
    ready_interval_s,
    *,
    log_fn=None,
):
    deadline = time.monotonic() + max(0.0, float(ready_timeout_s or 0.0))
    interval = max(0.1, float(ready_interval_s or 0.0))
    status_url = "{}/ota/status".format(base_url)
    last_error = ""
    attempt = 0
    while True:
        attempt += 1
        _log(log_fn, "status {} attempt={}".format(base_url, attempt))
        try:
            status = _request_json(
                http,
                "GET",
                status_url,
                min(float(timeout_s or 10), 5.0),
            )
        except OTATransferError as exc:
            last_error = str(exc)
            if not _ota_status_error_retryable(last_error):
                raise
            status = None
        if status is not None:
            device_package = status.get("package_id")
            if device_package and device_package != manifest["package_id"]:
                raise OTATransferError(
                    "device_package_mismatch:{}!={}".format(
                        device_package,
                        manifest["package_id"],
                    )
                )
            phase = str(status.get("phase", "") or "")
            if phase == "ready":
                return status
            last_error = "device_not_ready:phase={}".format(phase)
        if time.monotonic() >= deadline:
            raise OTATransferError("device_ready_timeout:{}".format(last_error))
        _log(log_fn, "status retry reason={}".format(last_error or "unknown"))
        time.sleep(interval)


def _ota_status_error_retryable(error):
    text = str(error or "")
    return text.startswith("http_unreachable:") or text.startswith("http_failed:")


def _normalize_device_url(device_url):
    url = str(device_url or "").strip().rstrip("/")
    if not url:
        raise OTATransferError("device_url_missing")
    if "://" not in url:
        url = "http://{}".format(url)
    return url


def _merge_headers(first, second):
    merged = dict(first or {})
    merged.update(second or {})
    return merged


def _request_json(
    opener,
    method,
    url,
    timeout_s,
    *,
    payload=None,
    body=None,
    content_type="application/json",
    headers=None,
):
    data = body
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request_headers = {
        "Content-Type": content_type,
        "Accept": "application/json",
    }
    request_headers.update(headers or {})
    request = Request(
        url,
        data=data,
        method=method,
        headers=request_headers,
    )
    try:
        with opener(request, timeout=float(timeout_s or 10)) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        try:
            error_payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            error_payload = {"error": "http_{}".format(exc.code)}
        return error_payload
    except URLError as exc:
        raise OTATransferError("http_unreachable:{}".format(exc.reason)) from exc
    except OSError as exc:
        raise OTATransferError("http_failed:{}".format(exc)) from exc
    try:
        decoded = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw or "")
        result = json.loads(decoded or "{}")
    except ValueError as exc:
        raise OTATransferError("http_invalid_json:{}".format(url)) from exc
    if not isinstance(result, dict):
        raise OTATransferError("http_invalid_shape:{}".format(url))
    return result


def _log(log_fn, message):
    if callable(log_fn):
        log_fn(message)


def _is_deployable_path(path):
    if path in EXCLUDED_NAMES:
        return False
    for prefix in EXCLUDED_PREFIXES:
        if path.startswith(prefix):
            return False
    if path in ROOT_DEPLOYABLE:
        return True
    for prefix in DEPLOYABLE_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _is_python_module_path(path):
    return str(path).startswith("cpynodus_ii/") and str(path).endswith(".py")


def _normalize_target(target):
    platform = str(target or DEFAULT_PLATFORM).strip().lower()
    if platform not in TARGET_CIRCUITPYTHON:
        raise OTAPackageError("unsupported_target:{}".format(platform))
    return platform


def _validate_compiled_root(compiled_root, target, circuitpython, project_version):
    if not compiled_root.is_dir():
        raise OTAPackageError("compiled_root_missing:{}".format(compiled_root))
    info_path = compiled_root / "BUILD_INFO"
    if not info_path.is_file():
        raise OTAPackageError("compiled_build_info_missing:{}".format(info_path))
    info = {}
    for raw_line in info_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = raw_line.partition("=")
        if separator:
            info[key.strip()] = value.strip()
    expected = {
        "format": "cpynodus-mpy-build-v1",
        "target": target,
        "circuitpython": circuitpython,
        "mpy_abi": "mpy v6.3",
        "project_version": project_version,
    }
    for key, expected_value in expected.items():
        actual_value = info.get(key, "")
        if actual_value != expected_value:
            raise OTAPackageError(
                "compiled_build_info_mismatch:{}:{}!={}".format(
                    key,
                    actual_value or "missing",
                    expected_value,
                )
            )


def _package_id(from_ref, to_ref):
    return "ota-{}-to-{}".format(_slug(from_ref), _slug(to_ref))


def _slug(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")


def _utc_timestamp():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _worktree_package_id():
    return "ota-working-tree-{}".format(
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )


def _message_id():
    return "fw-{}".format(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def _require_git_ref(repo_path, ref):
    try:
        _git(repo_path, "rev-parse", "--verify", "{}^{{commit}}".format(ref))
    except OTAPackageError as exc:
        raise OTAPackageError("missing_git_ref:{}".format(ref)) from exc


def _git_path_exists(repo_path, ref, path):
    try:
        _git(repo_path, "cat-file", "-e", "{}:{}".format(ref, path))
        return True
    except OTAPackageError:
        return False


def _git_file_bytes(repo_path, ref, path):
    return _git(repo_path, "show", "{}:{}".format(ref, path), text=False)


def _git_lines(repo_path, *args):
    output = _git(repo_path, *args)
    return [line.strip() for line in output.splitlines() if line.strip()]


def _git(repo_path, *args, text=True):
    cmd = ("git",) + tuple(str(arg) for arg in args)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(repo_path),
            check=True,
            capture_output=True,
            text=text,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr if text else exc.stderr.decode("utf-8", "replace")
        raise OTAPackageError(
            "git_failed:{}:{}".format(" ".join(cmd), detail.strip())
        ) from exc
    return completed.stdout


def _version_at_ref(repo_path, ref):
    try:
        source = _git(repo_path, "show", "{}:cpynodus_ii/__init__.py".format(ref))
    except OTAPackageError:
        return ""
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", source)
    return match.group(1) if match else ""


def _version_from_worktree(repo_path):
    try:
        source = (Path(repo_path) / "cpynodus_ii" / "__init__.py").read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", source)
    return match.group(1) if match else ""


def _fwupdate_topic(device_id, *, base_topic="nodus"):
    base = str(base_topic or "nodus").strip().strip("/")
    device = str(device_id or "").strip().strip("/")
    if not base or not device or "/" in device:
        return ""
    return "{}/{}/fwupdate".format(base, device)


def _paho_client_factory():
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise OTATransferError("paho_mqtt_not_installed") from exc
    return mqtt.Client()


def _sleep_after_prepare(delay_s, *, log_fn=None):
    delay = max(0.0, float(delay_s or 0.0))
    if delay <= 0:
        return
    _log(log_fn, "wait after prepare seconds={}".format(delay))
    time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
