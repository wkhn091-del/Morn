"""
Concurrent metadata crawler for HebrewBooks.

    id producer  ──▶  N fetch workers  ──▶  results queue  ──▶  1 writer
                       (curl_cffi)                               (sqlite)

Only the writer touches SQLite, so there is no lock contention and workers
never block on disk. Progress lives in the database itself
(`crawl_state.last_id`), so the process can be killed and resumed.

It fetches HTML detail pages only. No PDFs are downloaded, ever.


Why curl_cffi and not aiohttp
-----------------------------
Cloudflare fingerprints the TLS handshake, not just the headers. Every HTTP
client has a characteristic ClientHello — cipher suite order, extension
order, supported curves, ALPN — plus a characteristic HTTP/2 SETTINGS frame.
Python's ssl module produces a shape nothing like Chrome's, so aiohttp is
identifiable as a bot before it has sent a single header. Perfect headers do
not help; the decision is already made.

curl_cffi is bound to curl-impersonate, a libcurl patched to reproduce a real
browser's handshake byte for byte. `impersonate="chrome131"` makes the
connection indistinguishable at that layer.

One thing it does not do: execute JavaScript. Cloudflare's *managed*
challenge requires running JS to earn a `cf_clearance` cookie, and no HTTP
client can pass that alone. In practice most challenges fire because of the
fingerprint mismatch, so a correct fingerprint means the real page is served
and no challenge appears. If one still appears, `Reason.CHALLENGE` is
recorded separately from `Reason.BLOCKED_PAGE` — the remedies differ.


Header handling
---------------
`impersonate` supplies the whole browser header set, in the browser's order,
with a `sec-ch-ua` version matching the TLS fingerprint. Overriding that with
a hand-written set is actively harmful: `sec-ch-ua: v="122"` above a Chrome
131 handshake is its own detection signal. Only Accept-Language and the
navigation headers are set here, and only where a real browsing session
would differ.


Diagnosing
----------
    python -m indexer.run --probe

Tries several impersonation profiles against known-good IDs and prints which
ones get through. Takes about half a minute and replaces guessing.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

try:
    from curl_cffi.requests import AsyncSession
    from curl_cffi.requests import exceptions as curl_exceptions
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "\n  curl_cffi is required.\n\n"
        "    pip install curl_cffi\n\n"
        "  It ships prebuilt wheels for Linux, macOS and Windows on\n"
        "  CPython 3.8+. If pip falls back to building from source, your\n"
        "  platform has no wheel — check https://github.com/lexiforest/curl_cffi\n"
    ) from exc

from common.hebrew import expand
from indexer.parser import Book, Reject, describe, inspect, looks_blocked

BASE_URL = "https://hebrewbooks.org/{id}"
HOME_URL = "https://hebrewbooks.org/"

# IDs known to hold real seforim. Used by `probe` to tell "the site refused
# us" apart from "that stretch of the ID range is empty".
KNOWN_GOOD_IDS = (14763, 9780, 8774, 43081, 2865)


# ------------------------------------------------------------ impersonation

# curl_cffi 0.16 ships profiles up to chrome150. Newer is usually better
# against Cloudflare, because the fingerprint corpus tracks current Chrome.
DEFAULT_IMPERSONATE = "chrome131"

# Tried in order by `probe`. Chrome first, then other engines — a site
# tuned against Chrome bots sometimes waves Safari or Firefox through.
PROBE_PROFILES = (
    "chrome131",
    "chrome136",
    "chrome142",
    "chrome150",
    "safari184",
    "firefox144",
    "chrome131_android",
)

# Everything else — User-Agent, Accept, sec-ch-ua, Sec-Fetch-*, Priority,
# Accept-Encoding — comes from the impersonation profile. Do not add to this
# without checking it against what the profile already sends.
SESSION_HEADERS = {
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
}

# A real browser sends Sec-Fetch-Site: none on a typed URL and same-origin
# once it is following links within the site.
NAVIGATION_HEADERS = {
    "Referer": HOME_URL,
    "Sec-Fetch-Site": "same-origin",
}


# ---------------------------------------------------------------- logging

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def log(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"::warning::{message}" if IN_CI else f"  !  {message}", flush=True)


def error(message: str) -> None:
    print(f"::error::{message}" if IN_CI else f"  ✗  {message}", flush=True)


# ---------------------------------------------------------------- decoding

# HebrewBooks has served pages in both UTF-8 and Windows-1255 over the years.
# Getting this wrong yields mojibake that fails the is_hebrew() check, so
# every page would be silently dropped as "not-hebrew".
_HEBREW_ENCODINGS = ("utf-8", "cp1255", "iso-8859-8")


def decode_body(response) -> str:
    """Decode a response body, trying the encodings this site actually uses."""
    raw = response.content
    if not raw:
        return ""

    declared = (response.headers.get("content-type") or "").lower()
    candidates: list[str] = []
    if "charset=" in declared:
        candidates.append(declared.split("charset=", 1)[1].split(";")[0].strip())
    candidates.extend(_HEBREW_ENCODINGS)

    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # A decode that "succeeds" into replacement characters is a failure.
        if "\ufffd" not in text:
            return text

    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- outcomes


class Reason(str, Enum):
    """Where an ID ended up. Exactly one per ID."""

    OK = "ok"
    HTTP_403 = "http-403"
    HTTP_404 = "http-404"
    HTTP_429 = "http-429"
    HTTP_5XX = "http-5xx"
    HTTP_OTHER = "http-other"
    TIMEOUT = "timeout"
    NETWORK = "network"
    CHALLENGE = "challenge"          # a JS challenge despite a good fingerprint
    BLOCKED_PAGE = "blocked-page"    # a flat refusal page
    NO_TITLE = "no-title"
    DEAD_TITLE = "dead-title"
    NOT_HEBREW = "not-hebrew"
    EMPTY_BODY = "empty-body"


# A 404 is expected — the ID range is sparse. These are not.
HARD_FAILURES = frozenset({
    Reason.HTTP_403, Reason.HTTP_429, Reason.HTTP_5XX,
    Reason.TIMEOUT, Reason.NETWORK, Reason.CHALLENGE, Reason.BLOCKED_PAGE,
})

# Reasons meaning "we are being refused", as opposed to a transient blip.
BLOCK_SIGNALS = frozenset({
    Reason.HTTP_403, Reason.HTTP_429, Reason.CHALLENGE, Reason.BLOCKED_PAGE,
})

# Markers that specifically indicate a JavaScript challenge rather than a
# flat block. These are the ones curl_cffi alone cannot clear.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_",
    "captcha",
    "are you a robot",
)

_REJECT_TO_REASON = {
    Reject.EMPTY_BODY:   Reason.EMPTY_BODY,
    Reject.BLOCKED_PAGE: Reason.BLOCKED_PAGE,
    Reject.NO_TITLE:     Reason.NO_TITLE,
    Reject.DEAD_TITLE:   Reason.DEAD_TITLE,
    Reject.NOT_HEBREW:   Reason.NOT_HEBREW,
}


def classify_block(marker: str) -> Reason:
    """A JS challenge and a flat refusal need different remedies."""
    return (
        Reason.CHALLENGE
        if any(m in marker for m in _CHALLENGE_MARKERS)
        else Reason.BLOCKED_PAGE
    )


@dataclass(slots=True)
class Fetched:
    book_id: int
    reason: Reason
    book: Book | None = None
    detail: str = ""


@dataclass
class Stats:
    reasons: Counter = field(default_factory=Counter)
    started: float = field(default_factory=time.monotonic)
    aborted: bool = False
    abort_message: str = ""
    impersonate: str = DEFAULT_IMPERSONATE

    def record(self, reason: Reason) -> None:
        self.reasons[reason] += 1

    @property
    def found(self) -> int:
        return self.reasons[Reason.OK]

    @property
    def missing(self) -> int:
        """Legitimately empty IDs — a 404 or the site's own dead page."""
        return self.reasons[Reason.HTTP_404] + self.reasons[Reason.DEAD_TITLE]

    @property
    def failed(self) -> int:
        return sum(self.reasons[reason] for reason in HARD_FAILURES)

    @property
    def blocked(self) -> int:
        return sum(self.reasons[reason] for reason in BLOCK_SIGNALS)

    @property
    def challenged(self) -> int:
        return self.reasons[Reason.CHALLENGE]

    @property
    def processed(self) -> int:
        return sum(self.reasons.values())

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def rate(self) -> float:
        return self.processed / self.elapsed if self.elapsed > 0 else 0.0

    def summary(self) -> str:
        if not self.processed:
            return "  nothing processed"

        width = max(len(reason.value) for reason in self.reasons)
        lines = ["  outcome by reason"]
        for reason, count in self.reasons.most_common():
            share = count / self.processed * 100
            lines.append(f"    {reason.value:<{width}}  {count:>7,}  {share:>5.1f}%")
        return "\n".join(lines)


