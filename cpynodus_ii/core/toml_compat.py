"""Minimal TOML reader/writer for CircuitPython runtime settings files."""


def load_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read())


def loads(text):
    document = {}
    current = document
    for raw_line in str(text or "").splitlines():
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = document
            section = line[1:-1].strip()
            if not section:
                continue
            for part in section.split("."):
                current = current.setdefault(part.strip(), {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = _parse_value(value.strip())
    return document


def dumps(document):
    lines = []
    _emit_sections(lines, document, prefix=())
    return "\n".join(lines).rstrip() + "\n"


def _emit_sections(lines, document, prefix):
    scalar_items = []
    nested_items = []
    for key, value in document.items():
        if isinstance(value, dict):
            nested_items.append((key, value))
        else:
            scalar_items.append((key, value))

    if prefix:
        lines.append("[{}]".format(".".join(prefix)))
    for key, value in scalar_items:
        lines.append("{} = {}".format(key, _format_value(value)))
    if prefix and nested_items:
        lines.append("")
    for index, (key, value) in enumerate(nested_items):
        _emit_sections(lines, value, prefix + (key,))
        if index != len(nested_items) - 1:
            lines.append("")


def _strip_comment(line):
    in_string = False
    escaped = False
    result = []
    for char in str(line or ""):
        if char == '"' and not escaped:
            in_string = not in_string
        if char == "#" and not in_string:
            break
        result.append(char)
        escaped = (char == "\\") and not escaped
        if char != "\\":
            escaped = False
    return "".join(result)


def _parse_value(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    lower = text.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        if any(marker in text for marker in (".", "e", "E")):
            return float(text)
        return int(text)
    except ValueError:
        return text


def _format_value(value):
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
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
