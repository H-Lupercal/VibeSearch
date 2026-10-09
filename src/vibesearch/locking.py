"""One process owner for a data directory's SQLite/Chroma/cache operations."""

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


class DataBusy(RuntimeError):
    pass


@contextmanager
def data_lock(data_dir):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / ".vibesearch.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise DataBusy(
            "Data directory is busy; stop the server or other command first."
        ) from None
    try:
        yield
    finally:
        lock.release()
