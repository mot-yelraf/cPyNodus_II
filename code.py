"""CircuitPython runtime entrypoint for cPyNodus_II."""

import asyncio

from cpynodus_ii.app import main


asyncio.run(main())
