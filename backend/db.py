"""
Read-only access to the catalog.

FastAPI runs sync endpoints in a thread pool, and SQLite connections are not
safe to share across threads. So each thread gets its own connection, opened
once and reused for the process lifetime. There is no pool to exhaust and no
lock to wait on, because nothing here ever writes.

Opening with `mode=ro` is deliberate: a bug in a route can't corrupt the
catalog, and SQLite skips journal setup entirely.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "books.db"

DB_PATH = Path(os.environ.get("GANZACH_DB", DEFAULT_DB)).resolve()

_local = threading.local()


class CatalogMissing(RuntimeError):
    """Raised when the catalog file isn't there. The fix is to run the indexer."""


def connect() -> sqlite3.Connection:
    """The calling thread's connection, opened on first use."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    if not DB_PATH.exists():
        raise CatalogMissing(
            f"No catalog at {DB_PATH}.\n"
            f"Build one with:  python -m indexer.run"
        )

    conn = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    # 64 MB of page cache. The whole index is small enough that after a few
    # queries the hot pages stay resident and reads never touch disk.
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA query_only = 1")

    # Map the whole catalog into the address space. Reads then come from
    # the OS page cache with no copy into SQLite's own buffers. For a
    # read-only file a few tens of MB, this is the single biggest win
    # available, and it costs nothing on a memory-constrained instance
    # because mapped pages are file-backed and evictable.
    conn.execute("PRAGMA mmap_size = 268435456")   # 256 MB ceiling

    _local.conn = conn
    return conn


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def scalar(sql: str, params: tuple = ()) -> int:
    row = connect().execute(sql, params).fetchone()
    return row[0] if row else 0


def catalog_info() -> dict:
    """Row count, file size, and category breakdown — used by /api/stats."""
    total = scalar("SELECT COUNT(*) FROM books")
    categories = [
        {"name": row["category"], "count": row["n"]}
        for row in query(
            "SELECT category, COUNT(*) AS n FROM books "
            "WHERE category <> '' GROUP BY category "
            "ORDER BY n DESC LIMIT 24"
        )
    ]
    return {
        "total": total,
        "categories": categories,
        "size_mb": round(DB_PATH.stat().st_size / 1024 / 1024, 1),
        "path": str(DB_PATH),
    }