# ------------------------------------------------------------ throttling


class Throttle:
    """
    Shared, adaptive pacing.

    A refusal seen by one worker must slow all of them — otherwise the other
    fifteen keep hammering and confirm whatever the WAF suspected. On success
    the delay decays back toward the configured floor.
    """

    def __init__(self, base_delay: float, max_delay: float = 8.0) -> None:
        self.base = base_delay
        self.max = max_delay
        self.current = base_delay
        self._gate = asyncio.Event()
        self._gate.set()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        await self._gate.wait()
        if self.current > 0:
            # Jitter: identical spacing across workers is itself a signature.
            import random
            await asyncio.sleep(self.current * random.uniform(0.7, 1.3))

    async def penalise(self, seconds: float | None = None) -> None:
        async with self._lock:
            if seconds is not None:
                self.current = min(self.max, max(self.current, seconds))
            else:
                self.current = min(self.max, max(0.25, self.current * 2))

    async def relax(self) -> None:
        if self.current > self.base:
            async with self._lock:
                self.current = max(self.base, self.current * 0.9)

    async def pause(self, seconds: float) -> None:
        """Stop every worker for a fixed period."""
        if not self._gate.is_set():
            return
        self._gate.clear()
        warn(f"pausing all workers for {seconds:.0f}s")
        try:
            await asyncio.sleep(seconds)
        finally:
            self._gate.set()


