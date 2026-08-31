"""
Hebrew text normalization shared by the indexer and the backend.

Both sides MUST normalize identically, otherwise a query will never match
what was written into the FTS index.

Why this exists
---------------
Hebrew titles are full of abbreviations written with geresh/gershayim:
    רמב"ם   רמב״ם   רמבם
Those are three spellings of one word. SQLite's unicode61 tokenizer treats
the quote marks as token separators, so "רמב״ם" would be indexed as two
tokens (רמב, ם) and a search for "רמבם" would never hit it.

So we strip all geresh/gershayim/quote variants before indexing AND before
searching. Everything collapses to רמבם and all three spellings match.

Nikud (vowel points) and cantillation are stripped here too, rather than
relying on `remove_diacritics`, so the behaviour is explicit and testable.
"""

import re
import unicodedata

# Geresh, gershayim, and every ASCII/typographic quote that stands in for them.
_QUOTE_MARKS = "\u05f3\u05f4'\"\u2018\u2019\u201c\u201d`\u00b4"

# Hebrew points (nikud), cantillation marks (te'amim), and the meteg.
_HEBREW_MARKS = re.compile(r"[\u0591-\u05c7]")

# Maqaf (Hebrew hyphen) and the usual dash family act as word separators.
_SEPARATORS = re.compile(r"[\u05be\-\u2010-\u2015_/\\|,.;:()\[\]{}<>]+")

_WHITESPACE = re.compile(r"\s+")

# Prefixes that Hebrew glues onto the front of a word. Not stripped by
# default — stripping them is lossy — but exposed for callers that want
# looser matching.
HEBREW_PREFIXES = ("ה", "ו", "ב", "ל", "כ", "מ", "ש")


def normalize(text: str) -> str:
    """
    Fold a Hebrew string into its searchable form.

    >>> normalize('שו"ת חתם סופר')
    'שות חתם סופר'
    >>> normalize('רמב״ם')
    'רמבם'
    >>> normalize('בְּרֵאשִׁית')
    'בראשית'
    """
    if not text:
        return ""

    # Compose first so precomposed and decomposed forms behave the same.
    text = unicodedata.normalize("NFKC", text)
    text = _HEBREW_MARKS.sub("", text)

    for mark in _QUOTE_MARKS:
        text = text.replace(mark, "")

    text = _SEPARATORS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip().lower()


def tokenize(text: str) -> list[str]:
    """Normalized whitespace-delimited tokens. Empty tokens are dropped."""
    normalized = normalize(text)
    return [token for token in normalized.split(" ") if token]


def is_hebrew(text: str) -> bool:
    """True if the string contains at least one Hebrew letter."""
    return any("\u05d0" <= char <= "\u05ea" for char in text)


def strip_prefix(word: str) -> str:
    """
    Drop a leading definite article or conjunction.

    Only ה and ו are stripped, not the full HEBREW_PREFIXES set. The others
    (ב ל כ מ ש) start too many ordinary words — stripping ב from ברכות gives
    רכות, and every search for רכות would then surface ברכות. ה and ו carry
    most of the benefit (הרמב״ם, הרמח״ל, ורשב״א) at almost none of the cost.

    The remainder must be at least 3 letters, so ה alone or הוא survive intact.
    """
    if len(word) >= 4 and word[0] in ("ה", "ו"):
        return word[1:]
    return word


def expand(text: str) -> str:
    """
    Index-time form: normalized tokens plus their prefix-stripped variants.

    Written into books_fts so that a query for רמבם reaches the stored
    author הרמבם. Doing the expansion here rather than at query time means
    one index lookup instead of an OR across variants — the search path
    stays a single MATCH.

    >>> expand('הרמב״ם')
    'הרמבם רמבם'
    """
    tokens = tokenize(text)
    expanded = list(tokens)

    for token in tokens:
        stripped = strip_prefix(token)
        if stripped != token and stripped not in expanded:
            expanded.append(stripped)

    return " ".join(expanded)
