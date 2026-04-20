"""Password obfuscation compatibility for `obf1:` settings values."""

import binascii
import hashlib


PASSWORD_OBF_PREFIX = "obf1"
PASSWORD_OBF_NONCE_LEN = 8


def decode_password(stored, *, hostname=""):
    """Decode an `obf1:` password string, returning cleartext or empty string."""
    if stored is None:
        return ""
    if not isinstance(stored, str):
        stored = str(stored)
    if stored == "":
        return ""
    if not stored.startswith(PASSWORD_OBF_PREFIX + ":"):
        return stored

    try:
        _prefix, nonce_b64, cipher_b64 = stored.split(":", 2)
        nonce = _b64decode_bytes(nonce_b64)
        cipher = _b64decode_bytes(cipher_b64)
    except Exception:
        return ""

    keys_to_try = [_build_obf_key(include_hostname=False, hostname=hostname)]
    legacy_key = _build_obf_key(include_hostname=True, hostname=hostname)
    if legacy_key is not None:
        keys_to_try.append(legacy_key)

    for key in keys_to_try:
        try:
            stream = _obf_keystream(nonce, len(cipher), key=key)
            raw = bytes((cipher[i] ^ stream[i]) for i in range(len(cipher)))
            return raw.decode("utf-8")
        except Exception:
            pass
    return ""


def encode_password(cleartext, *, hostname="", nonce=None):
    """Encode cleartext using the `obf1:` format."""
    if cleartext is None:
        return ""
    if not isinstance(cleartext, str):
        cleartext = str(cleartext)
    if cleartext == "":
        return ""
    if cleartext.startswith(PASSWORD_OBF_PREFIX + ":"):
        return cleartext
    raw = cleartext.encode("utf-8")
    nonce = bytes(nonce) if nonce is not None else b"\x00" * PASSWORD_OBF_NONCE_LEN
    stream = _obf_keystream(nonce, len(raw), key=_build_obf_key(include_hostname=False, hostname=hostname))
    cipher = bytes((raw[i] ^ stream[i]) for i in range(len(raw)))
    return "{}:{}:{}".format(
        PASSWORD_OBF_PREFIX,
        _b64encode_bytes(nonce),
        _b64encode_bytes(cipher),
    )


def _build_obf_key(*, include_hostname, hostname=""):
    parts = []
    try:
        import microcontroller  # type: ignore

        uid = getattr(getattr(microcontroller, "cpu", None), "uid", None)
        if isinstance(uid, (bytes, bytearray)):
            parts.append(bytes(uid))
        elif uid is not None:
            parts.append(str(uid).encode("utf-8"))
    except Exception:
        pass

    try:
        import board  # type: ignore

        board_id = getattr(board, "board_id", None)
        if board_id:
            parts.append(str(board_id).encode("utf-8"))
    except Exception:
        pass

    if include_hostname:
        host = str(hostname or "").strip()
        if host:
            parts.append(host.encode("utf-8"))
        else:
            return None

    parts.append(b"nodus-password-obf-v1")
    seed = b"|".join(parts)
    return _digest32((seed,))


def _obf_keystream(nonce, n_bytes, *, key):
    out = bytearray()
    counter = 0
    while len(out) < n_bytes:
        out.extend(_digest32((key, nonce, str(counter).encode("utf-8"))))
        counter += 1
    return bytes(out[:n_bytes])


def _digest32(parts):
    data_parts = tuple(parts or ())
    if hasattr(hashlib, "sha256"):
        h = hashlib.sha256()
        for part in data_parts:
            h.update(part)
        return h.digest()
    if hasattr(hashlib, "sha1"):
        h1 = hashlib.sha1()
        for part in data_parts:
            h1.update(part)
        d1 = h1.digest()
        h2 = hashlib.sha1()
        for part in data_parts:
            h2.update(part)
        h2.update(b"\x01")
        d2 = h2.digest()
        return (d1 + d2)[:32]
    data = b"".join(data_parts)
    mask = 0xFFFFFFFFFFFFFFFF
    state = 1469598103934665603
    for byte in data:
        state ^= byte
        state = (state * 1099511628211) & mask
    out = bytearray()
    for index in range(4):
        z = (state + (index + 1) * 0x9E3779B97F4A7C15) & mask
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z = z ^ (z >> 31)
        out.extend(z.to_bytes(8, "big"))
    return bytes(out)


def _b64decode_bytes(text):
    raw = str(text or "").encode("ascii")
    return binascii.a2b_base64(raw)


def _b64encode_bytes(raw):
    return binascii.b2a_base64(bytes(raw), newline=False).decode("ascii")