class SessionGuard:
    """
    Re-warm the session when a challenge appears mid-crawl.

    Cloudflare's `__cf_bm` cookie expires after about half an hour, and its
    disappearance mid-run looks like a fresh unidentified client. Re-fetching
    the homepage picks up a new one. Guarded by a lock and a cooldown so a
    burst of challenges triggers one recovery, not fifty.
    """

    def __init__(self, session: AsyncSession, throttle: Throttle,
                 cooldown: float = 60.0) -> None:
        self.session = session
        self.throttle = throttle
        self.cooldown = cooldown
        self.last_attempt = 0.0
        self.attempts = 0
        self._lock = asyncio.Lock()

    async def recover(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            if now - self.last_attempt < self.cooldown:
                return False
            self.last_attempt = now
            self.attempts += 1

            warn(f"challenge seen — refreshing session cookies (attempt {self.attempts})")
            await self.throttle.pause(10.0)

            try:
                response = await self.session.get(HOME_URL, timeout=30)
                body = decode_body(response)
            except curl_exceptions.RequestException as exc:
                error(f"recovery request failed — {type(exc).__name__}: {exc}")
                return False

            if response.status_code == 200 and not looks_blocked(body):
                log("    session refreshed, resuming")
                return True

            error("still challenged after refresh")
            return False


class CircuitBreaker:
    """
    Abort a doomed crawl early.

    The failure this exists for: a run where every request is refused,
    finishing in seconds with a zero exit code and no books.
    """

    def __init__(self, threshold: int = 25, sample: int = 60) -> None:
        self.threshold = threshold
        self.sample = sample
        self.consecutive = 0
        self.seen = 0
        self.hard = 0
        self.tripped = False
        self.message = ""

    def record(self, reason: Reason, found_so_far: int) -> bool:
        """Return True when the crawl should stop."""
        if self.tripped:
            return True

        self.seen += 1

        if reason in HARD_FAILURES:
            self.hard += 1
            self.consecutive += 1
        else:
            self.consecutive = 0

        if self.consecutive >= self.threshold:
            self.tripped = True
            self.message = (
                f"{self.consecutive} consecutive failed requests — "
                f"the upstream is refusing this client"
            )
        elif (
            self.seen >= self.sample
            and found_so_far == 0
            and self.hard / self.seen > 0.8
        ):
            self.tripped = True
            self.message = (
                f"{self.hard} of the first {self.seen} requests failed and "
                f"nothing was indexed — this is a block, not an empty range"
            )

        return self.tripped


# ---------------------------------------------------------------- database


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.commit()


def get_checkpoint(conn: sqlite3.Connection, default: int) -> int:
    row = conn.execute(
        "SELECT value FROM crawl_state WHERE key = 'last_id'"
    ).fetchone()
    return int(row[0]) if row else default


def set_checkpoint(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        "INSERT INTO crawl_state(key, value) VALUES('last_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(value),),
    )


def known_ids(conn: sqlite3.Connection) -> set[int]:
    """
    IDs already resolved. Only *confirmed* empties count — a 403, a challenge
    or a timeout must be retried on the next run, not treated as a gap.
    """
    seen = {row[0] for row in conn.execute("SELECT id FROM books")}
    seen |= {
        row[0]
        for row in conn.execute(
            "SELECT id FROM crawl_misses WHERE reason IN (?, ?)",
            (Reason.HTTP_404.value, Reason.DEAD_TITLE.value),
        )
    }
    return seen


def write_batch(
    conn: sqlite3.Connection,
    books: list[Book],
    misses: list[tuple[int, str]] | list[int],
) -> None:
    """
    Insert into `books` and `books_fts` together.

    `misses` accepts either (id, reason) pairs or bare ids; the second form
    keeps older callers and the test suite working.
    """
    if books:
        conn.executemany(
            "INSERT OR REPLACE INTO books"
            "(id, title, author, year, city, category, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [book.as_row() for book in books],
        )
        # FTS5 has no UPSERT; clear any stale row first.
        conn.executemany(
            "DELETE FROM books_fts WHERE rowid = ?",
            [(book.id,) for book in books],
        )
        conn.executemany(
            "INSERT INTO books_fts(rowid, title, author, category) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    book.id,
                    expand(book.title),
                    expand(book.author),
                    expand(book.category),
                )
                for book in books
            ],
        )

    if misses:
        normalized = [
            item if isinstance(item, tuple) else (item, Reason.HTTP_404.value)
            for item in misses
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO crawl_misses(id, reason) VALUES (?, ?)",
            normalized,
        )

    conn.commit()


