"""Tests for deploy-time preservation and OTA trust-key provisioning.

The cases inspect deployment script behavior so operator configuration and
authentication material are handled by the documented workflow.
"""

import os
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


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")
@pytest.mark.parametrize("content", ["full", "runtime"])
def test_drive_deploy_compares_contents_without_rewriting_identical_files(
    tmp_path, content
):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "deploy_nodus.sh"
    shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / script.name, script)
    target = tmp_path / "CIRCUITPY-test"
    target.mkdir()
    public_key = tmp_path / "trusted-key.json"
    public_key.write_text('{"key_id":"test-key"}\n')

    source_time = 1_700_000_000
    older_time = 1_600_000_000
    for source, name, contents, target_contents, target_time in (
        (repo / "boot.py", "boot.py", "same\n", "same\n", older_time),
        (repo / "code.py", "code.py", "new!\n", "old!\n", source_time),
        (public_key, "ota-public-key.json", public_key.read_text(),
         public_key.read_text(), older_time),
    ):
        source.write_text(contents)
        os.utime(source, (source_time, source_time))
        existing = target / name
        existing.write_text(target_contents)
        os.utime(existing, (target_time, target_time))

    args = [
        str(script), "--target", str(target), "--content", content,
        "--ota-public-key", str(public_key), "--keep-reboot-log",
    ]
    preview = subprocess.run([*args, "--dry-run"], capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    transfers = [line for line in preview.stdout.splitlines() if line.startswith(">f")]
    assert len(transfers) == 1 and transfers[0].endswith(" code.py")
    assert (target / "code.py").read_text() == "old!\n"

    unchanged = [target / "boot.py", target / "ota-public-key.json"]
    before = [path.stat() for path in unchanged]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (target / "code.py").read_text() == "new!\n"
    for path, previous in zip(unchanged, before):
        current = path.stat()
        assert current.st_mtime_ns == previous.st_mtime_ns
        assert current.st_ctime_ns == previous.st_ctime_ns

    preview = subprocess.run([*args, "--dry-run"], capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert not any(line.startswith(">f") for line in preview.stdout.splitlines())
