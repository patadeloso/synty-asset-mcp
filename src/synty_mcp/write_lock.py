"""Serialize cooperating library writers. Always acquire before a StateDB transaction."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path

from .config import Settings

DEFAULT_TIMEOUT_S = 3600  # a different process may be importing a large pack
_held = threading.local()


class LibraryWriteLockTimeout(TimeoutError):
    pass


@contextmanager
def library_write_lock(settings: Settings, *, timeout: float | None = None):
    """An OS-backed write lock, *not* a transaction on the main state database.

    Reentrant in the calling thread so a cache import can cover the tree check,
    extraction and indexing without letting another CLI/server writer in between.
    The empty sidecar must not be unlinked; SQLite releases its lock on close/crash.
    """
    path = Path(settings.library_dir) / ".write_lock.sqlite3"
    key = os.getpid(), path
    active = getattr(_held, "keys", None)
    if active is None:
        active = _held.keys = set()
    if key in active:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=DEFAULT_TIMEOUT_S if timeout is None else timeout)) as con:
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY):
                raise LibraryWriteLockTimeout("another process or thread is writing the library") from None
            raise
        active.add(key)
        try:
            yield
        finally:
            active.remove(key)


def library_writer(fn):
    @wraps(fn)
    def locked(settings, *args, **kwargs):
        with library_write_lock(settings):
            return fn(settings, *args, **kwargs)
    return locked
