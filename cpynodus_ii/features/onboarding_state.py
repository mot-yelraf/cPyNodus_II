"""Persist lightweight onboarding runtime state outside TOML config."""

import json
import os

ONBOARDING_STATE_FILE = "onboarding_state.json"


def _join_path(root, name):
    root_text = str(root or ".")
    if not root_text or root_text == ".":
        return str(name or "")
    if root_text.endswith("/"):
        return "{}{}".format(root_text, name)
    return "{}/{}".format(root_text, name)


def load_onboarding_state(root="."):
    """Load any persisted onboarding runtime state."""
    path = _join_path(root, ONBOARDING_STATE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except OSError:
        return {}
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def save_onboarding_state(root, state):
    """Persist onboarding runtime state outside TOML config files."""
    path = _join_path(root, ONBOARDING_STATE_FILE)
    payload = dict(state or {})
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    return path


def clear_onboarding_state(root="."):
    """Delete any persisted onboarding runtime state."""
    path = _join_path(root, ONBOARDING_STATE_FILE)
    try:
        os.remove(path)
    except OSError:
        return False
    return True
