"""Persist lightweight onboarding runtime state outside TOML config.

``load_onboarding_state``, ``save_onboarding_state``, and
``clear_onboarding_state`` manage the small reboot-spanning state document.
Writes replace a temporary file so partially written JSON is not made active.
"""

import json

from cpynodus_ii.core.filesystem import clear_file, commit_file, readable_path

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
        with open(readable_path(path), "r", encoding="utf-8") as handle:
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
    tmp_path = "{}.tmp".format(path)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    commit_file(path, tmp_path, "{}.bak".format(path))
    return path


def clear_onboarding_state(root="."):
    """Delete any persisted onboarding runtime state."""
    path = _join_path(root, ONBOARDING_STATE_FILE)
    try:
        return clear_file(path)
    except OSError:
        return False
