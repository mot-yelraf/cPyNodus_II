"""Focused tests for signed OTA authentication and manifest policy."""

import json
import shutil

import pytest

from cpynodus_ii.ota import auth as ota_auth
from cpynodus_ii.ota.auth import load_public_key, verify_manifest_signature
from cpynodus_ii.ota.http import _validate_manifest_for_begin
from cpynodus_ii.ota.state import FwUpdateState
from scripts.ota_signing import generate_signing_key, sign_manifest


def _manifest():
    return {
        "schema": "nodus-ota/v2",
        "package_id": "cpynodusii_test",
        "target": {
            "platform": "pico2w",
            "circuitpython": "9.2.8",
        },
        "requires": {"version": "v0.26.211.1"},
        "files": [
            {
                "path": "cpynodus_ii/app.mpy",
                "size": 4,
                "sha256": "a" * 64,
            }
        ],
        "delete": ["cpynodus_ii/app.py"],
        "preserve": ["settings.toml", "ota-public-key.json"],
    }


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl unavailable")
def test_device_verifier_accepts_exact_signed_manifest_and_rejects_tampering(
    tmp_path,
):
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "ota-public-key.json"
    document = generate_signing_key(private_key, public_key)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    signature = sign_manifest(manifest_path, private_key)
    payload = manifest_path.read_bytes()
    trusted = load_public_key(str(public_key))

    assert (
        verify_manifest_signature(
            payload,
            signature["signature"],
            trusted,
            document["key_id"],
        )
        == ""
    )
    assert (
        verify_manifest_signature(
            payload + b" ",
            signature["signature"],
            trusted,
            document["key_id"],
        )
        == "ota_signature_invalid"
    )


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl unavailable")
def test_device_verifier_uses_streaming_fallback_without_hashlib_sha256(
    tmp_path, monkeypatch
):
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "ota-public-key.json"
    document = generate_signing_key(private_key, public_key)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    signature = sign_manifest(manifest_path, private_key)
    monkeypatch.setattr(ota_auth, "hashlib", type("_FakeHashlib", (), {}))

    assert (
        verify_manifest_signature(
            manifest_path.read_bytes(),
            signature["signature"],
            load_public_key(str(public_key)),
            document["key_id"],
        )
        == ""
    )


@pytest.mark.parametrize(
    ("target", "circuitpython"),
    (("pico2w", "9.2.8"), ("xesp32s3", "10.2.1")),
)
def test_signed_manifest_policy_accepts_both_supported_targets(
    target, circuitpython
):
    state = FwUpdateState(package_id="cpynodusii_test")
    manifest = _manifest()
    manifest["target"] = {
        "platform": target,
        "circuitpython": circuitpython,
    }

    assert (
        _validate_manifest_for_begin(
            manifest,
            state,
            version="v0.26.211.1",
            platform=target,
            circuitpython=circuitpython,
        )
        == ""
    )


def test_signed_manifest_policy_requires_mpy_modules_and_source_deletion():
    state = FwUpdateState(package_id="cpynodusii_test")
    manifest = _manifest()
    manifest["files"][0]["path"] = "cpynodus_ii/app.py"

    assert (
        _validate_manifest_for_begin(
            manifest,
            state,
            version="v0.26.211.1",
            platform="pico2w",
            circuitpython="9.2.8",
        )
        == "manifest_file_path_forbidden"
    )

    manifest = _manifest()
    manifest["delete"] = []
    assert (
        _validate_manifest_for_begin(
            manifest,
            state,
            version="v0.26.211.1",
            platform="pico2w",
            circuitpython="9.2.8",
        )
        == "manifest_source_delete_missing"
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("schema", "nodus-ota/v1", "manifest_schema_invalid"),
        ("platform", "xesp32s3", "manifest_platform_mismatch"),
        ("circuitpython", "10.2.1", "manifest_circuitpython_mismatch"),
        ("version", "v0.26.210.1", "manifest_version_mismatch"),
    ),
)
def test_signed_manifest_policy_rejects_version_and_target_mismatch(
    field, value, error
):
    state = FwUpdateState(package_id="cpynodusii_test")
    manifest = _manifest()
    if field == "schema":
        manifest["schema"] = value
    elif field in {"platform", "circuitpython"}:
        manifest["target"][field] = value
    else:
        manifest["requires"]["version"] = value

    assert (
        _validate_manifest_for_begin(
            manifest,
            state,
            version="v0.26.211.1",
            platform="pico2w",
            circuitpython="9.2.8",
        )
        == error
    )