# ---------------------------------------------------------------- fetching


def make_session(impersonate: str, concurrency: int, timeout: int) -> AsyncSession:
    """
    A session carrying a browser's TLS fingerprint.

    `impersonate` sets the ClientHello and the HTTP/2 SETTINGS frame as well
    as the default headers, so the connection matches the User-Agent it
    claims. `max_clients` caps concurrent transfers inside libcurl's multi
    handle; the semaphore in `crawl` gates on top of it.
    """
    return AsyncSession(
        impersonate=impersonate,
        max_clients=max(1, concurrency),
        headers=dict(SESSION_HEADERS),
        timeout=timeout,
        verify=True,
    )


async def warm_up(session: AsyncSession) -> tuple[bool, str]:
    """
    Request the homepage once before crawling.

    A client whose first request is a deep link, carrying no cookies, looks
    like a scraper. This collects Cloudflare's `__cf_bm` cookie and gives the
    run a plausible history. It also fails fast when the host is unreachable
    or already refusing.
    """
    try:
        response = await session.get(HOME_URL)
        body = decode_body(response)
    except curl_exceptions.RequestException as exc:
        return False, f"cannot reach {HOME_URL} — {type(exc).__name__}: {exc}"

    if response.status_code != 200:
        marker = looks_blocked(body) or ""
        suffix = f" ({marker})" if marker else ""
        return False, f"homepage returned HTTP {response.status_code}{suffix}"

    marker = looks_blocked(body)
    if marker:
        return False, f"homepage is an interstitial — {marker!r}"

    cookies = len(session.cookies)
    log(f"  warm-up   HTTP 200, {len(body):,} bytes, {cookies} cookie(s)")
    return True, ""


