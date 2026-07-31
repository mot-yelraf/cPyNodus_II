"""Verify detached RSA signatures for OTA manifests.

``load_public_key``, ``sha256_hex``, and ``verify_manifest_signature`` support
the fixed RSA-2048 PKCS#1 v1.5/SHA-256 trust format used by Nodus OTA. The
implementation stays compatible with CircuitPython's limited crypto modules.
"""

import binascii
import hashlib
import json

OTA_SIGNATURE_ALGORITHM = "rsa-pkcs1v15-sha256"
OTA_PUBLIC_KEY_FILE = "ota-public-key.json"

_SHA256_DIGEST_INFO = binascii.unhexlify(
    b"3031300d060960864801650304020105000420"
)


def load_public_key(path=OTA_PUBLIC_KEY_FILE):
    """Load one compact trusted OTA public-key document."""
    try:
        with open(path, "r") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema") != "nodus-ota-public-key/v1":
        return None
    if document.get("algorithm") != OTA_SIGNATURE_ALGORITHM:
        return None
    if not str(document.get("key_id", "") or "").strip():
        return None
    modulus = str(document.get("modulus", "") or "").strip().lower()
    try:
        exponent = int(document.get("exponent", 65537) or 65537)
        int(modulus, 16)
    except (TypeError, ValueError, binascii.Error):
        return None
    if exponent < 3 or not modulus:
        return None
    return document


def sha256_hex(payload):
    """Return the lowercase SHA-256 digest for bytes."""
    digest = _sha256()
    digest.update(bytes(payload))
    return _hexlify(digest.digest())


def verify_manifest_signature(payload, signature_text, key_document, key_id=""):
    """Return a short error string, or empty string for a valid signature."""
    if not isinstance(key_document, dict):
        return "ota_trust_key_missing"
    trusted_id = str(key_document.get("key_id", "") or "").strip()
    requested_id = str(key_id or "").strip()
    if not requested_id or requested_id != trusted_id:
        return "ota_signing_key_mismatch"
    try:
        signature = binascii.a2b_base64(str(signature_text or "").encode("ascii"))
        modulus = int(str(key_document.get("modulus", "") or ""), 16)
        exponent = int(key_document.get("exponent", 65537) or 65537)
    except (TypeError, ValueError):
        return "ota_signature_invalid"
    size = (modulus.bit_length() + 7) // 8
    if size != 256 or len(signature) != size:
        return "ota_signature_invalid"
    try:
        decoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(
            size, "big"
        )
    except (OverflowError, ValueError):
        return "ota_signature_invalid"
    digest = _sha256()
    digest.update(bytes(payload))
    expected_tail = _SHA256_DIGEST_INFO + digest.digest()
    padding_size = size - len(expected_tail) - 3
    if padding_size < 8:
        return "ota_signature_invalid"
    expected = b"\x00\x01" + (b"\xff" * padding_size) + b"\x00" + expected_tail
    if not _constant_time_equal(decoded, expected):
        return "ota_signature_invalid"
    return ""


def _sha256():
    sha256 = getattr(hashlib, "sha256", None)
    if callable(sha256):
        try:
            return sha256()
        except Exception:
            pass
    new_digest = getattr(hashlib, "new", None)
    if callable(new_digest):
        try:
            return new_digest("sha256")
        except Exception:
            pass
    from cpynodus_ii.ota.http import _StreamingSha256

    return _StreamingSha256()


def _hexlify(payload):
    value = binascii.hexlify(payload)
    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value)


def _constant_time_equal(left, right):
    left_bytes = bytes(left)
    right_bytes = bytes(right)
    mismatch = len(left_bytes) ^ len(right_bytes)
    limit = min(len(left_bytes), len(right_bytes))
    for index in range(limit):
        mismatch |= left_bytes[index] ^ right_bytes[index]
    return mismatch == 0
