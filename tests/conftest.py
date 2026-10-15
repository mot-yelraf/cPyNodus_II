"""Shared pytest configuration for host-side cPyNodus_II tests.

The fixtures and import setup in this module provide a consistent CPython
environment for exercising firmware logic without CircuitPython hardware.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def host_filesystem_sync(monkeypatch):
    """Avoid syncing the entire host disk when exercising device durability calls."""
    monkeypatch.setattr(os, "sync", lambda: None, raising=False)
