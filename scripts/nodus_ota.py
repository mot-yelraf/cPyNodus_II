#!/usr/bin/env python3
"""Run the host-side Nodus OTA package command-line interface.

This thin entry point delegates all package construction, signing, and
transfer behavior to ``ota_package.main`` and is intended for host Python, not
the CircuitPython firmware runtime.
"""

from ota_package import main

if __name__ == "__main__":
    raise SystemExit(main())
