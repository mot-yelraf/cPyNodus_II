"""Persist calibration command updates to sensor TOML documents.

``persist_calibration_updates`` preserves unrelated settings while replacing
or adding supported calibration keys. Writes use temporary and backup files so
an interrupted update does not directly overwrite the active document.
"""


def persist_calibration_updates(runtime_config, updates, *, settings_root):
    """Persist calibration updates to the active sensor TOML file."""
    filename = str(runtime_config.sensor.active_config_file or "").strip()
    if not filename:
        filename = "sensor_soil.toml" if runtime_config.sensor.family == "soil" else ""
    if not filename:
        filename = "sensor_i2c.toml"
    return _write_calibration_file(
        _join_settings_path(settings_root, filename),
        updates,
    )


def _write_calibration_file(path, updates):
    import os

    targets = _calibration_patch_targets(updates)
    if not targets:
        return ("calibration_key_missing",)

    found = [False] * len(targets)
    current_section = ""
    seen_sections = []
    tmp_path = "{}.tmp".format(path)
    backup_path = "{}.bak".format(path)
    try:
        with open(path, "r", encoding="utf-8") as source:
            with open(tmp_path, "w", encoding="utf-8") as target:
                while True:
                    raw_line = source.readline()
                    if raw_line == "":
                        break
                    stripped = str(raw_line or "").strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        _append_missing_targets_for_section(
                            target,
                            current_section,
                            targets,
                            found,
                        )
                        current_section = stripped[1:-1].strip()
                        if current_section not in seen_sections:
                            seen_sections.append(current_section)
                        target.write(raw_line)
                        continue
                    key = _toml_line_key(raw_line)
                    replacement = ""
                    if key:
                        replacement = _calibration_replacement_line(
                            current_section,
                            key,
                            raw_line,
                            targets,
                            found,
                        )
                    target.write(replacement if replacement else raw_line)
                _append_missing_targets_for_section(
                    target,
                    current_section,
                    targets,
                    found,
                )
                _append_missing_sections(target, targets, found, seen_sections)
                try:
                    target.flush()
                except AttributeError:
                    pass
        if not _all_found(found):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return ("calibration_key_missing",)
        if _path_size(tmp_path) <= 0:
            return ("toml_write_empty_tmp",)
        if _path_exists(backup_path):
            os.remove(backup_path)
        if _path_exists(path):
            os.rename(path, backup_path)
        os.rename(tmp_path, path)
    except OSError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return (_persistence_error(exc),)
    return ()


def _calibration_patch_targets(updates):
    targets = []
    seen = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        target = (section, key)
        if target in seen:
            continue
        seen.append(target)
        targets.append((section, key, _format_toml_scalar(update.get("value"))))
    return tuple(targets)


def _calibration_replacement_line(section, key, raw_line, targets, found):
    for index, target in enumerate(targets):
        if found[index]:
            continue
        if section == target[0] and key == target[1]:
            found[index] = True
            return "{} = {}{}".format(key, target[2], _line_ending(raw_line))
    return ""


def _append_missing_targets_for_section(handle, section, targets, found):
    if not section:
        return
    for index, target in enumerate(targets):
        if found[index] or section != target[0]:
            continue
        handle.write("{} = {}\n".format(target[1], target[2]))
        found[index] = True


def _append_missing_sections(handle, targets, found, seen_sections):
    active_section = ""
    for index, target in enumerate(targets):
        if found[index] or target[0] in seen_sections:
            continue
        if target[0] != active_section:
            handle.write("\n[{}]\n".format(target[0]))
            active_section = target[0]
        handle.write("{} = {}\n".format(target[1], target[2]))
        found[index] = True


def _toml_line_key(raw_line):
    body = str(raw_line or "").split("#", 1)[0]
    if "=" not in body:
        return ""
    return body.split("=", 1)[0].strip()


def _line_ending(raw_line):
    text = str(raw_line or "")
    if text.endswith("\r\n"):
        return "\r\n"
    if text.endswith("\n"):
        return "\n"
    return ""


def _all_found(found):
    for item in found:
        if not item:
            return False
    return True


def _format_toml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        text = repr(float(value))
        if "." not in text and "e" not in text.lower():
            text += ".0"
        return text
    text = str(value or "")
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))


def _join_settings_path(root, name):
    root_text = str(root or ".")
    if not root_text or root_text == ".":
        return str(name or "")
    if root_text.endswith("/"):
        return "{}{}".format(root_text, name)
    return "{}/{}".format(root_text, name)


def _path_exists(path):
    try:
        import os

        os.stat(path)
        return True
    except OSError:
        return False


def _path_size(path):
    try:
        import os

        return os.stat(path)[6]
    except OSError:
        return -1


def _persistence_error(exc):
    code = getattr(exc, "errno", None)
    if code in {30} or "read-only" in str(exc).lower():
        return "read_only_filesystem"
    return "persistence_failed"
