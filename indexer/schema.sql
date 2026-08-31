-- ============================================================
--  Ganzach — catalog schema
--
--  Two tables carry the whole system:
--    books      display data, one row per sefer
--    books_fts  normalized search text, rowid == books.id
--
--  books_fts is a standalone (not external-content) FTS5 table
--  because what we index is NOT what we display: the indexed text
--  has been folded by common.hebrew.normalize(). External-content
--  mode would force those to be identical.
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

-- ---------- catalog ----------------------------------------

CREATE TABLE IF NOT EXISTS books (
    id        INTEGER PRIMARY KEY,   -- HebrewBooks req ID. This is the shelf mark.
    title     TEXT NOT NULL,
    author    TEXT NOT NULL DEFAULT '',
    year      TEXT NOT NULL DEFAULT '',
    city      TEXT NOT NULL DEFAULT '',
    category  TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT 'hebrewbooks'
);

CREATE INDEX IF NOT EXISTS idx_books_category ON books(category);
CREATE INDEX IF NOT EXISTS idx_books_author   ON books(author);

-- ---------- search index -----------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title,
    author,
    category,
    tokenize = "unicode61 remove_diacritics 2"
);

-- ---------- crawl bookkeeping ------------------------------
-- Kept in the same file so a crawl can resume after a crash
-- without any external state.

CREATE TABLE IF NOT EXISTS crawl_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- IDs that returned 404 / no parsable title. Recorded so a resumed
-- crawl doesn't spend requests re-checking known gaps.
CREATE TABLE IF NOT EXISTS crawl_misses (
    id      INTEGER PRIMARY KEY,
    reason  TEXT NOT NULL DEFAULT '',
    seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
