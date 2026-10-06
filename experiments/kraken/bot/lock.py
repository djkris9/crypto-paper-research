import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def process_lock(store):
    db = store.db.execute("PRAGMA database_list").fetchone()[2]
    path = Path(db+".lock")
    handle = path.open("a+b")
    handle.seek(0)
    if not handle.read(1):
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another process is already using this database") from None
    try:
        yield
    finally:
        handle.close()
