"""Single-key TOML persistence for calibration offsets."""


def persist_single_calibration_offset(runtime_config, update, *, settings_root):
    """Persist one calibration offset to the active sensor TOML file."""
    import os

    section = str(update.get("section", "") or "").strip()
    key = str(update.get("key", "") or "").strip()
    if not (section and key):
        return ("calibration_key_missing",)
    sensor = runtime_config.sensor
    filename = str(getattr(sensor, "active_config_file", "") or "").strip()
    if not filename:
        filename = "sensor_soil.toml" if sensor.family == "soil" else "sensor_i2c.toml"
    root_text = str(settings_root or ".")
    if not root_text or root_text == ".":
        path = filename
    elif root_text.endswith("/"):
        path = "{}{}".format(root_text, filename)
    else:
        path = "{}/{}".format(root_text, filename)

    tmp_path = "{}.tmp".format(path)
    backup_path = "{}.bak".format(path)
    current_section = ""
    section_seen = False
    key_written = False
    formatted_value = _format_toml_scalar(update.get("value"))
    source = None
    target = None
    try:
        source = open(path, "r")
        target = open(tmp_path, "w")
        while True:
            raw_line = source.readline()
            if raw_line == "":
                break
            stripped = str(raw_line or "").strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if section_seen and not key_written:
                    target.write("{} = {}\n".format(key, formatted_value))
                    key_written = True
                current_section = stripped[1:-1].strip()
                if current_section == section:
                    section_seen = True
                target.write(raw_line)
                continue
            line_key = ""
            if current_section == section:
                line_body = str(raw_line or "").split("#", 1)[0]
                equal_index = line_body.find("=")
                if equal_index >= 0:
                    line_key = line_body[:equal_index].strip()
            if current_section == section and line_key == key:
                line_ending = ""
                text_line = str(raw_line or "")
                if text_line.endswith("\r\n"):
                    line_ending = "\r\n"
                elif text_line.endswith("\n"):
                    line_ending = "\n"
                target.write("{} = {}{}".format(key, formatted_value, line_ending))
                key_written = True
            else:
                target.write(raw_line)
        if section_seen and not key_written:
            target.write("{} = {}\n".format(key, formatted_value))
            key_written = True
        if not section_seen:
            target.write("\n[{}]\n{} = {}\n".format(section, key, formatted_value))
            key_written = True
        try:
            target.flush()
        except AttributeError:
            pass
        try:
            source.close()
        except AttributeError:
            pass
        source = None
        try:
            target.close()
        except AttributeError:
            pass
        target = None
        if not key_written:
            _remove_tmp(tmp_path)
            return ("calibration_key_missing",)
        try:
            os.stat(backup_path)
            os.remove(backup_path)
        except OSError:
            pass
        try:
            os.stat(path)
            os.rename(path, backup_path)
        except OSError:
            pass
        os.rename(tmp_path, path)
    except OSError as exc:
        if source is not None:
            try:
                source.close()
            except AttributeError:
                pass
        if target is not None:
            try:
                target.close()
            except AttributeError:
                pass
        _remove_tmp(tmp_path)
        return (_persistence_error(exc),)
    return ()


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


def _remove_tmp(path):
    try:
        import os

        os.remove(path)
    except OSError:
        pass


def _persistence_error(exc):
    code = getattr(exc, "errno", None)
    if code in {30} or "read-only" in str(exc).lower():
        return "read_only_filesystem"
    return "persistence_failed"
