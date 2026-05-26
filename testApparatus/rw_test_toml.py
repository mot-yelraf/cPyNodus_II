"""Standalone TOML read/write probe for CircuitPython Nodus devices.

Copy this file to the CIRCUITPY root and run it from the CircuitPython REPL
while the normal app is stopped:

    import rw_test_toml
    rw_test_toml.run()

When run from the repository checkout for host smoke checks:

    from testApparatus import rw_test_toml
    rw_test_toml.run(root="/tmp/nodus-rw-test", cleanup=True)

The test writes only `test.toml`, `test.toml.tmp`, and `test.toml.bak` by
default in the selected root. On CIRCUITPY, the default root is `"."`, so the
files are created at the filesystem root. It uses the firmware Settings
read/write helpers so the diagnostic matches the app's temp-file, backup, and
rename persistence path.
"""

import os
import time

RW_TEST_TOML_VERSION = "v0.26.146.2"
DEFAULT_FILENAME = "test.toml"


def run(*, root=".", filename=DEFAULT_FILENAME, rounds=3, cleanup=False):
    """Write, read, patch, and verify a test TOML file on the device FS."""
    started = _monotonic()
    root = str(root or ".")
    filename = str(filename or DEFAULT_FILENAME)
    path = _join_root(root, filename)
    backup_path = "{}.bak".format(path)
    tmp_path = "{}.tmp".format(path)
    _log(
        "rw_toml phase=start version={} path={} rounds={}".format(
            RW_TEST_TOML_VERSION,
            path,
            int(rounds or 0),
        )
    )

    settings_cls, errors = _load_settings_class()
    if settings_cls is None:
        _log("rw_toml phase=done result=fail errors={}".format(_join_errors(errors)))
        return False

    writable = _filesystem_writable(settings_cls, root)
    _log("fs phase=check mode={}".format(_fs_mode_label(writable)))
    if writable is not True:
        _log("rw_toml phase=done result=fail step=rwfs errors=filesystem_not_rw")
        return False

    _remove_if_exists(tmp_path)
    if cleanup:
        _remove_if_exists(path)
        _remove_if_exists(backup_path)

    try:
        initial_document = _document(0, "initial", "app_write")
        _write_toml(settings_cls, path, initial_document)
        _verify_document(settings_cls, path, 0, "initial", "app_write")
        _log_backup_state(backup_path, "after_initial")

        count = max(0, int(rounds or 0))
        for round_index in range(1, count + 1):
            label = "round_{}".format(round_index)
            mode = "app_write"
            before_text = _read_text(path)
            _write_toml(settings_cls, path, _document(round_index, label, mode))
            _verify_document(settings_cls, path, round_index, label, mode)
            if _read_text(backup_path) != before_text:
                raise RuntimeError("backup_mismatch_round_{}".format(round_index))
            _log(
                "rw_toml phase=round_ok round={} size={} backup_size={}".format(
                    round_index,
                    _path_size(path),
                    _path_size(backup_path),
                )
            )

        patcher = getattr(settings_cls, "_try_patch_toml_scalar_file", None)
        if callable(patcher):
            before_text = _read_text(path)
            update = {
                "section": "Probe",
                "key": "LABEL",
                "value": "scalar_patch",
            }
            patched = patcher(path, filename, (update,))
            if not patched:
                raise RuntimeError("scalar_patch_rejected")
            _verify_document(settings_cls, path, count, "scalar_patch", "app_write")
            if _read_text(backup_path) != before_text:
                raise RuntimeError("backup_mismatch_scalar_patch")
            _log(
                "rw_toml phase=scalar_patch_ok size={} backup_size={}".format(
                    _path_size(path),
                    _path_size(backup_path),
                )
            )
        else:
            _log("rw_toml phase=scalar_patch_skipped reason=helper_missing")

        if cleanup:
            _remove_if_exists(path)
            _remove_if_exists(backup_path)
            _remove_if_exists(tmp_path)
            _log("rw_toml cleanup=done")

        _log(
            "rw_toml phase=done result=pass elapsed_s={:.1f}".format(
                _monotonic() - started,
            )
        )
        return True
    except Exception as exc:
        _log(
            "rw_toml phase=done result=fail type={} error={}".format(
                type(exc).__name__,
                exc,
            )
        )
        _log(
            "rw_toml artifacts path_size={} backup_size={} tmp_size={}".format(
                _path_size(path),
                _path_size(backup_path),
                _path_size(tmp_path),
            )
        )
        return False