async def fetch_one(
    session: AsyncSession,
    book_id: int,
    retries: int,
    throttle: Throttle,
    guard: SessionGuard | None,
) -> Fetched:
    """
    Resolve one ID. Never raises; every path returns a Fetched with a reason.
    """
    url = BASE_URL.format(id=book_id)
    last = Reason.NETWORK
    detail = ""

    for attempt in range(retries + 1):
        await throttle.wait()

        try:
            response = await session.get(url, headers=NAVIGATION_HEADERS)
            status = response.status_code

            if status == 200:
                html = decode_body(response)

                marker = looks_blocked(html)
                if marker:
                    reason = classify_block(marker)
                    await throttle.penalise()
                    if reason is Reason.CHALLENGE and guard is not None:
                        if await guard.recover() and attempt < retries:
                            continue
                    return Fetched(book_id, reason, None, marker)

                await throttle.relax()
                result = inspect(book_id, html)
                if result.ok:
                    return Fetched(book_id, Reason.OK, result.book)
                return Fetched(
                    book_id,
                    _REJECT_TO_REASON.get(result.reason, Reason.NO_TITLE),
                    None,
                    result.detail,
                )

            if status == 404:
                await throttle.relax()
                return Fetched(book_id, Reason.HTTP_404)

            if status == 429:
                retry_after = str(response.headers.get("Retry-After") or "")
                wait = float(retry_after) if retry_after.isdigit() else 30.0
                await throttle.penalise(wait)
                last, detail = Reason.HTTP_429, f"Retry-After: {retry_after or '—'}"
                if attempt < retries:
                    await asyncio.sleep(wait)
                    continue
                return Fetched(book_id, last, None, detail)

            if status == 403:
                body = decode_body(response)
                marker = looks_blocked(body)
                await throttle.penalise()
                if marker:
                    reason = classify_block(marker)
                    if reason is Reason.CHALLENGE and guard is not None:
                        if await guard.recover() and attempt < retries:
                            continue
                    return Fetched(book_id, reason, None, marker)
                last, detail = Reason.HTTP_403, "forbidden"
                if attempt < retries:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                return Fetched(book_id, last, None, detail)

            if status >= 500:
                last, detail = Reason.HTTP_5XX, f"HTTP {status}"
                if attempt < retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                return Fetched(book_id, last, None, detail)

            return Fetched(book_id, Reason.HTTP_OTHER, None, f"HTTP {status}")

        except curl_exceptions.Timeout:
            last, detail = Reason.TIMEOUT, "read timeout"
        except curl_exceptions.RequestException as exc:
            last, detail = Reason.NETWORK, f"{type(exc).__name__}: {exc}"[:120]

        if attempt < retries:
            await asyncio.sleep(0.5 * (2**attempt))

    return Fetched(book_id, last, None, detail)


