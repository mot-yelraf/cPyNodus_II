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
    return dumps_with_template(document)


def dumps_with_template(document, template_text=""):
    lines = []
    scalar_order, child_order = _parse_template_order(template_text)
    _emit_sections(lines, document, prefix=(), scalar_order=scalar_order, child_order=child_order)
    return "\n".join(lines).rstrip() + "\n"


def _emit_sections(lines, document, prefix, *, scalar_order, child_order):
    scalar_map = {}
    nested_map = {}
    for key, value in document.items():
        if isinstance(value, dict):
            nested_map[key] = value
        else:
            scalar_map[key] = value

    scalar_items = []
    seen = set()
    for key in scalar_order.get(prefix, ()):
        if key in scalar_map:
            scalar_items.append((key, scalar_map[key]))
            seen.add(key)
    for key, value in scalar_map.items():
        if key in seen:
            continue
        scalar_items.append((key, value))

    nested_items = []
    seen = set()
    for key in child_order.get(prefix, ()):
        if key in nested_map:
            nested_items.append((key, nested_map[key]))
            seen.add(key)
    for key, value in nested_map.items():
        if key in seen:
            continue
        nested_items.append((key, value))

    if prefix:
        lines.append("[{}]".format(".".join(prefix)))
    for key, value in scalar_items:
        lines.append("{} = {}".format(key, _format_value(value)))
    if prefix and nested_items:
        lines.append("")
    for index, (key, value) in enumerate(nested_items):
        _emit_sections(
            lines,
            value,
            prefix + (key,),
            scalar_order=scalar_order,
            child_order=child_order,
        )
        if index != len(nested_items) - 1:
            lines.append("")


def _parse_template_order(template_text):
    scalar_order = {}
    child_order = {}
    current = ()

    def _register_section(section):
        child_order.setdefault(section, [])
        scalar_order.setdefault(section, [])
        for index in range(len(section)):
            parent = section[:index]
            child = section[index]
            siblings = child_order.setdefault(parent, [])
            if child not in siblings:
                siblings.append(child)
            scalar_order.setdefault(parent, [])
            child_order.setdefault(section[: index + 1], [])
            scalar_order.setdefault(section[: index + 1], [])

    _register_section(())
    for raw_line in str(template_text or "").splitlines():
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = tuple(part.strip() for part in line[1:-1].strip().split(".") if part.strip())
            current = section
            _register_section(current)
            continue
        if "=" not in line:
            continue
        key, _value = line.split("=", 1)
        keys = scalar_order.setdefault(current, [])
        key = key.strip()
        if key not in keys:
            keys.append(key)
    return scalar_order, child_order


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