def _load_settings_class():
    try:
        from cpynodus_ii.core.settings import Settings

        return Settings, ()
    except Exception as exc:
        return None, ("settings_import_failed:{}".format(exc),)


def _filesystem_writable(settings_cls, root):
    try:
        writable = settings_cls.filesystem_writable(root)
        if writable is not None:
            return bool(writable)
    except Exception as exc:
        _log("fs phase=storage_check_error error={}".format(exc))

    probe_path = _join_root(root, ".rw_test_toml_probe.tmp")
    try:
        with open(probe_path, "w") as handle:
            handle.write("1")
            try:
                handle.flush()
            except AttributeError:
                pass
        _remove_if_exists(probe_path)
        return True
    except OSError as exc:
        _log("fs phase=probe_write_error error={}".format(exc))
        return False


def _write_toml(settings_cls, path, document):
    writer = getattr(settings_cls, "_write_toml_file", None)
    if not callable(writer):
        raise RuntimeError("settings_write_helper_missing")
    writer(path, document)
    if _path_size(path) <= 0:
        raise RuntimeError("write_empty_file")


def _verify_document(settings_cls, path, expected_round, expected_label, expected_mode):
    reader = getattr(settings_cls, "_read_toml_file", None)
    if not callable(reader):
        raise RuntimeError("settings_read_helper_missing")
    document = reader(path)
    probe = document.get("Probe", {})
    stats = document.get("Stats", {})
    if probe.get("LABEL", "") != expected_label:
        raise RuntimeError("label_mismatch")
    if probe.get("MODE", "") != expected_mode:
        raise RuntimeError("mode_mismatch")
    if _int_value(probe.get("ROUND", -1), -1) != int(expected_round):
        raise RuntimeError("round_mismatch")
    if stats.get("OK", None) is not True:
        raise RuntimeError("ok_flag_mismatch")
    if _int_value(stats.get("COUNT", -1), -1) != int(expected_round + 1):
        raise RuntimeError("count_mismatch")


def _document(round_index, label, mode):
    now = int(_monotonic())
    return {
        "Probe": {
            "SCHEMA": "nodus-rw-test/v1",
            "LABEL": str(label or ""),
            "MODE": str(mode or ""),
            "ROUND": int(round_index or 0),
            "UPDATED_MONOTONIC": now,
        },
        "Stats": {
            "OK": True,
            "COUNT": int(round_index or 0) + 1,
        },
    }


def _log_backup_state(path, phase):
    size = _path_size(path)
    state = "present" if size > 0 else "missing"
    _log("rw_toml phase={} backup={} backup_size={}".format(phase, state, size))


def _join_root(root, path):
    root_text = str(root or ".")
    path_text = str(path or "")
    if root_text == "/":
        return "/{}".format(path_text.lstrip("/"))
    if root_text.endswith("/"):
        return "{}{}".format(root_text, path_text.lstrip("/"))
    if root_text == ".":
        return path_text
    return "{}/{}".format(root_text, path_text.lstrip("/"))


def _path_size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return -1


def _read_text(path):
    with open(path, "r") as handle:
        return handle.read()


def _remove_if_exists(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _fs_mode_label(writable):
    if writable is True:
        return "RWFS"
    if writable is False:
        return "ROFS"
    return "FS?"


def _join_errors(errors):
    return ",".join(str(error or "") for error in tuple(errors or ())) or "none"


def _int_value(value, default):
    try:
        return int(value)
    except Exception:
        return int(default)


def _monotonic():
    try:
        return time.monotonic()
    except Exception:
        return 0.0


def _log(message):
    print(str(message or ""))
