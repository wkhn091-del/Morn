#!/usr/bin/env python3
"""
Build the Ganzach catalog.

    python -m indexer.run                        # full crawl, IDs 1–65000
    python -m indexer.run --start 1 --end 5000   # a slice
    python -m indexer.run --no-resume            # ignore the checkpoint
    python -m indexer.run --stats                # report and exit

Run it once. The result is data/books.db, a few dozen megabytes, which is
the artifact you ship. The backend opens it read-only and never writes.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

# Allow `python indexer/run.py` as well as `python -m indexer.run`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexer.scraper import (  # noqa: E402
    DEFAULT_IMPERSONATE,
    PROBE_PROFILES,
    apply_schema,
    crawl,
    inspect_one,
    open_db,
    probe,
)

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "books.db"


def show_stats(db_path: Path) -> int:
    if not db_path.exists():
        print(f"No catalog at {db_path}. Run the crawl first.")
        return 1

    conn = open_db(db_path)
    apply_schema(conn)

    books = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    misses = conn.execute("SELECT COUNT(*) FROM crawl_misses").fetchone()[0]
    checkpoint = conn.execute(
        "SELECT value FROM crawl_state WHERE key = 'last_id'"
    ).fetchone()
    size_mb = db_path.stat().st_size / 1024 / 1024

    print(f"\n  catalog     {db_path}")
    print(f"  books       {books:,}")
    print(f"  empty ids   {misses:,}")
    print(f"  checkpoint  {checkpoint[0] if checkpoint else '—'}")
    print(f"  size        {size_mb:.1f} MB")

    rows = conn.execute(
        "SELECT category, COUNT(*) AS n FROM books "
        "WHERE category <> '' GROUP BY category ORDER BY n DESC LIMIT 12"
    ).fetchall()
    if rows:
        print("\n  top categories")
        for category, count in rows:
            print(f"    {count:>7,}  {category}")

    conn.close()
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indexer",
        description="Crawl HebrewBooks metadata into a local SQLite catalog.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="catalog path (default: data/books.db)")
    parser.add_argument("--start", type=int, default=1,
                        help="first ID (default: 1)")
    parser.add_argument("--end", type=int, default=65000,
                        help="last ID (default: 65000)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="parallel requests (default: 8 — be polite)")
    parser.add_argument("--timeout", type=int, default=20,
                        help="per-request timeout in seconds (default: 20)")
    parser.add_argument("--retries", type=int, default=2,
                        help="retries per ID on network error (default: 2)")
    parser.add_argument("--delay", type=float, default=0.15,
                        help="pause per worker between requests (default: 0.15)")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="rows per database commit (default: 200)")
    parser.add_argument("--no-resume", action="store_true",
                        help="start from --start, re-fetching known IDs")
    parser.add_argument("--stats", action="store_true",
                        help="print catalog stats and exit")
    parser.add_argument("--probe", action="store_true",
                        help="test connectivity against known-good IDs and exit")
    parser.add_argument("--inspect", type=int, metavar="ID",
                        help="fetch one ID and show everything the parser saw")
    parser.add_argument("--skip-warmup", action="store_true",
                        help="skip the homepage request before crawling")
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE,
                        metavar="PROFILE",
                        help=f"TLS fingerprint to present "
                             f"(default: {DEFAULT_IMPERSONATE}). "
                             f"Run --probe to find one that works.")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.stats:
        return show_stats(args.db)

    if args.inspect:
        return 0 if asyncio.run(
            inspect_one(args.inspect, impersonate=args.impersonate,
                        timeout=args.timeout)
        ) else 1

    if args.probe:
        profiles = (
            (args.impersonate,)
            if args.impersonate != DEFAULT_IMPERSONATE
            else PROBE_PROFILES
        )
        return 0 if asyncio.run(
            probe(timeout=args.timeout, profiles=profiles)
        ) else 1

    if args.start > args.end:
        print("--start must be <= --end")
        return 2

    print("\n  Ganzach indexer")
    print("  metadata only — no PDFs are downloaded\n")

    try:
        stats = asyncio.run(
            crawl(
                db_path=args.db,
                start_id=args.start,
                end_id=args.end,
                concurrency=args.concurrency,
                timeout=args.timeout,
                retries=args.retries,
                delay=args.delay,
                batch_size=args.batch_size,
                resume=not args.no_resume,
                skip_warmup=args.skip_warmup,
                impersonate=args.impersonate,
            )
        )
    except KeyboardInterrupt:
        print("\n  stopped. Run again to resume from the checkpoint.")
        return 130

    print(f"\n  done in {stats.elapsed / 60:.1f} min")
    print(f"  {stats.found:,} seforim indexed, {stats.missing:,} empty IDs, "
          f"{stats.failed:,} failed\n")

    # Exit codes matter here. A crawl that indexed nothing because it was
    # refused used to return 0, which CI renders as a green tick — the exact
    # failure mode that hides a broken run.
    if stats.aborted:
        print(f"  ABORTED: {stats.abort_message}\n")
        return 2

    if stats.processed and stats.found == 0:
        print("  Indexed 0 seforim. Run --probe to find out why.\n")
        return 3

    if stats.challenged:
        print(f"  {stats.challenged:,} JavaScript challenges — a different "
              f"--impersonate profile may help. Run --probe.\n")
        return 5

    if stats.processed and stats.blocked / stats.processed > 0.25:
        share = stats.blocked / stats.processed * 100
        print(f"  {share:.0f}% of requests were refused. The catalog is incomplete.\n")
        return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
