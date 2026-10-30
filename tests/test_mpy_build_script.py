"""Verify MCU and CircuitPython selection in host build/deployment tooling.

Temporary repositories and fake compilers exercise artifact isolation, metadata
rejection, library staging and automatic rebuilds without accessing a device.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LIB_ENTRIES = (
    "adafruit_minimqtt/__init__.mpy",
    "adafruit_scd30.mpy",
    "adafruit_connection_manager.mpy",
    "adafruit_ahtx0.mpy",
    "adafruit_bme280/__init__.mpy",
    "adafruit_ticks.mpy",
    "adafruit_scd4x.mpy",
    "adafruit_ntp.mpy",
    "adafruit_httpserver/__init__.mpy",
    "adafruit_register/__init__.mpy",
    "adafruit_veml7700.mpy",
    "asyncio/__init__.mpy",
    "adafruit_bme680.mpy",
)


@pytest.fixture
def build_repo(tmp_path):
    """Create a small repository with isolated compiler and library fixtures."""
    repo = tmp_path / "project"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    for name in ("nodus_mpy.sh", "deploy_nodus.sh"):
        shutil.copy2(source / "scripts" / name, scripts / name)
    package = repo / "cpynodus_ii"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "v0.26.303.1"\n')
    (package / "sample.py").write_text('"""Example build input."""\n')
    (repo / "code.py").write_text('"""Example entrypoint."""\n')
    roots = (
        "circuitPython_9.2.8/libs/adafruit-circuitpython-bundle-9.x-mpy-20250314",
        "circuitPython_10.x.x/adafruit-circuitpython-bundle-10.x-mpy-20260606",
        "circuitPython_10.x.x/adafruit-circuitpython-bundle-10.x-mpy-20261030",
    )
    for root in roots:
        for entry in LIB_ENTRIES:
            library = tmp_path / "mcu_libs" / root / "lib" / entry
            library.parent.mkdir(parents=True, exist_ok=True)
            library.write_bytes(b"C\x06\x00\x1f" + root.encode())
    return repo


def make_compiler(repo, version, abi="6.3"):
    """Write a fake compiler with a selectable version and ABI banner."""
    compiler = repo / ("compiler-" + version)
    banner = "CircuitPython {} on 2026-09-14; mpy-cross emitting mpy v{}".format(
        version, abi
    )
    compiler.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        f"    print({banner!r})\n"
        "else:\n"
        "    output = Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "    output.write_bytes(b'C\\x06\\x00\\x1f__version__\\nv0.26.303.1\\n')\n"
    )
    compiler.chmod(0o755)
    return compiler


def run_script(repo, name, *args, compiler=None):
    """Run one script without inheriting machine-specific compiler overrides."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MPY_CROSS")
    }
    if compiler:
        env["MPY_CROSS"] = str(compiler)
    return subprocess.run(
        [str(repo / "scripts" / name), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "target,default", [("pico2w", "9.2.8"), ("xesp32s3", "10.2.1")]
)
@pytest.mark.parametrize("explicit", [False, True])
def test_build_selects_version_compiler_and_libraries(
    build_repo, target, default, explicit
):
    version = "10.3.1" if explicit else default
    compiler = make_compiler(build_repo, version)
    args = ["--target", target, "--compiler", str(compiler)]
    root = build_repo / "build" / "firmware" / target
    if explicit:
        args += ["--circuitpython-version", version]
        root /= version
    result = run_script(build_repo, "nodus_mpy.sh", *args)
    assert result.returncode == 0, result.stderr
    info = dict(
        line.split("=", 1) for line in (root / "BUILD_INFO").read_text().splitlines()
    )
    assert info["circuitpython"] == version
    assert info["target"] == target
    assert info["compiler"] == str(compiler)
    assert info["stage_libs"] == "1"
    assert set(p.name for p in (root / "cpynodus_ii").iterdir()) == {
        "__init__.mpy",
        "sample.mpy",
    }
    if explicit:
        assert "20261030/lib" in info["lib_source"]
    for entry in LIB_ENTRIES:
        assert (root / "lib" / entry).read_bytes() == (
            Path(info["lib_source"]) / entry
        ).read_bytes()


def test_clean_1031_build_preserves_default_artifacts(build_repo):
    default = build_repo / "build/firmware/pico2w"
    default.mkdir(parents=True)
    (default / "BUILD_INFO").write_text("baseline\n")
    selected = default / "10.3.1"
    selected.mkdir()
    (selected / "stale.mpy").write_bytes(b"stale")
    compiler = make_compiler(build_repo, "10.3.1")
    result = run_script(
        build_repo,
        "nodus_mpy.sh",
        "--target",
        "pico2w",
        "--circuitpython-version",
        "10.3.1",
        "--clean",
        compiler=compiler,
    )
    assert result.returncode == 0, result.stderr
    assert (default / "BUILD_INFO").read_text() == "baseline\n"
    assert not (selected / "stale.mpy").exists()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")
@pytest.mark.parametrize("explicit_compiler", [False, True])
def test_existing_dirty_928_compiler_builds_and_deploys(build_repo, explicit_compiler):
    compiler = make_compiler(build_repo, "9.2.8-dirty")
    discovered = build_repo.parent / "mcu_libs/mpy-cross/mpy-cross"
    discovered.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(compiler, discovered)
    args = ["--target", "pico2w", "--circuitpython-version", "9.2.8"]
    if explicit_compiler:
        args += ["--compiler", str(compiler)]
    built = run_script(build_repo, "nodus_mpy.sh", *args)
    assert built.returncode == 0, built.stderr
    info = build_repo / "build/firmware/pico2w/BUILD_INFO"
    assert "circuitpython=9.2.8\n" in info.read_text()
    assert "CircuitPython 9.2.8-dirty on " in info.read_text()
    deployed = run_script(
        build_repo,
        "deploy_nodus.sh",
        "--target",
        str(build_repo / "destination"),
        "--mode",
        "staging",
        "--content",
        "pico2w-mpy",
        "--circuitpython-version",
        "9.2.8",
    )
    assert deployed.returncode == 0, deployed.stderr
    assert "Running scripts/nodus_mpy.sh" not in deployed.stdout
    assert (build_repo / "destination/cpynodus_ii/sample.mpy").exists()


@pytest.mark.parametrize(
    "version,abi", [("10.2.1", "6.3"), ("10.3.10", "6.3"), ("10.3.1", "5")]
)
def test_build_rejects_wrong_compiler_before_writing(build_repo, version, abi):
    compiler = make_compiler(build_repo, version, abi)
    result = run_script(
        build_repo,
        "nodus_mpy.sh",
        "--target",
        "pico2w",
        "--circuitpython-version",
        "10.3.1",
        "--compiler",
        str(compiler),
    )
    assert result.returncode != 0
    assert "MPY compiler must" in result.stderr
    assert not (build_repo / "build").exists()


@pytest.mark.parametrize(
    "target,version",
    [("pico2w", "10.2.1"), ("xesp32s3", "9.2.8"), ("pico2w", "11.0.0")],
)
def test_build_rejects_unsupported_runtime(build_repo, target, version):
    result = run_script(
        build_repo,
        "nodus_mpy.sh",
        "--target",
        target,
        "--circuitpython-version",
        version,
    )
    assert result.returncode == 2
    assert "Unsupported CircuitPython" in result.stderr


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")
@pytest.mark.parametrize("target", ["pico2w", "xesp32s3"])
def test_deploy_1031_builds_and_stages_selected_version(build_repo, target):
    compiler = make_compiler(build_repo, "10.3.1")
    destination = build_repo / "destination"
    destination.mkdir()
    (destination / "settings.toml").write_text("preserve settings\n")
    package = destination / "cpynodus_ii"
    package.mkdir()
    (package / "sample.py").write_text("old source")
    result = run_script(
        build_repo,
        "deploy_nodus.sh",
        "--target",
        str(destination),
        "--mode",
        "staging",
        "--content",
        target + "-mpy",
        "--circuitpython-version",
        "10.3.1",
        compiler=compiler,
    )
    assert result.returncode == 0, result.stderr
    assert "--circuitpython-version 10.3.1" in result.stdout
    assert (package / "sample.mpy").exists()
    assert not (package / "sample.py").exists()
    assert (destination / "settings.toml").read_text() == "preserve settings\n"
    info = build_repo / "build/firmware" / target / "10.3.1/BUILD_INFO"
    assert "circuitpython=10.3.1\n" in info.read_text()
    assert "20261030" in (destination / "lib/adafruit_ntp.mpy").read_bytes().decode()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")
def test_deploy_dry_run_does_not_build_or_copy(build_repo):
    destination = build_repo / "destination"
    destination.mkdir()
    result = run_script(
        build_repo,
        "deploy_nodus.sh",
        "--target",
        str(destination),
        "--mode",
        "staging",
        "--content",
        "pico2w-mpy",
        "--circuitpython-version",
        "10.3.1",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert "--circuitpython-version 10.3.1" in result.stdout
    assert not (build_repo / "build").exists()
    assert list(destination.iterdir()) == []


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")
@pytest.mark.parametrize(
    "key,value",
    [
        ("target", "xesp32s3"),
        ("circuitpython", "9.2.8"),
        ("compiler_version", "CircuitPython 10.3.10 on test; mpy v6.3"),
        ("mpy_abi", "mpy v5"),
        ("stage_libs", "0"),
    ],
)
def test_deploy_rejects_mismatched_metadata_before_copy(build_repo, key, value):
    compiler = make_compiler(build_repo, "10.3.1")
    built = run_script(
        build_repo,
        "nodus_mpy.sh",
        "--target",
        "pico2w",
        "--circuitpython-version",
        "10.3.1",
        compiler=compiler,
    )
    assert built.returncode == 0, built.stderr
    info = build_repo / "build/firmware/pico2w/10.3.1/BUILD_INFO"
    lines = info.read_text().splitlines()
    info.write_text(
        "\n".join(
            key + "=" + value if line.startswith(key + "=") else line for line in lines
        )
        + "\n"
    )
    # A mismatched build must trigger a rebuild, and a wrong compiler must
    # stop that rebuild before staged files can reach the destination.
    wrong = make_compiler(build_repo, "10.2.1")
    destination = build_repo / "destination"
    destination.mkdir()
    result = run_script(
        build_repo,
        "deploy_nodus.sh",
        "--target",
        str(destination),
        "--mode",
        "staging",
        "--content",
        "pico2w-mpy",
        "--circuitpython-version",
        "10.3.1",
        compiler=wrong,
    )
    assert result.returncode != 0
    assert "--circuitpython-version 10.3.1" in result.stdout
    assert "Could not find a CircuitPython 10.3.1 MPY compiler" in result.stderr
    assert list(destination.iterdir()) == []


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")
def test_default_deploy_ignores_sibling_1031_build(build_repo):
    for version_args, version in (
        ([], "9.2.8"),
        (["--circuitpython-version", "10.3.1"], "10.3.1"),
    ):
        built = run_script(
            build_repo,
            "nodus_mpy.sh",
            "--target",
            "pico2w",
            *version_args,
            compiler=make_compiler(build_repo, version),
        )
        assert built.returncode == 0, built.stderr
    destination = build_repo / "destination"
    result = run_script(
        build_repo,
        "deploy_nodus.sh",
        "--target",
        str(destination),
        "--mode",
        "staging",
        "--content",
        "pico2w-mpy",
    )
    assert result.returncode == 0, result.stderr
    assert "Running scripts/nodus_mpy.sh" not in result.stdout
    library = (destination / "lib/adafruit_ntp.mpy").read_bytes()
    assert b"bundle-9.x-mpy-20250314" in library


def test_clean_default_preserves_1031_artifacts(build_repo):
    selected = build_repo / "build/firmware/pico2w/10.3.1"
    selected.mkdir(parents=True)
    (selected / "BUILD_INFO").write_text("10.3.1 baseline\n")
    result = run_script(
        build_repo,
        "nodus_mpy.sh",
        "--target",
        "pico2w",
        "--clean",
        compiler=make_compiler(build_repo, "9.2.8"),
    )
    assert result.returncode == 0, result.stderr
    assert (selected / "BUILD_INFO").read_text() == "10.3.1 baseline\n"


@pytest.mark.parametrize(
    "content,version",
    [("runtime", "10.3.1"), ("pico2w-mpy", "10.2.1"), ("xesp32s3-mpy", "9.2.8")],
)
def test_deploy_rejects_invalid_version_selection(build_repo, content, version):
    result = run_script(
        build_repo,
        "deploy_nodus.sh",
        "--target",
        str(build_repo / "destination"),
        "--mode",
        "staging",
        "--content",
        content,
        "--circuitpython-version",
        version,
    )
    assert result.returncode == 2
    assert not (build_repo / "destination").exists()
