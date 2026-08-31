"""
Search against the FTS5 index.

The important detail is that user input never reaches SQLite as FTS syntax.
FTS5 has its own query language — `*`, `"`, `^`, `NEAR`, `OR`, `-` all mean
something — and a stray quote in a search box would otherwise raise
`fts5: syntax error`. So we tokenize the input ourselves and rebuild a query
from known-safe pieces.

Ranking uses bm25 with a heavier weight on the title, because someone typing
"ברכות" wants the sefer called ברכות first, not the twelve seforim by an
author whose name happens to contain it.
"""

from __future__ import annotations

import re
import time

from common.hebrew import normalize
from backend import db

# bm25 weights, positional: title, author, category.
# Lower is better in SQLite's bm25(), so we negate at query time and sort ASC.
_TITLE_WEIGHT = 10.0
_AUTHOR_WEIGHT = 4.0
_CATEGORY_WEIGHT = 1.0

# After normalize() the only things left are letters, digits and spaces,
# but strip defensively — this is the boundary between user input and SQL.
_UNSAFE = re.compile(r"[^\w\s\u0590-\u05ff]", re.UNICODE)

MAX_QUERY_TOKENS = 8
MAX_LIMIT = 100


def build_match(raw: str, prefix: bool = True) -> str | None:
    """
    Turn a search box string into an FTS5 MATCH expression.

    Every token is quoted, so nothing is interpreted as an operator. Tokens
    are ANDed — narrowing as you type is what people expect. The final token
    gets a `*` so results appear mid-word while typing.

        'רמב"ם משנה'  ->  "רמבם" AND "משנה"*

    Returns None when there's nothing searchable.
    """
    normalized = normalize(raw)
    normalized = _UNSAFE.sub(" ", normalized)

    tokens = [token for token in normalized.split() if token][:MAX_QUERY_TOKENS]
    if not tokens:
        return None

    quoted = [f'"{token}"' for token in tokens[:-1]]
    last = f'"{tokens[-1]}"'
    if prefix:
        last += "*"
    quoted.append(last)

    return " AND ".join(quoted)


def _rank_expression() -> str:
    return (
        f"bm25(books_fts, {_TITLE_WEIGHT}, "
        f"{_AUTHOR_WEIGHT}, {_CATEGORY_WEIGHT})"
    )


def search(
    q: str,
    category: str = "",
    limit: int = 30,
    offset: int = 0,
) -> dict:
    """
    Full search with paging. Returns rows plus the elapsed time, which the
    frontend displays — the whole point of a local index is that this number
    is small, so we show it rather than assert it.
    """
    started = time.perf_counter()
    limit = max(1, min(limit, MAX_LIMIT))
    match = build_match(q)

    if match is None:
        rows, total = _browse(category, limit, offset)
    else:
        rows, total = _match(match, category, limit, offset)

    return {
        "books": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "query": q,
        "category": category,
    }


def _match(match: str, category: str, limit: int, offset: int) -> tuple[list, int]:
    where = ["books_fts MATCH ?"]
    params: list = [match]

    if category:
        where.append("b.category = ?")
        params.append(category)

    clause = " AND ".join(where)

    total = db.scalar(
        f"SELECT COUNT(*) FROM books_fts "
        f"JOIN books AS b ON b.id = books_fts.rowid WHERE {clause}",
        tuple(params),
    )

    rows = db.query(
        f"SELECT b.id, b.title, b.author, b.year, b.city, b.category "
        f"FROM books_fts "
        f"JOIN books AS b ON b.id = books_fts.rowid "
        f"WHERE {clause} "
        f"ORDER BY {_rank_expression()} "
        f"LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return rows, total


def _browse(category: str, limit: int, offset: int) -> tuple[list, int]:
    """Empty query — show the shelf rather than an empty screen."""
    if category:
        total = db.scalar(
            "SELECT COUNT(*) FROM books WHERE category = ?", (category,)
        )
        rows = db.query(
            "SELECT id, title, author, year, city, category FROM books "
            "WHERE category = ? ORDER BY id LIMIT ? OFFSET ?",
            (category, limit, offset),
        )
    else:
        total = db.scalar("SELECT COUNT(*) FROM books")
        rows = db.query(
            "SELECT id, title, author, year, city, category FROM books "
            "ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return rows, total


def suggest(q: str, limit: int = 8) -> list[dict]:
    """
    Autocomplete. Same index, no COUNT(*), tiny LIMIT — this runs on every
    keystroke so it does the minimum work that produces a useful list.
    """
    match = build_match(q)
    if match is None:
        return []

    rows = db.query(
        f"SELECT b.id, b.title, b.author, b.category "
        f"FROM books_fts "
        f"JOIN books AS b ON b.id = books_fts.rowid "
        f"WHERE books_fts MATCH ? "
        f"ORDER BY {_rank_expression()} "
        f"LIMIT ?",
        (match, max(1, min(limit, 20))),
    )
    return [dict(row) for row in rows]


def get_book(book_id: int) -> dict | None:
    row = db.query_one(
        "SELECT id, title, author, year, city, category, source "
        "FROM books WHERE id = ?",
        (book_id,),
    )
    return dict(row) if row else None


def related(book_id: int, limit: int = 8) -> list[dict]:
    """Same author first, then same category. Cheap, index-backed."""
    book = get_book(book_id)
    if not book:
        return []

    rows = db.query(
        "SELECT id, title, author, year, category FROM books "
        "WHERE id <> ? AND (author = ? AND author <> '') "
        "LIMIT ?",
        (book_id, book["author"], limit),
    )
    if len(rows) < limit and book["category"]:
        rows += db.query(
            "SELECT id, title, author, year, category FROM books "
            "WHERE id <> ? AND category = ? AND author <> ? "
            "LIMIT ?",
            (book_id, book["category"], book["author"], limit - len(rows)),
        )
    return [dict(row) for row in rows]
