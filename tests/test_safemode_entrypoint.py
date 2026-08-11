"""Tests for bounded CircuitPython safe-mode recovery.

The cases execute the safe-mode entrypoint with faked supervisor state to pin
retry limits, diagnostic writes, and reset decisions.
"""

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

SAFEMODE_PATH = Path(__file__).resolve().parents[1] / "safemode.py"


class _ResetRequested(Exception):
    pass


def _module(name, **values):
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _run_safemode(monkeypatch, *, reason, hard_fault, attempts=0):
    nvm = bytearray((0, 0, 0, 0, attempts))
    state = {"resets": 0}

    def _reset():
        state["resets"] += 1
        raise _ResetRequested

    monkeypatch.setitem(
        sys.modules,
        "microcontroller",
        _module("microcontroller", nvm=nvm, reset=_reset),
    )
    monkeypatch.setitem(
        sys.modules,
        "supervisor",
        _module(
            "supervisor",
            runtime=SimpleNamespace(safe_mode_reason=reason),
            SafeModeReason=SimpleNamespace(HARD_FAULT=hard_fault),
        ),
    )
    try:
        runpy.run_path(str(SAFEMODE_PATH))
    except _ResetRequested:
        pass
    return nvm, state


def test_hard_fault_safe_mode_requests_bounded_reset(monkeypatch):
    hard_fault = object()
    nvm, state = _run_safemode(
        monkeypatch,
        reason=hard_fault,
        hard_fault=hard_fault,
        attempts=0,
    )

    assert state["resets"] == 1
    assert nvm[4] == 1


def test_hard_fault_safe_mode_stops_after_two_attempts(monkeypatch):
    hard_fault = object()
    nvm, state = _run_safemode(
        monkeypatch,
        reason=hard_fault,
        hard_fault=hard_fault,
        attempts=2,
    )

    assert state["resets"] == 0
    assert nvm[4] == 2


def test_non_hard_fault_safe_mode_remains_stopped(monkeypatch):
    hard_fault = object()
    nvm, state = _run_safemode(
        monkeypatch,
        reason=object(),
        hard_fault=hard_fault,
        attempts=0,
    )

    assert state["resets"] == 0
    assert nvm[4] == 0
