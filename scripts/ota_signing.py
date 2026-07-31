"""Create and verify host-side Nodus OTA signing material.

``generate_signing_key``, ``sign_manifest``, and ``verify_manifest`` implement
the fixed RSA-2048 PKCS#1 v1.5/SHA-256 format; ``load_signature`` validates its
detached document. Private-key operations require the OpenSSL executable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

SIGNATURE_SCHEMA = "nodus-ota-signature/v1"
PUBLIC_KEY_SCHEMA = "nodus-ota-public-key/v1"
ALGORITHM = "rsa-pkcs1v15-sha256"
_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class OTASigningError(ValueError):
    """Raised when OTA signing material is invalid or unavailable."""


def generate_signing_key(private_key, public_key, *, openssl="openssl"):
    """Create an RSA-2048 key and compact public trust document."""
    private_path = Path(private_key)
    public_path = Path(public_key)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            openssl,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_path),
        ]
    )
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass
    modulus_output = _run(
        [openssl, "rsa", "-in", str(private_path), "-noout", "-modulus"]
    )
    match = re.search(r"Modulus=([0-9A-Fa-f]+)", modulus_output)
    if not match:
        raise OTASigningError("ota_public_modulus_unavailable")
    modulus = match.group(1).lower()
    key_id = hashlib.sha256(bytes.fromhex(modulus)).hexdigest()[:16]
    document = {
        "schema": PUBLIC_KEY_SCHEMA,
        "algorithm": ALGORITHM,
        "key_id": key_id,
        "modulus": modulus,
        "exponent": 65537,
    }
    public_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def sign_manifest(manifest_path, private_key, *, key_id="", openssl="openssl"):
    """Sign exact manifest bytes and write its detached signature document."""
    path = Path(manifest_path)
    payload = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="nodus-ota-sign-") as temp_dir:
        signature_path = Path(temp_dir) / "manifest.sig.bin"
        _run(
            [
                openssl,
                "dgst",
                "-sha256",
                "-sign",
                str(Path(private_key)),
                "-out",
                str(signature_path),
                str(path),
            ]
        )
        signature = signature_path.read_bytes()
    resolved_key_id = str(key_id or "").strip()
    if not resolved_key_id:
        modulus_output = _run(
            [openssl, "rsa", "-in", str(Path(private_key)), "-noout", "-modulus"]
        )
        match = re.search(r"Modulus=([0-9A-Fa-f]+)", modulus_output)
        if not match:
            raise OTASigningError("ota_public_modulus_unavailable")
        resolved_key_id = hashlib.sha256(
            bytes.fromhex(match.group(1))
        ).hexdigest()[:16]
    document = {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": ALGORITHM,
        "key_id": resolved_key_id,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    signature_file = path.with_name("manifest.sig")
    signature_file.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def load_signature(package_dir):
    """Load and validate a detached OTA signature document."""
    path = Path(package_dir) / "manifest.sig"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OTASigningError("ota_signature_missing") from exc
    except ValueError as exc:
        raise OTASigningError("ota_signature_invalid_json") from exc
    if not isinstance(document, dict):
        raise OTASigningError("ota_signature_invalid_shape")
    if document.get("schema") != SIGNATURE_SCHEMA:
        raise OTASigningError("ota_signature_schema_invalid")
    if document.get("algorithm") != ALGORITHM:
        raise OTASigningError("ota_signature_algorithm_invalid")
    if not str(document.get("key_id", "") or ""):
        raise OTASigningError("ota_signature_key_id_missing")
    manifest_sha256 = str(document.get("manifest_sha256", "") or "").lower()
    if len(manifest_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in manifest_sha256
    ):
        raise OTASigningError("ota_signature_manifest_sha256_invalid")
    if not str(document.get("signature", "") or ""):
        raise OTASigningError("ota_signature_missing")
    return document


def verify_manifest(manifest_bytes, signature_document, public_key_document):
    """Verify exact manifest bytes with one compact RSA public key."""
    if signature_document.get("algorithm") != ALGORITHM:
        return False
    if public_key_document.get("algorithm") != ALGORITHM:
        return False
    if signature_document.get("key_id") != public_key_document.get("key_id"):
        return False
    if hashlib.sha256(manifest_bytes).hexdigest() != signature_document.get(
        "manifest_sha256"
    ):
        return False
    try:
        signature = base64.b64decode(signature_document["signature"], validate=True)
        modulus = int(public_key_document["modulus"], 16)
        exponent = int(public_key_document.get("exponent", 65537))
    except (KeyError, TypeError, ValueError):
        return False
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    decoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(
        size, "big"
    )
    tail = _DIGEST_INFO + hashlib.sha256(manifest_bytes).digest()
    padding_size = size - len(tail) - 3
    expected = b"\x00\x01" + (b"\xff" * padding_size) + b"\x00" + tail
    return decoded == expected


def _run(command):
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise OTASigningError("openssl_unavailable") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise OTASigningError("openssl_failed:{}".format(detail))
    return str(result.stdout or "")
