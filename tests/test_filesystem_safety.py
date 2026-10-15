"""Exercise interrupted file transactions and shallow firmware persistence.

Faults run against temporary host files, including simulated power loss that
bypasses normal exception cleanup. They do not emulate flash-sector failures.
"""

import builtins
import hashlib
import os
import sys
from types import SimpleNamespace

import pytest

from cpynodus_ii.core.filesystem import clear_file, commit_file
from cpynodus_ii.core.reboot_log import _trim_reboot_log
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features import (
    calibration_offset_persistence,
    calibration_persistence,
    device_config,
    display_config,
    onboarding_state,
    scalar_persistence,
    switch_label_config,
    switch_location_config,
    time_config,
    web_services,
)
from cpynodus_ii.ota import http as ota_http
from cpynodus_ii.ota.state import (
    FwUpdateState,
    clear_ota_state,
    load_ota_state,
    save_ota_state,
)


def test_failed_install_restores_original(tmp_path, monkeypatch):
    live = tmp_path / "settings.toml"
    tmp = tmp_path / "settings.toml.tmp"
    backup = tmp_path / "settings.toml.bak"
    live.write_text("original")
    tmp.write_text("replacement")
    rename = os.rename

    def fail_install(src, dst):
        if str(src) == str(tmp):
            raise OSError(5, "injected install failure")
        rename(src, dst)

    monkeypatch.setattr(os, "rename", fail_install)
    with pytest.raises(OSError):
        commit_file(live, tmp, backup)
    assert live.read_text() == "original"
    assert tmp.read_text() == "replacement"


def test_sync_failure_does_not_rotate_original(tmp_path, monkeypatch):
    live = tmp_path / "config"
    tmp = tmp_path / "config.tmp"
    backup = tmp_path / "config.bak"
    live.write_text("original")
    tmp.write_text("replacement")

    def fail_sync():
        raise OSError(5, "sync failed")

    monkeypatch.setattr(os, "sync", fail_sync)
    with pytest.raises(OSError):
        commit_file(live, tmp, backup)
    assert live.read_text() == "original"
    assert not backup.exists()


def test_empty_live_does_not_destroy_retained_backup(tmp_path):
    live = tmp_path / "config"
    tmp = tmp_path / "config.tmp"
    backup = tmp_path / "config.bak"
    live.write_text("")
    backup.write_text("original")
    tmp.write_text("replacement")
    commit_file(live, tmp, backup)
    assert live.read_text() == "replacement"
    assert backup.read_text() == "original"


@pytest.mark.parametrize("empty", [False, True])
def test_bootstrap_recovers_before_defaults_or_detection(tmp_path, monkeypatch, empty):
    documents = {
        "settings.toml": '[Profile]\nACTIVE_PROFILE = "sensorius"\n',
        "sensor_i2c.toml": '[Sensor]\nDEVICE = "co2"\n',
        "sensor_soil.toml": '[Sensor]\nDEVICE = "soil"\n',
        "switch.toml": '[Switch]\nDEVICE_ID = "switch-saved"\n',
    }
    for name, text in documents.items():
        (tmp_path / (name + ".bak")).write_text(text)
        if empty:
            (tmp_path / name).write_text("")
    monkeypatch.setattr(
        Settings, "_ensure_seeded_ids_and_hostname", lambda *a, **k: None
    )

    def fail_detect(*args, **kwargs):
        pytest.fail("recovery was mistaken for a factory device")

    config = Settings.bootstrap_factory_defaults(tmp_path, detect_fn=fail_detect)
    assert config.active_profile == "sensorius"
    for name, text in documents.items():
        assert (tmp_path / name).read_text() == text


def test_read_only_startup_can_load_missing_settings_from_backup(tmp_path, monkeypatch):
    (tmp_path / "settings.toml.bak").write_text(
        '[Profile]\nACTIVE_PROFILE = "sensorius"\n'
    )
    monkeypatch.chdir(tmp_path)
    assert (
        Settings.from_working_directory().runtime_config().active_profile == "sensorius"
    )
    assert not (tmp_path / "settings.toml").exists()


