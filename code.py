"""Run the cPyNodus_II application on CircuitPython boards.

On host Python, this module defers to the standard-library ``code`` module so
test tooling and local scripts behave normally. On CircuitPython, it starts the
main async application, records fatal tracebacks when possible, and requests a
soft reload after an unhandled runtime failure.
"""

import sys

if getattr(sys.implementation, "name", "") != "circuitpython":
    import os

    _stdlib_code = os.path.join(os.path.dirname(os.__file__), "code.py")
    with open(_stdlib_code, "r", encoding="utf-8") as _handle:
        exec(compile(_handle.read(), _stdlib_code, "exec"), globals(), globals())
else:
    import asyncio
    import sys
    import time

    from cpynodus_ii.core.autoreload import disable_auto_reload
    from cpynodus_ii.core.reboot_log import append_reboot_traceback

    def _stamp():
        try:
            now = time.localtime()
        except Exception:
            now = None
        if now is not None:
            try:
                if int(now[0]) >= 2023:
                    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                        int(now[0]),
                        int(now[1]),
                        int(now[2]),
                        int(now[3]),
                        int(now[4]),
                        int(now[5]),
                    )
            except Exception:
                pass
        try:
            elapsed = max(0, int(time.monotonic()))
        except Exception:
            elapsed = 0
        return "{}s".format(elapsed)

    disable_auto_reload()

    from cpynodus_ii.app import main

    try:
        asyncio.run(main())
    except Exception as exc:
        append_reboot_traceback(exc)
        print_exception = getattr(sys, "print_exception", None)
        if callable(print_exception):
            print_exception(exc)
        else:
            raise
        try:
            import supervisor

            reload_runtime = getattr(supervisor, "reload", None)
            if callable(reload_runtime):
                print(
                    "{} runtime action=reload reason=unhandled_exception:{}".format(
                        _stamp(),
                        type(exc).__name__,
                    )
                )
                reload_runtime()
        except Exception:
            pass
        raise
