## Summary

Describe what changed and why.

## Related issue

Link the issue, or explain why this focused change does not need one.

## Verification

List host-side commands and on-device checks performed. For hardware testing,
include the board, CircuitPython version, active profile, duration, and
filesystem mode. For MQTT changes, include broker-visible evidence.

## Checklist

- [ ] I kept the change focused and avoided unrelated reformatting.
- [ ] I added or updated focused tests when behavior changed.
- [ ] `ruff check .` passes.
- [ ] `pytest -q` passes.
- [ ] I updated docs and TOML templates when public behavior or configuration changed.
- [ ] I did not commit credentials, live device configuration, private logs, or host-specific files.
- [ ] I preserved CircuitPython compatibility and constrained-memory behavior.
- [ ] I documented relevant hardware validation, or explained why it was not required.
- [ ] I updated the firmware version only if runtime behavior changed.