@pytest.mark.parametrize("kind", ["onboarding", "ota"])
def test_json_recovers_interrupted_rotation_and_clear_removes_retained_state(
    tmp_path, monkeypatch, kind
):
    if kind == "onboarding":
        path = tmp_path / "onboarding_state.json"

        def save():
            return onboarding_state.save_onboarding_state(tmp_path, {"token": "old"})

        def load():
            return onboarding_state.load_onboarding_state(tmp_path)

        def clear():
            return onboarding_state.clear_onboarding_state(tmp_path)
    else:
        path = tmp_path / "state.json"

        def save():
            return save_ota_state(FwUpdateState(package_id="old"), str(path))

        def load():
            return load_ota_state(str(path))

        def clear():
            return clear_ota_state(str(path))

    save()
    rename = os.rename

    def power_cut(src, dst):
        if str(src).endswith(".tmp"):
            raise SystemExit("power cut after backup rotation")
        rename(src, dst)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "rename", power_cut)
        with pytest.raises(SystemExit):
            save()
    assert not path.exists()
    assert load() is not None
    if kind == "onboarding":
        assert load() == {"token": "old"}
    else:
        assert load().package_id == "old"
    assert clear()
    assert not path.exists()
    assert not (tmp_path / (path.name + ".bak")).exists()
    assert not (tmp_path / (path.name + ".tmp")).exists()
    assert not load()


@pytest.mark.parametrize("kind", ["device", "display", "time", "location", "settings"])
def test_all_onboarding_clear_paths_remove_backups(tmp_path, kind):
    onboarding_state.save_onboarding_state(tmp_path, {"onboard_token": "old"})
    onboarding_state.save_onboarding_state(tmp_path, {"onboard_token": "new"})
    functions = {
        "device": device_config._clear_token,
        "display": display_config._clear_onboarding_state_fast,
        "time": time_config._clear_onboarding_state_fast,
        "location": switch_location_config._clear_onboarding_state_fast,
        "settings": Settings.clear_onboarding_state,
    }
    functions[kind](tmp_path)
    assert onboarding_state.load_onboarding_state(tmp_path) == {}


def _writer_case(kind, path):
    section, key = {
        "scalar": ("Network", "SSID"),
        "time": ("Time", "TZ_NAME"),
        "display": ("Display", "METRIC_1"),
        "location": ("Sensor", "LOCATION"),
        "label": ("Switch", "SWITCH_1_LABEL"),
        "calibration": ("Calibration.Device", "TEMP_OFFSET"),
        "offset": ("Calibration.Device", "TEMP_OFFSET"),
        "web": ("Network", "SSID"),
    }[kind]
    path.write_text('[{}]\n{} = "old"\n'.format(section, key))
    updates = ({"section": section, "key": key, "value": "new"},)
    if kind == "scalar":
        return lambda: scalar_persistence.write_toml_scalar(path, section, key, "new")
    if kind == "offset":
        config = SimpleNamespace(sensor=SimpleNamespace(active_config_file=path.name))
        return lambda: calibration_offset_persistence.persist_single_calibration_offset(
            config, updates[0], settings_root=path.parent
        )
    if kind == "web":
        return lambda: web_services._rewrite_itaot_settings_file(
            str(path), updates, ((section, key, '"new"'),), None
        )
    writer = {
        "time": time_config._write_time_file,
        "display": display_config._write_display_file,
        "location": switch_location_config._write_location_file,
        "label": switch_label_config._write_switch_label_file,
        "calibration": calibration_persistence._write_calibration_file,
    }[kind]
    return lambda: writer(path, updates)


@pytest.mark.parametrize(
    "kind",
    ["scalar", "time", "display", "location", "label", "calibration", "offset", "web"],
)
@pytest.mark.parametrize("failure", [MemoryError, RuntimeError])
def test_writers_close_both_files_on_non_oserror(tmp_path, monkeypatch, kind, failure):
    path = tmp_path / "settings.toml"
    writer = _writer_case(kind, path)
    original = path.read_bytes()
    opened = []
    real_open = builtins.open

    class FailingFile:
        def __init__(self, handle, mode):
            self.handle = handle
            self.mode = mode

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def write(self, text):
            raise failure("injected write failure")

    def failing_open(name, mode="r", *args, **kwargs):
        handle = real_open(name, mode, *args, **kwargs)
        opened.append(handle)
        return FailingFile(handle, mode)

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "open", failing_open)
        with pytest.raises(failure):
            writer()
    assert len(opened) == 2
    assert all(handle.closed for handle in opened)
    assert path.read_bytes() == original


