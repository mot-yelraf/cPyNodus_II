"""CircuitPython runtime entrypoint for cPyNodus_II."""

import sys


if getattr(sys.implementation, "name", "") != "circuitpython":
    import os

    _stdlib_code = os.path.join(os.path.dirname(os.__file__), "code.py")
    with open(_stdlib_code, "r", encoding="utf-8") as _handle:
        exec(compile(_handle.read(), _stdlib_code, "exec"), globals(), globals())
else:
    import asyncio

    from cpynodus_ii.app import main

    asyncio.run(main())