async def worker(
    session: AsyncSession,
    ids: asyncio.Queue,
    results: asyncio.Queue,
    limiter: asyncio.Semaphore,
    retries: int,
    throttle: Throttle,
    guard: SessionGuard,
    stop: asyncio.Event,
) -> None:
    while True:
        book_id = await ids.get()
        try:
            if stop.is_set():
                continue
            async with limiter:
                fetched = await fetch_one(session, book_id, retries, throttle, guard)
            await results.put(fetched)
        finally:
            ids.task_done()


# ---------------------------------------------------------------- writer

_DONE = object()

# How many examples of each failure reason to print before going quiet.
_SAMPLES_PER_REASON = 3


async def writer(
    conn: sqlite3.Connection,
    results: asyncio.Queue,
    stats: Stats,
    breaker: CircuitBreaker,
    stop: asyncio.Event,
    batch_size: int,
    total: int,
    verbose: bool,
) -> None:
    books: list[Book] = []
    misses: list[tuple[int, str]] = []
    high_water = 0
    shown: Counter = Counter()
    last_report = time.monotonic()

    def flush() -> None:
        nonlocal books, misses
        if books or misses:
            write_batch(conn, books, misses)
            books, misses = [], []
        if high_water:
            set_checkpoint(conn, high_water)
            conn.commit()

    while True:
        item = await results.get()
        try:
            if item is _DONE:
                flush()
                return

            stats.record(item.reason)
            high_water = max(high_water, item.book_id)

            if item.reason is Reason.OK and item.book:
                books.append(item.book)
            else:
                # Only record a permanent gap for confirmed empties. A refusal
                # must stay retryable, or one blocked run poisons the range.
                if item.reason in (Reason.HTTP_404, Reason.DEAD_TITLE):
                    misses.append((item.book_id, item.reason.value))

                # The first few of each kind print with the ID, so the log
                # names the problem instead of only counting it.
                if verbose and shown[item.reason] < _SAMPLES_PER_REASON:
                    shown[item.reason] += 1
                    suffix = f" — {item.detail}" if item.detail else ""
                    log(f"    id {item.book_id:<7} {item.reason.value}{suffix}")

            if breaker.record(item.reason, stats.found) and not stop.is_set():
                stop.set()
                stats.aborted = True
                stats.abort_message = breaker.message
                error(breaker.message)
                flush()
                return

            if len(books) + len(misses) >= batch_size:
                flush()

            now = time.monotonic()
            if now - last_report >= 10.0:
                last_report = now
                report(stats, high_water, total)

        finally:
            results.task_done()


def report(stats: Stats, current_id: int, total: int) -> None:
    """One line per interval, newline-terminated so CI logs stay readable."""
    percent = (stats.processed / total * 100) if total else 0.0
    log(
        f"    id {current_id:<7} "
        f"found {stats.found:<7,} "
        f"empty {stats.missing:<7,} "
        f"failed {stats.failed:<6,} "
        f"{stats.rate:>5.1f}/s  {percent:>5.1f}%"
    )


# ---------------------------------------------------------------- probe


async def probe_profile(profile: str, timeout: int = 25) -> tuple[str, int, int, str]:
    """
    Test one impersonation profile.

    Returns (profile, homepage_status, seforim_parsed, note).
    """
    try:
        session = make_session(profile, concurrency=2, timeout=timeout)
    except Exception as exc:  # unknown profile for this curl_cffi build
        return profile, 0, 0, f"unsupported: {exc}"

    async with session:
        try:
            response = await session.get(HOME_URL)
            body = decode_body(response)
            home_status = response.status_code
        except curl_exceptions.RequestException as exc:
            return profile, 0, 0, type(exc).__name__

        marker = looks_blocked(body)
        if marker or home_status != 200:
            return profile, home_status, 0, marker or f"HTTP {home_status}"

        parsed = 0
        note = ""
        for book_id in KNOWN_GOOD_IDS:
            try:
                response = await session.get(
                    BASE_URL.format(id=book_id), headers=NAVIGATION_HEADERS
                )
                html = decode_body(response)
            except curl_exceptions.RequestException as exc:
                note = note or type(exc).__name__
                continue

            blocked = looks_blocked(html)
            if blocked:
                note = note or blocked
                continue
            if response.status_code != 200:
                note = note or f"HTTP {response.status_code}"
                continue

            result = inspect(book_id, html)
            if result.ok:
                parsed += 1
            else:
                note = note or (result.reason.value if result.reason else "unparsed")

            await asyncio.sleep(0.4)

        return profile, home_status, parsed, note


