# Ganzach

A catalog of Hebrew seforim. **Search runs locally and offline. Reading streams from the source.**

No PDFs are downloaded, stored, or redistributed. The database holds metadata only — around 25 MB for 130,000 entries — so search returns in single-digit milliseconds with the network unplugged. When a reader opens a sefer, the file is served by hebrewbooks.org.

---

## How it fits together

```
indexer/          one-time crawl        →  data/books.db
                  asyncio + curl_cffi      (id, title, author,
                                             year, city, category)
                                                    │
backend/          FastAPI, read-only  ←─────────────┘
                  SQLite FTS5
                       │
frontend/         instant search  ──── reads from ────→ hebrewbooks.org
                  + PDF viewer
```

The indexer is a separate program, not a background thread inside the server. That is the main structural decision here: a crawl that fails cannot take the web server down with it, and the server has no startup dependency on the network.

```
ganzach/
├── common/hebrew.py       normalization shared by both sides
├── indexer/
│   ├── schema.sql         tables + FTS5 virtual table
│   ├── parser.py          HTML → Book
│   ├── scraper.py         concurrent fetch, batched writes, resume
│   └── run.py             CLI
├── backend/
│   ├── db.py              read-only, thread-local connections
│   ├── search.py          FTS5 query builder, bm25 ranking
│   ├── routes.py          /api/*
│   └── main.py            app + static hosting
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   └── js/{search,viewer}.js
└── data/books.db          built by the indexer, shipped to users
```

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 1. Build the catalog

```bash
python -m indexer.run
```

IDs 1–65,000 at the default 8 concurrent requests takes roughly 60–90 minutes depending on the connection. Progress is written to the database as it goes, so `Ctrl+C` is safe — run the same command again and it resumes from the checkpoint.

```bash
python -m indexer.run --probe                 # can this machine reach the site?
python -m indexer.run --start 1 --end 2000    # a slice, to try it out
python -m indexer.run --concurrency 16        # faster, less polite
python -m indexer.run --stats                 # what's in there now
```

### 2. Run the server

```bash
python -m backend.main
# or: uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000`.

---

## Why it's fast

**FTS5, not `LIKE`.** `WHERE title LIKE '%ברכות%'` cannot use an index and scans all 130,000 rows. The FTS5 inverted index turns the same search into a term lookup — typically 1–4 ms, which the UI displays next to the result count rather than claiming in a README.

**Hebrew is normalized on both sides.** `רמב"ם`, `רמב״ם`, and `רמבם` are the same word written three ways. `common/hebrew.py` strips geresh, gershayim, quote marks, and nikud before text enters the index *and* before a query leaves the search box, so all three spellings match. Both sides import the same function; if you change one, you change both.

**bm25 weighted toward the title.** Someone typing "ברכות" wants the sefer named ברכות first, not twelve seforim by an author whose name contains it. Weights are `title 10 / author 4 / category 1` in `backend/search.py`.

**Read-only, per-thread connections.** The catalog opens with `mode=ro` and `query_only`, so a bug in a route cannot corrupt it and SQLite skips journal setup. FastAPI runs sync endpoints in a thread pool, and each thread keeps one connection for the process lifetime — no pool, no lock, no `check_same_thread` workaround.

**User input never becomes FTS syntax.** `*`, `"`, `^`, `-`, and `NEAR` all mean something to FTS5, so a quote mark pasted into the search box would otherwise raise `fts5: syntax error`. `build_match()` tokenizes the input and rebuilds a quoted expression from scratch.

---

## When the crawl indexes nothing

Two commands, in this order.

```bash
python -m indexer.run --probe          # can this machine reach the site?
python -m indexer.run --inspect 14763  # what does the parser see on one page?
```

`--probe` tries several TLS fingerprints against known-good IDs. `--inspect`
fetches a single page and prints every title candidate, every table field it
found, which strategy matched, and the final verdict:

```
  id 14763
  bytes            18,204
  → <title>        HebrewBooks.org Sefer Detail: משנה תורה -- משה בן מימון
  table fields
      city       ורשה
      year       תק"פ
      category   הלכה
  strategy         sefer-detail
  RESULT           ok
```

Use `--inspect` whenever requests succeed but nothing is indexed. Aggregate
counters cannot tell you *which* check rejected a page; this can.

It fetches the homepage plus five IDs known to hold real seforim, and prints
the status, byte count, and parsed title for each. Five titles means the
crawler will work from this machine. Zero means the IP is being refused.

**Exit codes.** A crawl that indexed nothing used to return `0`, which CI
renders as a green tick — the failure mode that hides a broken run.

| Code | Meaning |
|---|---|
| `0` | Crawl completed |
| `2` | Aborted — the circuit breaker fired |
| `3` | Ran to completion but indexed 0 seforim |
| `4` | More than 25% of requests were refused; the catalog is incomplete |
| `5` | JavaScript challenges encountered; try another `--impersonate` profile |

