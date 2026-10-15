"""Commit small file replacements with a recoverable prior version.

These flat helpers run after writers close their handles. They avoid callbacks,
context-manager wrappers, and document allocations on shallow command paths.
They reduce interrupted-write exposure; FAT is not a power-loss-safe journal.
"""

import os


def commit_file(path, tmp_path, backup_path):
    """Sync a closed temporary, retain the prior file, and install the new one."""
    sync = getattr(os, "sync", None)
    moved = False
    try:
        if sync is not None:
            sync()
        try:
            size = os.stat(path)[6]
        except OSError as exc:
            if exc.args[0] != 2:
                raise
            size = -1
        if size > 0:
            try:
                os.remove(backup_path)
            except OSError as exc:
                if exc.args[0] != 2:
                    raise
            os.rename(path, backup_path)
            moved = True
        elif size == 0:
            # Never replace a useful backup with an empty live document.
            os.remove(path)
        if sync is not None:
            sync()
        os.rename(tmp_path, path)
        if sync is not None:
            sync()
    except Exception:
        if moved:
            try:
                os.stat(path)
            except OSError as exc:
                if exc.args[0] == 2:
                    os.rename(backup_path, path)
                    if sync is not None:
                        sync()
        raise


def readable_path(path):
    """Select the retained backup only when the live path is missing or empty."""
    try:
        if os.stat(path)[6] > 0:
            return path
    except OSError as exc:
        if exc.args[0] != 2:
            raise
    backup_path = "{}.bak".format(path)
    try:
        if os.stat(backup_path)[6] > 0:
            return backup_path
    except OSError as exc:
        if exc.args[0] != 2:
            raise
    return path


def clear_file(path):
    """Remove retained versions before durably deleting a state document."""
    sync = getattr(os, "sync", None)
    removed = False
    for suffix in (".bak", ".tmp", ""):
        try:
            os.remove("{}{}".format(path, suffix))
            removed = True
        except OSError as exc:
            if exc.args[0] != 2:
                raise
        # A reset after deleting the live file must not reveal stale state.
        if sync is not None:
            sync()
    return removed
