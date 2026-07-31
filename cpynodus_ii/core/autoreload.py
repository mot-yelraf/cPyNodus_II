"""Control CircuitPython auto-reload behavior for device runtime.

``disable_auto_reload`` tolerates missing and version-specific ``supervisor``
APIs so the same startup call is safe in host tests and on supported boards.
"""


def disable_auto_reload(supervisor_module=None):
    """Disable filesystem-triggered auto-reload when supported."""
    module = supervisor_module
    if module is None:
        try:
            import supervisor as module  # type: ignore
        except ImportError:
            return False

    runtime = getattr(module, "runtime", None)
    if runtime is not None:
        try:
            setattr(runtime, "autoreload", False)
            return True
        except Exception:
            pass

    disable = getattr(module, "disable_autoreload", None)
    if callable(disable):
        try:
            disable()
            return True
        except Exception:
            pass
    return False