**Reading the summary.** Every ID lands in exactly one bucket, printed at
the end:

```
  outcome by reason
    ok            11,240   74.9%
    http-404       3,760   25.1%
```

### The page format

Every detail page brands its own `<title>`:

```
HebrewBooks.org Sefer Detail: {title} -- {author}
```

Three consequences the parser has to respect, each of which broke an
earlier version:

- **The site name is inside every valid title.** A dead-page check that
  substring-matches `hebrewbooks.org` therefore rejects the entire
  catalogue as `dead-title`. Dead pages are identified by the *absence* of
  the `Sefer Detail:` marker, never by the presence of the brand.
- **Titles contain single hyphens** — `העיטור - א`, `פרדס יוסף-בראשית`. The
  title/author separator is `--` specifically, so splitting on `-`
  truncates a large share of the catalogue mid-title.
- **Not every title is Hebrew.** `Hebrew fragment 12` is a real record.
  Requiring Hebrew discards it, so that test applies only to the fallback
  path, where it is less certain the page is a book page at all.

A healthy crawl of HebrewBooks is mostly `ok` and `http-404` — the ID range
is sparse, so a quarter to a third being empty is normal. Any of
`http-403`, `http-429`, `blocked-page`, or `challenge` appearing in volume
means you are being refused, and the crawl stops on its own rather than
continue. `challenge` specifically means a JS challenge got through the TLS
layer, which is the one case a different `--impersonate` profile may fix.

**Cloudflare fingerprints TLS, not headers.** Every HTTP client has a
characteristic ClientHello — cipher order, extension order, supported
curves, ALPN — plus a characteristic HTTP/2 SETTINGS frame. Python's `ssl`
module produces a shape nothing like Chrome's, so `aiohttp` and `requests`
are identifiable as bots before sending a single header. Perfect headers do
not help; the decision is already made.

The crawler therefore uses **`curl_cffi`**, bound to curl-impersonate — a
libcurl patched to reproduce a real browser's handshake byte for byte.
`--impersonate chrome131` makes the connection indistinguishable at that
layer. Which profile works is an empirical question, so `--probe` tries
several and prints a table:

```
  profile                home   seforim   note
  -------------------- ------  --------   --------------------
  chrome131               200       5/5
  chrome136               200       5/5
  safari184               403       0/5   just a moment
```

Then crawl with whichever got through: `--impersonate chrome136`.

Do **not** add your own `User-Agent` or `sec-ch-ua` headers. The profile
already supplies the full browser set in the browser's order, with a
`sec-ch-ua` version matching the TLS fingerprint. Overriding it with
`v="122"` above a Chrome 131 handshake is its own detection signal.

**What curl_cffi cannot do is run JavaScript.** Cloudflare's *managed*
challenge requires executing JS to earn a `cf_clearance` cookie. Most
challenges fire because of a fingerprint mismatch, so a correct fingerprint
means the real page is served and no challenge appears — but if one still
does, it is recorded as `challenge` rather than `blocked-page`, because the
remedies differ. `--probe` prints them.

**GitHub Actions will probably be blocked regardless.** Hosted runners use
Azure datacenter IP ranges that sit on most WAF blocklists, and that is an
IP decision no fingerprint changes. The intended path is to crawl once
locally and commit `data/books.db`; the workflow probes before crawling so
a blocked runner fails in fifteen seconds instead of burning a five-hour
job.

**Being refused mid-crawl is not permanent.** Only confirmed empties
(`http-404`, `dead-title`) are recorded in `crawl_misses`. A 403 or a
timeout stays retryable, so re-running after a block picks those IDs up
again rather than treating them as gaps.

---

## Reading a sefer

The reader tries three things in order:

1. **`<iframe>` of `pdfpager.aspx`** — their viewer, framed. Nothing passes through your server.
2. **`<object>` of `/api/read/{id}`** — the backend streams the PDF through so it becomes same-origin and the browser's own PDF viewer handles it. Bytes are piped, never written to disk.
3. **A link out**, with a plain explanation.

Which of the first two is attempted is decided by the **server**, at `/api/capabilities`. This matters: when a site sends `X-Frame-Options: SAMEORIGIN`, the parent page receives no error event — just a blank frame. JavaScript cannot detect it. So the backend requests the page once, reads the headers, caches the answer, and tells the client which viewer to build.

**Check this before building anything on top of it:**

```bash
curl -sI "https://hebrewbooks.org/pdfpager.aspx?req=14763" | grep -i -E "x-frame|content-security"
```

Empty output means framing is allowed and path 1 works. Otherwise the proxy carries the load — set `GANZACH_PROXY=0` to turn it off and fall back to opening a new tab.

---

## API

Everything except the last two works with the machine offline.