def test_scalar_persistence_uses_at_most_two_firmware_frames(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[Network]\nSSID = "old"\n')
    depths = []

    def profile(frame, event, arg):
        if event != "call" or "/cpynodus_ii/" not in frame.f_code.co_filename:
            return
        depth = 0
        while frame is not None:
            if "/cpynodus_ii/" in frame.f_code.co_filename:
                depth += 1
            frame = frame.f_back
        depths.append(depth)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        assert (
            scalar_persistence.write_toml_scalar(path, "Network", "SSID", "new") == ()
        )
    finally:
        sys.setprofile(previous)
    assert max(depths) <= 2


def test_log_trim_failure_preserves_live_log(tmp_path, monkeypatch):
    path = tmp_path / "_recovery.log"
    original = b"=== old ===\n" + b"x" * 500 + b"\n=== new ===\nrecent\n"
    path.write_bytes(original)
    rename = os.rename

    def fail_install(src, dst):
        if str(src).endswith(".tmp"):
            raise OSError(5, "injected rename failure")
        rename(src, dst)

    monkeypatch.setattr(os, "rename", fail_install)
    assert not _trim_reboot_log(str(path), max_bytes=80)
    assert path.read_bytes() == original


def test_clear_syncs_backup_deletion_before_live_deletion(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    backup = tmp_path / "state.json.bak"
    path.write_text("new")
    backup.write_text("old")
    checkpoints = []
    monkeypatch.setattr(
        os, "sync", lambda: checkpoints.append((path.exists(), backup.exists()))
    )
    assert clear_file(path)
    assert checkpoints[0] == (True, False)
    assert checkpoints[-1] == (False, False)


def _ota_transaction(tmp_path):
    path = tmp_path / "firmware.mpy"
    path.write_bytes(b"complete original firmware")
    staged = tmp_path / "_ota/stage/firmware.mpy"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"replacement firmware")
    manifest = {"files": [{"path": path.name}], "delete": []}
    transaction = ota_http._build_apply_transaction(tmp_path, manifest)
    return path, manifest, transaction


def test_ota_power_cut_during_backup_leaves_original_live(tmp_path, monkeypatch):
    path, manifest, transaction = _ota_transaction(tmp_path)
    original = path.read_bytes()
    copy = ota_http._copy_file

    def interrupted_copy(src, dst):
        if str(dst).endswith(".tmp"):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as handle:
                handle.write(b"partial")
            raise SystemExit("power cut during backup")
        copy(src, dst)

    with monkeypatch.context() as scoped:
        scoped.setattr(ota_http, "_copy_file", interrupted_copy)
        with pytest.raises(SystemExit):
            ota_http._apply_staged_manifest_files(tmp_path, manifest, transaction)
    assert path.read_bytes() == original
    assert ota_http._rollback_apply_transaction(tmp_path, manifest, transaction) == ""
    assert path.read_bytes() == original


def test_ota_rejects_truncated_backup_without_destroying_live_or_journal(tmp_path):
    path, manifest, transaction = _ota_transaction(tmp_path)
    original = path.read_bytes()
    backup = tmp_path / "_ota/backup/firmware.mpy"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"partial")
    journal = tmp_path / "_ota/transaction.json"
    ota_http._write_json_file(str(journal), transaction)
    assert (
        ota_http._rollback_apply_transaction(tmp_path, manifest, transaction)
        == "apply_rollback_failed"
    )
    assert path.read_bytes() == original
    assert backup.read_bytes() == b"partial"
    assert journal.exists()


