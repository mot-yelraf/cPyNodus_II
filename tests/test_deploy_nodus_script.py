"""Tests for deploy-time preservation and OTA trust-key provisioning.

The cases inspect deployment script behavior so operator configuration and
authentication material are handled by the documented workflow.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_deploy_script_preserves_device_ota_public_key():
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "deploy_nodus.sh"
    ).read_text(encoding="utf-8")

    assert '--exclude="/ota-public-key.json"' in script
    assert "--ota-public-key PATH" in script


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")
def test_deploy_script_provisions_ota_public_key(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "deploy_nodus.sh"
    target = tmp_path / "target"
    target.mkdir()
    public_key = tmp_path / "trusted-key.json"
    public_key.write_text('{"key_id":"test-key"}\n', encoding="utf-8")

    result = subprocess.run(
        (
            str(script),
            "--target",
            str(target),
            "--mode",
            "staging",
            "--content",
            "runtime",
            "--ota-public-key",
            str(public_key),
        ),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (target / "ota-public-key.json").read_text(
        encoding="utf-8"
    ) == public_key.read_text(encoding="utf-8")
    assert (target / "safemode.py").is_file()