async def probe(timeout: int = 25, profiles: tuple[str, ...] = PROBE_PROFILES) -> bool:
    """
    Try several TLS fingerprints and report which get through.

    Cloudflare configurations differ, and which profile works is an empirical
    question. This answers it in about half a minute instead of by guessing.
    """
    total = len(KNOWN_GOOD_IDS)
    log("\n  Probing hebrewbooks.org across TLS fingerprints\n")
    log(f"  {'profile':<20} {'home':>6}  {'seforim':>8}   note")
    log(f"  {'-' * 20} {'-' * 6}  {'-' * 8}   {'-' * 28}")

    working: list[str] = []

    for profile in profiles:
        name, status, parsed, note = await probe_profile(profile, timeout)
        verdict = f"{parsed}/{total}"
        log(f"  {name:<20} {status or '—':>6}  {verdict:>8}   {note[:28]}")
        if parsed == total:
            working.append(name)
            # One clean profile is enough; stop burning requests.
            if len(working) >= 2:
                break

    log("")

    if working:
        log(f"  Working profile(s): {', '.join(working)}\n")
        if working[0] != DEFAULT_IMPERSONATE:
            log(f"  Run the crawl with:  --impersonate {working[0]}\n")
        else:
            log("  The default profile works. Start the crawl.\n")
        return True

    error("No impersonation profile got through.")
    explain_failure()
    return False


async def inspect_one(
    book_id: int,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: int = 25,
) -> bool:
    """
    Fetch one page and print everything the parser saw.

    This exists because of a failure that aggregate counters cannot express:
    a valid page rejected by a bad heuristic. Fifteen thousand identical
    `dead-title` results say the parser refused everything, but not which
    check fired or what the page actually contained. This shows the raw
    <title>, every title candidate, the table fields, and the verdict.
    """
    async with make_session(impersonate, concurrency=2, timeout=timeout) as session:
        ok, message = await warm_up(session)
        if not ok:
            error(message)
            return False

        url = BASE_URL.format(id=book_id)
        try:
            response = await session.get(url, headers=NAVIGATION_HEADERS)
        except curl_exceptions.RequestException as exc:
            error(f"{type(exc).__name__}: {exc}")
            return False

        html = decode_body(response)

    log(f"\n  {url}")
    log(f"  HTTP {response.status_code}   "
        f"{response.headers.get('content-type', '—')}   tls {impersonate}\n")

    marker = looks_blocked(html)
    if marker:
        error(f"interstitial — {marker!r}")
        return False

    log(describe(book_id, html))
    log("")
    return inspect(book_id, html).ok