def test_ota_rollback_can_repeat_until_state_is_cleared(tmp_path):
    path, manifest, transaction = _ota_transaction(tmp_path)
    original = path.read_bytes()
    assert ota_http._apply_staged_manifest_files(tmp_path, manifest, transaction) == ""
    backup = tmp_path / "_ota/backup/firmware.mpy"
    for _ in range(2):
        assert (
            ota_http._rollback_apply_transaction(tmp_path, manifest, transaction) == ""
        )
        assert path.read_bytes() == original
        assert backup.read_bytes() == original
    assert transaction["originals"][path.name] == (
        len(original),
        hashlib.sha256(original).hexdigest(),
    )


def test_malformed_onboarding_json_closes_reader(tmp_path, monkeypatch):
    path = tmp_path / "onboarding_state.json"
    path.write_text("{broken")
    opened = []
    real_open = builtins.open

    def tracking_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(builtins, "open", tracking_open)
    assert device_config._validate_token(tmp_path, "token") == ""
    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.parametrize("kind", ["soft", "hard"])
@pytest.mark.parametrize("sync_failure", [False, True])
def test_reboot_syncs_before_reset_and_survives_sync_failure(
    monkeypatch, kind, sync_failure
):
    from cpynodus_ii import app

    events = []

    def sync():
        events.append("sync")
        if sync_failure:
            raise OSError(5, "injected sync failure")

    monkeypatch.setattr(os, "sync", sync)
    monkeypatch.setattr(app, "_mark_soft_reload_cleanup_requested", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "supervisor",
        SimpleNamespace(reload=lambda: events.append("reset")),
    )
    monkeypatch.setitem(
        sys.modules,
        "microcontroller",
        SimpleNamespace(reset=lambda: events.append("reset")),
    )
    if kind == "soft":
        app._soft_reboot()
    else:
        app._hard_reboot()
    assert events == ["sync", "reset"]


def test_ota_does_not_delete_recovery_artifacts_if_state_clear_fails(
    tmp_path, monkeypatch
):
    path, manifest, transaction = _ota_transaction(tmp_path)
    original = path.read_bytes()
    assert ota_http._apply_staged_manifest_files(tmp_path, manifest, transaction) == ""
    ota_http._write_json_file(str(tmp_path / "_ota/manifest.json"), manifest)
    ota_http._write_json_file(str(tmp_path / "_ota/transaction.json"), transaction)
    save_ota_state(FwUpdateState(phase="applying"), str(tmp_path / "_ota/state.json"))

    def fail_clear(path):
        raise OSError(5, "injected state clear failure")

    monkeypatch.setattr(ota_http, "clear_file", fail_clear)
    assert (
        ota_http.recover_interrupted_ota_apply(tmp_path)
        == "apply_recovery_state_clear_failed"
    )
    assert path.read_bytes() == original
    assert (tmp_path / "_ota/backup/firmware.mpy").exists()
    assert (tmp_path / "_ota/transaction.json").exists()


def test_empty_parsed_backup_is_preserved_for_operator(tmp_path):
    backup = tmp_path / "settings.toml.bak"
    backup.write_text("# incomplete configuration\n")
    with pytest.raises(ValueError, match="toml_backup_empty"):
        Settings.bootstrap_factory_defaults(tmp_path)
    assert backup.exists()
    assert not (tmp_path / "settings.toml").exists()


def test_log_trim_reads_only_bounded_tail(tmp_path, monkeypatch):
    from cpynodus_ii.core import reboot_log

    path = tmp_path / "_recovery.log"
    path.write_bytes(b"x" * 100_000 + b"\n=== recent ===\nrecent detail\n")
    real_open = builtins.open
    reads = []

    class TailReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def seek(self, position):
            return self.handle.seek(position)

        def read(self, size=-1):
            assert 0 <= size <= 128
            reads.append(size)
            return self.handle.read(size)

    def bounded_open(name, mode="r", *args, **kwargs):
        handle = real_open(name, mode, *args, **kwargs)
        return TailReader(handle) if mode == "rb" else handle

    monkeypatch.setattr(reboot_log, "open", bounded_open, raising=False)
    assert reboot_log._trim_reboot_log(str(path), max_bytes=128)
    assert reads == [128]
    assert path.stat().st_size <= 128
    assert b"recent detail" in path.read_bytes()
