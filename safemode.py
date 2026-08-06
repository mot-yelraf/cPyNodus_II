"""Bound automatic recovery from CircuitPython hard-fault safe mode."""

import microcontroller
import supervisor

# Keep these values aligned with cpynodus_ii.app without importing the package
# into CircuitPython's minimal safe-mode VM.
HARD_FAULT_RECOVERY_NVM_INDEX = 4
HARD_FAULT_RECOVERY_MAX_ATTEMPTS = 2

try:
    safe_mode_reason = supervisor.runtime.safe_mode_reason
    hard_fault_reason = supervisor.SafeModeReason.HARD_FAULT
except Exception:
    safe_mode_reason = None
    hard_fault_reason = None

if safe_mode_reason == hard_fault_reason and hard_fault_reason is not None:
    try:
        nvm = microcontroller.nvm
        if len(nvm) > HARD_FAULT_RECOVERY_NVM_INDEX:
            attempts = int(nvm[HARD_FAULT_RECOVERY_NVM_INDEX] or 0)
            if attempts < HARD_FAULT_RECOVERY_MAX_ATTEMPTS:
                nvm[HARD_FAULT_RECOVERY_NVM_INDEX] = attempts + 1
                microcontroller.reset()
    except Exception:
        pass
