"""CircuitPython runtime entrypoint for cPyNodus_II."""

import sys


if getattr(sys.implementation, "name", "") != "circuitpython":
    import os

    _stdlib_code = os.path.join(os.path.dirname(os.__file__), "code.py")
    with open(_stdlib_code, "r", encoding="utf-8") as _handle:
        exec(compile(_handle.read(), _stdlib_code, "exec"), globals(), globals())
else:
    import asyncio
    import sys

    from cpynodus_ii.app import main
    from cpynodus_ii.core.reboot_log import append_reboot_traceback

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
                    "runtime action=reload reason=unhandled_exception:{}".format(
                        type(exc).__name__
                    )
                )
                reload_runtime()
        except Exception:
            pass
        raise
