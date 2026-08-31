"""
Tests for the parts that are easy to break without noticing.

    python tests/test_search.py     # no dependencies
    pytest tests/                   # if you have it

Builds a throwaway catalog in a temp directory, so it never touches
data/books.db and needs no network.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.hebrew import expand, normalize, strip_prefix  # noqa: E402
from indexer.parser import (  # noqa: E402
    Book,
    Reject,
    inspect,
    looks_blocked,
    parse,
    split_detail_title,
)
from indexer.scraper import apply_schema, open_db, write_batch  # noqa: E402

FIXTURES = [
    Book(14763, "משנה תורה", 'הרמב"ם', 'ה\'תק"פ', "ורשה", "הלכה"),
    Book(9780, "שולחן ערוך אורח חיים", "ר' יוסף קארו", 'שכ"ה', "ויניציאה", "הלכה"),
    Book(8774, "מסילת ישרים", 'הרמח"ל', 'ת"ק', "אמשטרדם", "מוסר"),
    Book(2865, "ברכות עם פירוש", 'רשב"י', 'ר"מ', "מנטובה", "תלמוד"),
    Book(3456, 'שו"ת חתם סופר', "ר' משה סופר", 'תקצ"ט', "פרשבורג", 'שו"ת'),
]


def _catalog() -> Path:
    """A temp catalog, wired into backend.db before it opens anything."""
    path = Path(tempfile.mkdtemp()) / "test.db"
    conn = open_db(path)
    apply_schema(conn)
    write_batch(conn, FIXTURES, [1, 2, 3])
    conn.close()

    from backend import db

    db.DB_PATH = path
    db._local = type(db._local)()  # drop any cached connection
    return path


# ---------------------------------------------------------- normalization


def test_normalize_strips_gershayim():
    assert normalize('שו"ת חתם סופר') == "שות חתם סופר"
    assert normalize("רמב״ם") == "רמבם"
    assert normalize('רמב"ם') == "רמבם"


def test_normalize_strips_nikud():
    assert normalize("בְּרֵאשִׁית") == "בראשית"


def test_normalize_collapses_whitespace():
    assert normalize("  מסילת   ישרים  ") == "מסילת ישרים"


def test_strip_prefix_only_touches_he_and_vav():
    assert strip_prefix("הרמבם") == "רמבם"
    assert strip_prefix("ורשבא") == "רשבא"
    # ב must survive — stripping it would make רכות match ברכות
    assert strip_prefix("ברכות") == "ברכות"
    # too short to strip safely
    assert strip_prefix("הוא") == "הוא"


def test_expand_adds_stripped_variant():
    assert expand("הרמב״ם") == "הרמבם רמבם"
    assert expand("ברכות") == "ברכות"


# ---------------------------------------------------------------- parser


# Verbatim <title> strings from real HebrewBooks pages. The format is
#     HebrewBooks.org Sefer Detail: {title} -- {author}
# and every one of these was rejected as `dead-title` by the previous
# parser, because the site brands its own <title> with its own name.
REAL_TITLES = [
    ('HebrewBooks.org Sefer Detail: ספר התשבי -- אליהו בן אשר, הלוי, אשכנזי, 1469-1549',
     "ספר התשבי", "אליהו בן אשר, הלוי, אשכנזי, 1469-1549"),
    ('HebrewBooks.org Sefer Detail: שו"ת משה מבחוריו -- זילברבלט, משה',
     'שו"ת משה מבחוריו', "זילברבלט, משה"),
    # a hyphen inside the title
    ("HebrewBooks.org Sefer Detail: העיטור - א -- רבי יצחק מרסילייא",
     "העיטור - א", "רבי יצחק מרסילייא"),
    ("HebrewBooks.org Sefer Detail: פרדס יוסף-בראשית -- פאצאנאווסקי, יוסף",
     "פרדס יוסף-בראשית", "פאצאנאווסקי, יוסף"),
    # empty author, trailing separator
    ("HebrewBooks.org Sefer Detail: ספרא --", "ספרא", ""),
    ('HebrewBooks.org Sefer Detail: בראשית-ע"פ דיוקים על התורה --',
     'בראשית-ע"פ דיוקים על התורה', ""),
    # open-ended birth date: the trailing hyphen is meaningful
    ("HebrewBooks.org Sefer Detail: תורת החנוך -- יפה, משה, 1859-",
     "תורת החנוך", "יפה, משה, 1859-"),
    # an English title in a Hebrew catalogue
    ("HebrewBooks.org Sefer Detail: Hebrew fragment 12 --",
     "Hebrew fragment 12", ""),
]


def test_split_detail_title_against_real_pages():
    for raw, want_title, want_author in REAL_TITLES:
        parts = split_detail_title(raw)
        assert parts is not None, f"no match: {raw}"
        title, author = parts
        assert title == want_title, f"{raw}\n  title {title!r} != {want_title!r}"
        assert author == want_author, f"{raw}\n  author {author!r} != {want_author!r}"


def test_real_page_is_not_rejected_as_dead():
    """The regression this whole rewrite exists for."""
    html = (
        "<html><head><title>HebrewBooks.org Sefer Detail: "
        "משנה תורה -- משה בן מימון, 1135-1204</title></head><body>"
        "<table><tr><td>מקום דפוס:</td><td>ורשה</td></tr>"
        '<tr><td>שנת דפוס:</td><td>תק"פ</td></tr>'
        "<tr><td>נושא:</td><td>הלכה</td></tr></table></body></html>"
    )
    result = inspect(14763, html)
    assert result.ok, f"rejected as {result.reason}"
    assert result.book.title == "משנה תורה"
    assert result.book.author == "משה בן מימון, 1135-1204"
    assert result.book.city == "ורשה"
    assert result.book.category == "הלכה"


def test_english_title_accepted_on_detail_page():
    """A Sefer Detail marker means it is a book page, whatever the script."""
    html = ("<html><head><title>HebrewBooks.org Sefer Detail: "
            "Hebrew fragment 12 --</title></head><body></body></html>")
    book = parse(42142, html)
    assert book is not None
    assert book.title == "Hebrew fragment 12"


def test_table_author_overrides_title_author():
    html = (
        "<html><head><title>HebrewBooks.org Sefer Detail: "
        "זהר -- רשב\"י</title></head><body>"
        "<table><tr><td>מחבר:</td><td>שמעון בן יוחאי</td></tr>"
        "</table></body></html>"
    )
    book = parse(2865, html)
    assert book.author == "שמעון בן יוחאי"


def test_legacy_template_without_detail_marker():
    html = ("<html><head><title>מסילת ישרים - HebrewBooks</title></head>"
            "<body><h1>מסילת ישרים</h1><h2>רבי משה חיים לוצאטו</h2>"
            "</body></html>")
    book = parse(8774, html)
    assert book is not None
    assert book.title == "מסילת ישרים", f"site chrome not stripped: {book.title}"
    assert book.author == "רבי משה חיים לוצאטו"


def test_hyphenated_title_not_truncated_by_chrome_stripper():
    html = ("<html><head><title>העיטור - א</title></head>"
            "<body></body></html>")
    assert parse(20515, html).title == "העיטור - א"


def test_dead_pages_rejected():
    cases = {
        "home": "<html><head><title>HebrewBooks.org Home Page</title></head></html>",
        "brand": "<html><head><title>hebrewbooks.org</title></head></html>",
        "404": "<html><head><title>404 - Not Found</title></head></html>",
    }
    for label, html in cases.items():
        assert parse(1, html) is None, f"{label} wrongly accepted"
    assert parse(1, "") is None


def test_challenge_page_is_blocked_not_no_title():
    html = ("<html><head><title>Just a moment...</title></head>"
            "<body><div id='cf-browser-verification'></div></body></html>")
    result = inspect(1, html)
    assert result.reason is Reject.BLOCKED_PAGE
    assert looks_blocked(html)


# ---------------------------------------------------------------- search


def test_all_abbreviation_spellings_match():
    _catalog()
    from backend import search

    for spelling in ['רמב"ם', "רמב״ם", "רמבם", "הרמבם"]:
        result = search.search(spelling)
        assert result["total"] == 1, f"{spelling!r} found {result['total']}"
        assert result["books"][0]["id"] == 14763


def test_prefix_match_while_typing():
    _catalog()
    from backend import search

    for partial in ["מס", "מסי", "מסילת"]:
        assert search.search(partial)["total"] == 1


def test_tokens_are_anded_not_ored():
    _catalog()
    from backend import search

    assert search.search("שולחן ערוך")["total"] == 1
    # both tokens must be present — this pairing exists in no single record
    assert search.search("שולחן מסילת")["total"] == 0


def test_no_over_matching_from_expansion():
    _catalog()
    from backend import search

    # ב is never stripped, so רכות must not reach ברכות
    assert search.search("רכות")["total"] == 0
    assert search.search("ברכות")["total"] == 1


def test_fts_operators_in_user_input_are_inert():
    _catalog()
    from backend import search

    for hostile in ['"', "* OR *", 'ברכות" OR "', "^NEAR(a b)", ")(", "-- drop", "*"]:
        search.search(hostile)  # must not raise fts5: syntax error


def test_category_filter():
    _catalog()
    from backend import search

    assert search.search("", category="הלכה")["total"] == 2
    assert search.search("", category="מוסר")["total"] == 1


def test_empty_query_browses_everything():
    _catalog()
    from backend import search

    assert search.search("")["total"] == len(FIXTURES)


def test_book_lookup_and_related():
    _catalog()
    from backend import search

    assert search.get_book(14763)["title"] == "משנה תורה"
    assert search.get_book(999999) is None
    assert isinstance(search.related(9780), list)


# ------------------------------------------------------------------ main

if __name__ == "__main__":
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0

    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")

    print(f"\n  {len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
