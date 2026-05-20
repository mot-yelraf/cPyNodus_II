"""Tests for CircuitPython auto-reload policy helpers."""

from types import SimpleNamespace

from cpynodus_ii.core.autoreload import disable_auto_reload


def test_disable_auto_reload_sets_runtime_flag():
    runtime = SimpleNamespace(autoreload=True)
    supervisor = SimpleNamespace(runtime=runtime)

    assert disable_auto_reload(supervisor) is True
    assert runtime.autoreload is False


def test_disable_auto_reload_uses_legacy_helper_when_runtime_missing():
    calls = []

    def _disable():
        calls.append(True)

    supervisor = SimpleNamespace(disable_autoreload=_disable)

    assert disable_auto_reload(supervisor) is True
    assert calls == [True]


def test_disable_auto_reload_reports_unavailable_without_supervisor():
    assert disable_auto_reload(SimpleNamespace()) is False
