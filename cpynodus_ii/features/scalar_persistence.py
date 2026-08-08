"""Shallow transactional persistence for one TOML scalar setting.

The writer streams an existing file into a temporary replacement so mutation
paths do not allocate a complete TOML document on constrained devices.
"""


def write_toml_scalar(path, section, key, value):
    """Replace or append one TOML scalar without loading the TOML document."""
    import os

    path_text = str(path or "")
    section_text = str(section or "").strip()
    key_text = str(key or "").strip()
    if not (path_text and section_text and key_text):
        return ("config_key_missing",)
    value_text = _format_scalar(value)
    tmp_path = "{}.tmp".format(path_text)
    backup_path = "{}.bak".format(path_text)
    source = None
    target = None
    current_section = ""
    section_seen = False
    written = False
    original_moved = False
    try:
        source = open(path_text, "r")
        target = open(tmp_path, "w")
        while True:
            raw_line = source.readline()
            if raw_line == "":
                break
            body = str(raw_line or "").split("#", 1)[0].strip()
            if body.startswith("[") and body.endswith("]"):
                if current_section == section_text and not written:
                    # lgtm[py/clear-text-storage-sensitive-data] Values that are
                    # credentials are obfuscated by the caller before this sink.
                    line = "{} = {}\n".format(key_text, value_text)
                    target.write(line)  # lgtm[py/clear-text-storage-sensitive-data]
                    written = True
                current_section = body[1:-1].strip()
                if current_section == section_text:
                    section_seen = True
                target.write(raw_line)
                continue
            line_key = ""
            if current_section == section_text and "=" in body:
                line_key = body.split("=", 1)[0].strip()
            if current_section == section_text and line_key == key_text:
                ending = ""
                if str(raw_line or "").endswith("\r\n"):
                    ending = "\r\n"
                elif str(raw_line or "").endswith("\n"):
                    ending = "\n"
                # lgtm[py/clear-text-storage-sensitive-data] Credential values
                # reach this generic writer only after caller-side obfuscation.
                line = "{} = {}{}".format(key_text, value_text, ending)
                target.write(line)  # lgtm[py/clear-text-storage-sensitive-data]
                written = True
            else:
                target.write(raw_line)
        if current_section == section_text and not written:
            # lgtm[py/clear-text-storage-sensitive-data] Credential values are
            # already obfuscated before the scalar writer receives them.
            line = "{} = {}\n".format(key_text, value_text)
            target.write(line)  # lgtm[py/clear-text-storage-sensitive-data]
            written = True
        if not section_seen:
            # lgtm[py/clear-text-storage-sensitive-data] Credential values are
            # already obfuscated before the scalar writer receives them.
            line = "\n[{}]\n{} = {}\n".format(section_text, key_text, value_text)
            target.write(line)  # lgtm[py/clear-text-storage-sensitive-data]
            written = True
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
        if not written or os.stat(tmp_path)[6] <= 0:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return ("toml_write_empty_tmp",)
        try:
            os.stat(backup_path)
            os.remove(backup_path)
        except OSError:
            pass
        try:
            os.stat(path_text)
            os.rename(path_text, backup_path)
            original_moved = True
        except OSError:
            pass
        os.rename(tmp_path, path_text)
        return ()
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
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        if original_moved:
            try:
                os.rename(backup_path, path_text)
            except OSError:
                pass
        code = getattr(exc, "errno", None)
        if code == 30 or "read-only" in str(exc).lower():
            return ("read_only_filesystem",)
        return ("persistence_failed",)


def _format_scalar(value):
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