def explain_failure() -> None:
    log("""
  What this means

    curl_cffi reproduces Chrome's TLS handshake and HTTP/2 fingerprint
    exactly, so if every profile is still refused, the block is not based
    on fingerprint. That leaves two possibilities.

    1. A JavaScript challenge (Cloudflare "managed challenge"). The page
       says "just a moment" and demands a cf_clearance cookie earned by
       executing JS. No HTTP client can produce that — it needs a real
       browser engine.

    2. IP reputation. Datacenter and VPN ranges are refused outright,
       whatever the fingerprint.

  What actually works

    A. Check whether you are on a VPN or proxy. Turn it off and re-probe.
       This is the single most common cause on a home connection.

    B. For a JS challenge, drive a real browser once:

           pip install playwright && playwright install chromium

       Fetch the homepage with Playwright, read the cf_clearance cookie,
       and hand it to the crawler. That cookie is bound to your IP and
       User-Agent, so the impersonation profile must match the browser
       that earned it. It typically lasts around 30 minutes.

    C. Slow right down. Some Cloudflare rules are rate-triggered and only
       challenge after a burst:

           python -m indexer.run --concurrency 2 --delay 1.0

    D. Check the site directly in your browser. If hebrewbooks.org shows
       you a challenge there too, it is having an incident and no client
       change will help. Wait and retry.
""")


# ---------------------------------------------------------------- crawl


async def crawl(
    db_path: Path,
    start_id: int,
    end_id: int,
    concurrency: int = 8,
    timeout: int = 25,
    retries: int = 2,
    delay: float = 0.15,
    batch_size: int = 100,
    resume: bool = True,
    quiet: bool = False,
    skip_warmup: bool = False,
    impersonate: str = DEFAULT_IMPERSONATE,
) -> Stats:
    conn = open_db(db_path)
    apply_schema(conn)

    if resume:
        start_id = max(start_id, get_checkpoint(conn, start_id))

    skip = known_ids(conn) if resume else set()
    targets = [i for i in range(start_id, end_id + 1) if i not in skip]

    if not targets:
        log("  Nothing to do — every ID in range is already resolved.")
        conn.close()
        return Stats(impersonate=impersonate)

    log(f"  range     {start_id:,} – {end_id:,}")
    log(f"  to fetch  {len(targets):,}  (skipping {len(skip):,} already resolved)")
    log(f"  workers   {concurrency}   delay {delay}s   retries {retries}")
    log(f"  tls       {impersonate}")

    stats = Stats(impersonate=impersonate)
    breaker = CircuitBreaker()
    throttle = Throttle(delay)
    stop = asyncio.Event()

    ids: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 4)
    results: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 4)
    limiter = asyncio.Semaphore(concurrency)

    async with make_session(impersonate, concurrency, timeout) as session:

        if not skip_warmup:
            ok, message = await warm_up(session)
            if not ok:
                error(message)
                explain_failure()
                conn.close()
                stats.aborted = True
                stats.abort_message = message
                return stats

        guard = SessionGuard(session, throttle)

        log("")
        writer_task = asyncio.create_task(
            writer(conn, results, stats, breaker, stop,
                   batch_size, len(targets), not quiet)
        )
        workers = [
            asyncio.create_task(
                worker(session, ids, results, limiter, retries,
                       throttle, guard, stop)
            )
            for _ in range(concurrency)
        ]

        try:
            for book_id in targets:
                if stop.is_set():
                    break
                await ids.put(book_id)
            await ids.join()
        except (KeyboardInterrupt, asyncio.CancelledError):
            log("\n  interrupted — flushing what we have")
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if not writer_task.done():
                await results.put(_DONE)
            await writer_task

    if stats.found:
        optimize(conn)
    conn.close()

    log("")
    log(stats.summary())

    if stats.challenged:
        warn(
            f"{stats.challenged:,} JavaScript challenges — curl_cffi cannot "
            f"clear those. Run --probe for the options."
        )

    return stats


def optimize(conn: sqlite3.Connection) -> None:
    """Merge FTS b-trees and reclaim pages. Meaningful on a 130k-row index."""
    log("\n  optimizing index...")
    conn.execute("INSERT INTO books_fts(books_fts) VALUES('optimize')")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.commit()


# ---------------------------------------------------------------- __main__

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        raise SystemExit(0 if asyncio.run(probe()) else 1)

    log("  Run the crawler with:  python -m indexer.run")
    log("  Diagnose a block with: python -m indexer.run --probe")
