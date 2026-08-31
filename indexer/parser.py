"""
Extract sefer metadata from a HebrewBooks detail page.

The page format
---------------
Every detail page brands its own <title> tag:

    HebrewBooks.org Sefer Detail: {title} -- {author}

Real examples:

    HebrewBooks.org Sefer Detail: זהר -- שמעון בן יוחאי (רשב"י)
    HebrewBooks.org Sefer Detail: העיטור - א -- רבי יצחק מרסילייא
    HebrewBooks.org Sefer Detail: תורת החנוך -- יפה, משה, 1859-
    HebrewBooks.org Sefer Detail: בראשית-ע"פ דיוקים על התורה --
    HebrewBooks.org Sefer Detail: Hebrew fragment 12 --

Four things that pattern forces:

  The site name is inside every valid title, so a dead-page check that
  substring-matches "hebrewbooks.org" rejects the entire catalogue. Dead
  pages are identified by the *absence* of the Sefer Detail marker, not
  by the presence of the brand.

  Titles contain single hyphens (`העיטור - א`, `פרדס יוסף-בראשית`), so the
  title/author separator is `--` specifically. Splitting on `-` truncates
  a large fraction of the catalogue mid-title.

  The author is frequently empty, leaving a trailing `--`.

  Some titles are English (`Hebrew fragment 12`). Requiring Hebrew in the
  title discards real records, so that test only applies to the fallback
  paths, where we are less certain the page is a book page at all.

`parse()` returns `Book | None`. `inspect()` returns the reason alongside
it, which is what the scraper logs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum

from bs4 import BeautifulSoup

from common.hebrew import is_hebrew


class Reject(str, Enum):
    """Why a page produced no Book."""

    EMPTY_BODY = "empty-body"        # nothing, or a truncated response
    BLOCKED_PAGE = "blocked-page"    # WAF challenge / access denied
    NO_TITLE = "no-title"            # parsed fine, no title anywhere
    DEAD_TITLE = "dead-title"        # a real page, but not a sefer page
    NOT_HEBREW = "not-hebrew"        # fallback path, and nothing Hebrew in it


# ── the detail-page marker ──────────────────────────────────────────────
# Tolerant of spacing and case; both have varied across the site's life.
_SEFER_DETAIL = re.compile(
    r"hebrewbooks\.org\s*sefer\s*detail\s*:\s*(?P<rest>.*)",
    re.IGNORECASE | re.DOTALL,
)

# Title and author are separated by a double hyphen. Split on the first one:
# author strings carry single hyphens in date ranges (`1469-1549`), and
# titles carry them as punctuation.
_AUTHOR_SEPARATOR = "--"

# Pages that exist but are not seforim. Matched against the whole title
# after folding, not as a substring of it.
_BOILERPLATE_TITLES = frozenset({
    "hebrewbooks.org",
    "hebrewbooks.org home page",
    "hebrewbooks",
    "home page",
    "page not found",
    "not found",
    "error",
    "object moved",
})

# Substrings that only ever appear on genuine error pages.
_DEAD_MARKERS = (
    "אינו קיים",
    "לא נמצא",
    "runtime error",
    "server error",
    "404",
)

# Field labels as they appear in the detail table. The site is bilingual,
# so both scripts are listed.
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "author":   ("מחבר", "המחבר", "שם המחבר", "author", "by"),
    "year":     ("שנת דפוס", "שנה", "תאריך", "year", "date", "printed"),
    "city":     ("מקום דפוס", "מקום הדפוס", "עיר", "מקום",
                 "city", "place", "publisher"),
    "category": ("נושא", "נושאים", "קטגוריה", "סוג",
                 "subject", "category", "topic"),
}

_WHITESPACE = re.compile(r"\s+")

# Brand suffixes appended to <title> on some templates. Removed by exact
# match rather than by splitting on punctuation: titles legitimately
# contain hyphens (`העיטור - א`), so a generic split truncates them.
_SITE_CHROME = re.compile(
    r"\s*[-–—|]\s*(hebrew\s*books(\.org)?|hebrewbooks(\.org)?)\s*$",
    re.IGNORECASE,
)

# Fingerprints of an interstitial served *instead of* the page. These parse
# cleanly as HTML, so without this check they would be filed as "no title".
_BLOCK_MARKERS = (
    "just a moment",              # Cloudflare JS challenge
    "checking your browser",      # Cloudflare, legacy
    "cf-browser-verification",
    "cf_chl_",                    # Cloudflare challenge script
    "attention required",         # Cloudflare block page
    "access denied",
    "request unsuccessful",       # Incapsula / Imperva
    "incapsula incident",
    "_incapsula_resource",
    "captcha",
    "ddos protection",
    "are you a robot",
    "unusual traffic",
)


@dataclass(slots=True)
class Book:
    id: int
    title: str
    author: str = ""
    year: str = ""
    city: str = ""
    category: str = ""
    source: str = "hebrewbooks"

    def as_row(self) -> tuple:
        return (
            self.id, self.title, self.author,
            self.year, self.city, self.category, self.source,
        )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ParseResult:
    book: Book | None
    reason: Reject | None = None
    detail: str = ""
    # What the parser saw on the way. Populated for --inspect.
    trace: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.book is not None


# ── helpers ─────────────────────────────────────────────────────────────


def _clean(value: str | None) -> str:
    """
    Trim whitespace, bidi marks and separator debris.

    Dashes are stripped from the left only. A leading dash is always
    separator debris, but a trailing one carries meaning — `יפה, משה, 1859-`
    is an open-ended birth date, and stripping it silently corrupts the
    record.
    """
    if not value:
        return ""
    value = _WHITESPACE.sub(" ", value)
    value = value.strip(" \t\n\r\u200e\u200f:·|")
    value = value.lstrip("-–— ")
    return value.rstrip(" \t\n\r\u200e\u200f:·|")


def looks_blocked(html: str) -> str | None:
    """
    Return the marker identifying this as an interstitial, or None.

    Only the first 4 KB is examined. Challenge pages announce themselves in
    the head, and scanning a whole sefer page for "captcha" would produce
    false positives.
    """
    head = html[:4096].lower()
    for marker in _BLOCK_MARKERS:
        if marker in head:
            return marker
    return None


def split_detail_title(raw: str) -> tuple[str, str] | None:
    """
    Pull (title, author) out of a Sefer Detail title string.

    Returns None when the string is not a Sefer Detail title at all.

    >>> split_detail_title('HebrewBooks.org Sefer Detail: זהר -- רשב"י')
    ('זהר', 'רשב"י')
    >>> split_detail_title('HebrewBooks.org Sefer Detail: העיטור - א -- ר\\' יצחק')
    ('העיטור - א', "ר' יצחק")
    >>> split_detail_title('HebrewBooks.org Sefer Detail: ספרא --')
    ('ספרא', '')
    """
    match = _SEFER_DETAIL.search(raw)
    if not match:
        return None

    rest = match.group("rest").strip()
    if _AUTHOR_SEPARATOR in rest:
        title_part, author_part = rest.split(_AUTHOR_SEPARATOR, 1)
    else:
        title_part, author_part = rest, ""

    return _clean(title_part), _clean(author_part)


def _title_candidates(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """
    Every string on the page that might be the title, tagged with its source
    so `--inspect` can show which one won.
    """
    found: list[tuple[str, str]] = []

    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        found.append(("og:title", _clean(meta["content"])))

    page_title = soup.find("title")
    if page_title:
        found.append(("<title>", _clean(page_title.get_text())))

    for tag in ("h1", "h2"):
        node = soup.find(tag)
        if node:
            found.append((f"<{tag}>", _clean(node.get_text())))

    # The detail table's own title row, on templates that have one.
    for cell in soup.find_all(("td", "th")):
        label = _clean(cell.get_text()).rstrip(":")
        if label in ("שם הספר", "כותר", "title", "שם"):
            sibling = cell.find_next_sibling(("td", "th"))
            if sibling:
                found.append(("table:title", _clean(sibling.get_text())))
            break

    return [(source, text) for source, text in found if text]


def _extract_fields(soup: BeautifulSoup) -> dict[str, str]:
    """
    Walk label/value pairs. Handles table cells and definition lists, and
    tolerates a trailing colon on the label.
    """
    found: dict[str, str] = {}

    def offer(label: str, value: str) -> None:
        label = label.rstrip(": \t").lower()
        if not label or len(label) > 24 or not value:
            return
        for name, aliases in _FIELD_LABELS.items():
            if name in found:
                continue
            if any(alias.lower() in label for alias in aliases):
                found[name] = value[:120]
                return

    for cell in soup.find_all(("td", "th")):
        sibling = cell.find_next_sibling(("td", "th"))
        if sibling:
            offer(_clean(cell.get_text()), _clean(sibling.get_text()))

    for term in soup.find_all("dt"):
        definition = term.find_next_sibling("dd")
        if definition:
            offer(_clean(term.get_text()), _clean(definition.get_text()))

    # Some templates use <b>Label:</b> value in a flat block.
    for bold in soup.find_all(("b", "strong")):
        label = _clean(bold.get_text())
        if not label.endswith(":") and ":" not in label:
            continue
        tail = bold.next_sibling
        if tail and isinstance(tail, str):
            offer(label, _clean(tail))

    return found


def _is_boilerplate(title: str) -> bool:
    """True when this is a real page that is not a sefer page."""
    folded = _clean(title).lower().rstrip(".")
    if folded in _BOILERPLATE_TITLES:
        return True
    return any(marker in folded for marker in _DEAD_MARKERS)


# ── entry points ────────────────────────────────────────────────────────


def inspect(book_id: int, html: str | None) -> ParseResult:
    """Parse a page, reporting why it failed when it does."""
    trace: dict = {}

    # Guards against an empty or truncated response only. Validity is decided
    # by the title, not by page size — a sparse legacy page is still valid.
    if not html or len(html) < 40:
        return ParseResult(None, Reject.EMPTY_BODY,
                           f"{len(html or '')} bytes", trace)

    marker = looks_blocked(html)
    if marker:
        return ParseResult(None, Reject.BLOCKED_PAGE, marker, trace)

    soup = BeautifulSoup(html, "lxml")

    candidates = _title_candidates(soup)
    trace["candidates"] = candidates
    if not candidates:
        return ParseResult(None, Reject.NO_TITLE, "", trace)

    fields = _extract_fields(soup)
    trace["fields"] = dict(fields)

    # ── preferred path: the Sefer Detail marker ──
    # A match means this is definitively a book page, whatever the script,
    # and it hands over the author for free.
    for source, text in candidates:
        parts = split_detail_title(text)
        if not parts:
            continue
        title, author = parts
        if not title:
            continue

        trace["matched"] = source
        trace["strategy"] = "sefer-detail"
        return ParseResult(
            Book(
                id=book_id,
                title=title[:200],
                # The table wins when it has one; it is better structured
                # than the title string.
                author=(fields.get("author") or author)[:120],
                year=fields.get("year", ""),
                city=fields.get("city", ""),
                category=fields.get("category", ""),
            ),
            trace=trace,
        )

    # ── fallback: an older template, or a page we do not recognise ──
    # Less certain this is a book page, so the Hebrew test applies here.
    for source, text in candidates:
        if source == "<title>":
            text = _clean(_SITE_CHROME.sub("", text))
        if not text or len(text) < 2:
            continue
        if _is_boilerplate(text):
            trace["rejected"] = f"{source}: boilerplate"
            continue
        if not is_hebrew(text):
            trace["rejected"] = f"{source}: no Hebrew"
            continue

        trace["matched"] = source
        trace["strategy"] = "fallback"

        author = fields.get("author", "")
        if not author:
            h2 = soup.find("h2")
            if h2:
                candidate = _clean(h2.get_text())
                if candidate and is_hebrew(candidate) and len(candidate) < 80:
                    author = candidate

        return ParseResult(
            Book(
                id=book_id,
                title=text[:200],
                author=author[:120],
                year=fields.get("year", ""),
                city=fields.get("city", ""),
                category=fields.get("category", ""),
            ),
            trace=trace,
        )

    # Nothing usable. Report which flavour of nothing.
    first = candidates[0][1]
    if _is_boilerplate(first):
        return ParseResult(None, Reject.DEAD_TITLE, first[:60], trace)
    return ParseResult(None, Reject.NOT_HEBREW, first[:60], trace)


def parse(book_id: int, html: str | None) -> Book | None:
    """
    Return a Book, or None if this ID has no real sefer behind it.

    Returning None is a normal outcome — the ID range is sparse. Use
    `inspect()` when you need to know which kind of None it was.
    """
    return inspect(book_id, html).book


def describe(book_id: int, html: str | None) -> str:
    """
    A human-readable account of what the parser saw. Backs `--inspect`.

    This exists because the failure it diagnoses — a valid page rejected by
    a bad heuristic — is invisible in aggregate counters. Fifteen thousand
    identical `dead-title` results say nothing about which check fired.
    """
    result = inspect(book_id, html)
    lines = [f"  id {book_id}"]

    if html:
        lines.append(f"  bytes            {len(html):,}")
        lines.append(f"  hebrew present   {is_hebrew(html)}")

    for source, text in result.trace.get("candidates", []):
        marker = "→" if result.trace.get("matched") == source else " "
        lines.append(f"  {marker} {source:<14} {text[:80]}")

    fields = result.trace.get("fields", {})
    if fields:
        lines.append("  table fields")
        for name, value in fields.items():
            lines.append(f"      {name:<10} {value[:60]}")
    else:
        lines.append("  table fields     (none found)")

    if result.trace.get("rejected"):
        lines.append(f"  rejected         {result.trace['rejected']}")

    if result.ok:
        book = result.book
        lines.append(f"  strategy         {result.trace.get('strategy')}")
        lines.append("  RESULT           ok")
        lines.append(f"      title      {book.title}")
        lines.append(f"      author     {book.author or '—'}")
        lines.append(f"      year       {book.year or '—'}")
        lines.append(f"      city       {book.city or '—'}")
        lines.append(f"      category   {book.category or '—'}")
    else:
        detail = f" ({result.detail})" if result.detail else ""
        lines.append(f"  RESULT           {result.reason.value}{detail}")

    return "\n".join(lines)
