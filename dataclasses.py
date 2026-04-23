"""Provide a ``dataclasses`` import that works on host Python and CircuitPython.

The project relies on standard ``dataclasses`` behavior during host-side test
execution, but boards may need a much smaller compatibility surface. This
module dispatches to the host standard library when available and falls back to
the lightweight local shim on-device.
"""

import sys


if getattr(sys.implementation, "name", "") != "circuitpython":
    import os

    _stdlib_dataclasses = os.path.join(os.path.dirname(os.__file__), "dataclasses.py")
    with open(_stdlib_dataclasses, "r", encoding="utf-8") as _handle:
        exec(compile(_handle.read(), _stdlib_dataclasses, "exec"), globals(), globals())
else:
    MISSING = object()

    class _FieldSpec:
        def __init__(self, *, default=MISSING, default_factory=MISSING):
            self.default = default
            self.default_factory = default_factory

    def field(*, default=MISSING, default_factory=MISSING):
        return _FieldSpec(default=default, default_factory=default_factory)

    def _raw_setattr(instance, name, value):
        setattr(instance, name, value)

    def dataclass(_cls=None, *, frozen=False):
        def wrap(cls):
            annotations = getattr(cls, "__annotations__", {}) or {}
            names = list(annotations.keys())
            if not names:
                for name, value in cls.__dict__.items():
                    if name.startswith("_"):
                        continue
                    if callable(value):
                        continue
                    if isinstance(value, (staticmethod, classmethod, property)):
                        continue
                    names.append(name)
            fields = []
            for name in names:
                raw_default = cls.__dict__.get(name, MISSING)
                spec = raw_default if isinstance(raw_default, _FieldSpec) else None
                default = MISSING if spec is not None else raw_default
                default_factory = MISSING if spec is None else spec.default_factory
                fields.append((name, default, default_factory))

            def __init__(self, *args, **kwargs):
                if not fields:
                    if args:
                        raise TypeError("positional arguments are not supported without discovered fields")
                    for name, value in kwargs.items():
                        _raw_setattr(self, name, value)
                    if kwargs:
                        cls.__dataclass_fields__ = tuple(kwargs.keys())
                    post_init = getattr(self, "__post_init__", None)
                    if callable(post_init):
                        post_init()
                    return
                if len(args) > len(fields):
                    raise TypeError("too many positional arguments")
                for index, (name, default, default_factory) in enumerate(fields):
                    if index < len(args):
                        if name in kwargs:
                            raise TypeError("multiple values for {}".format(name))
                        value = args[index]
                    elif name in kwargs:
                        value = kwargs.pop(name)
                    elif default_factory is not MISSING:
                        value = default_factory()
                    elif default is not MISSING:
                        value = default
                    else:
                        raise TypeError("missing required argument: {}".format(name))
                    _raw_setattr(self, name, value)
                if kwargs:
                    extra_names = []
                    for name, value in kwargs.items():
                        _raw_setattr(self, name, value)
                        extra_names.append(name)
                    if extra_names:
                        existing = tuple(getattr(cls, "__dataclass_fields__", ()))
                        merged = list(existing)
                        for name in extra_names:
                            if name not in merged:
                                merged.append(name)
                        cls.__dataclass_fields__ = tuple(merged)
                post_init = getattr(self, "__post_init__", None)
                if callable(post_init):
                    post_init()

            def __repr__(self):
                parts = []
                for name, _, _ in fields:
                    parts.append("{}={!r}".format(name, getattr(self, name)))
                return "{}({})".format(cls.__name__, ", ".join(parts))

            cls.__init__ = __init__
            cls.__repr__ = __repr__
            cls.__dataclass_fields__ = tuple(name for name, _, _ in fields)
            return cls

        if _cls is None:
            return wrap
        return wrap(_cls)

    def replace(instance, **changes):
        kwargs = {}
        for name in getattr(instance.__class__, "__dataclass_fields__", ()):
            if name in changes:
                kwargs[name] = changes[name]
            else:
                kwargs[name] = getattr(instance, name)
        return instance.__class__(**kwargs)