| Endpoint | Purpose |
|---|---|
| `GET /api/search?q=&category=&limit=&offset=` | Full search. Returns `books`, `total`, `took_ms`. |
| `GET /api/suggest?q=&limit=` | Autocomplete. No `COUNT(*)`, small `LIMIT`. |
| `GET /api/book/{id}` | One sefer, plus viewer URLs and related seforim. |
| `GET /api/categories` | Category list with counts. |
| `GET /api/stats` | Row count, file size, categories. |
| `GET /api/health` | `503` when the catalog is missing. |
| `GET /api/capabilities` | Whether the upstream permits framing. *Network.* |
| `GET /api/read/{id}` | Streams the PDF through. *Network.* |

Interactive docs at `/docs`.

---

## Interface

The reference object is a beis-medrash card catalog — buckram drawer, card stock, a shelf mark stamped in the corner. Red is rubric red, the second ink of Hebrew letterpress, so it appears on exactly two things: the matched substring and the timing readout. Display type is **Frank Ruhl Libre**, cut for Hebrew book printing; body is **Assistant**; IDs are mono because the HebrewBooks `req` ID *is* the shelf mark the viewer resolves.

`/` focuses the search field. `Esc` closes the reader.

---

## Deploying

The backend serves the frontend, so one service hosts everything.

**The catalog must be committed.** Render's free tier has no persistent
disk. If `data/books.db` is not in the repo, `/api/health` returns 503 and
every search returns 500 — the site loads and finds nothing. `.gitignore`
ignores only the `-wal`/`-shm` sidecars for this reason.

```bash
python -m indexer.run          # build it locally, once
git add -f data/books.db       # ~25 MB for 130k rows, well under GitHub's limit
git commit -m "Add catalog"
git push
```

### Koyeb

`koyeb.yaml` and `Dockerfile` cover it; in the Web UI, pick **Docker** as
the builder and the rest comes from the repo. If configuring by hand:

| Setting | Value |
|---|---|
| Builder | Dockerfile |
| Build command | *(none — the Dockerfile handles it)* |
| Run command | *(none — `CMD` handles it)* |
| Port | `8000`, protocol `http` |
| Health check | HTTP, path `/api/health` |
| Region | Frankfurt or Washington DC (free tier allows no others) |

Using the buildpack instead of Docker: build command
`pip install -r requirements.txt`, run command
`uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1`.
`.python-version` pins the interpreter and `Procfile` carries the same
command.

**The free instance is not always-on.** It scales to zero after 1 hour
idle and that cannot be disabled. The gain over Render is the wake time —
1–5s versus 30–60s — not the absence of sleeping. An `eco-nano` instance
($1.61/mo) can disable scale-to-zero if you need genuinely always-on.

**512 MB RAM, 0.1 vCPU, no Volumes.** One worker only: a second cannot run
in parallel on a tenth of a core and just duplicates ~60 MB of
interpreter. No Volumes means `data/books.db` must live inside the image,
which the Dockerfile checks for at build time.

### Render

`render.yaml` sets the start command, health check and env vars. Or
configure by hand:

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn backend.main:app --host 0.0.0.0 --port $PORT` |
| Health check | `/api/health` |
| `GANZACH_DB` | `data/books.db` |

`--host 0.0.0.0` and `$PORT` are both required; binding to `127.0.0.1` or a
fixed port makes the service unreachable and Render will fail the health
check.

**Free instances sleep after ~15 minutes idle** and take 30–60s to wake.
`frontend/js/config.js` allows for that with a 75s timeout on the first
request and a "מעיר את השרת" message, so a cold start looks like waiting
rather than breaking.

### Where the frontend points

`frontend/js/config.js` resolves the API base once at boot:

1. `window.GANZACH_API`, if set — explicit override
2. `?api=https://…` — for quick testing
3. the current origin, if it answers `/api/health` — the normal case
4. `https://ganzach.onrender.com` — the fallback

So the same build works served by the backend, hosted separately on a
static host, or opened as a `file://` double-click, with nothing to
reconfigure. Hardcoding an absolute URL would have broken local
development, which is why there was no such variable to begin with.

---

## Packaging as a desktop app

The server already hosts the frontend, so an Electron shell is a `BrowserWindow` pointed at `http://127.0.0.1:8000` with the Python process spawned alongside it. Ship `data/books.db` with the app — users should never run the crawler.

For a single binary:

```bash
pyinstaller --onefile --add-data "frontend:frontend" \
            --add-data "data/books.db:data" backend/main.py
```

---

## Notes

**Be polite to the source.** Default concurrency is 16 with retry backoff. `--concurrency 64` will finish sooner and may get you blocked. `--delay 0.1` adds a pause per worker if you want to be gentler still.

**Rebuilding.** The crawler skips IDs already recorded as a hit or a miss. To re-fetch a range, delete those rows or pass `--no-resume`.

**Extending the range.** `--end 65000` covers the bulk of the collection. Raise it if the catalog has grown; the checkpoint keeps the additional crawl incremental.
