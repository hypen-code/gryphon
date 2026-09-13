"""Exclusive single-process ownership of a run ledger's recovery lifecycle."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

from gryphon.errors import CacheError


class RecoveryLease:
    """Hold a private advisory lock from executor startup through service closure."""

    def __init__(self, db_path: str) -> None:
        """Remember the configured ledger path without creating files.

        Args:
            db_path: The same filesystem path used by the executor's run store.
        """
        self._path = Path(db_path)
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire nonblocking exclusive ownership before ledger initialization.

        Raises:
            CacheError: Another executor owns recovery or the lock is unsafe.
        """
        if self._fd is not None:
            return
        if not self._claim():
            raise CacheError("Run ledger recovery is already owned or unavailable")

    def try_acquire(self) -> bool:
        """Attempt nonblocking ownership, returning False only for live contention.

        Unsafe or unreadable lock files still raise, so a degraded filesystem never
        silently downgrades the advisory lock into no ownership at all.
        """
        if self._fd is not None:
            return True
        return self._claim()

    def _claim(self) -> bool:
        """Open and exclusively lock the private lock file, returning False only on contention."""
        fd: int | None = None
        try:
            path = self._path.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = path.with_name(path.name + ".lock")
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(lock, flags, 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise CacheError("Run ledger recovery lock is not a private regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            current = lock.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise CacheError("Run ledger recovery lock changed during acquisition")
        except (OSError, RuntimeError):
            if fd is not None:
                os.close(fd)
            raise CacheError("Run ledger recovery is already owned or unavailable") from None
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        self._fd = fd
        return True

    def close(self) -> None:
        """Release owned authority idempotently without unlinking any lock file."""
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)
