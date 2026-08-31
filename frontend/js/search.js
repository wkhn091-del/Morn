/* ══════════════════════════════════════════════════════════
   Search

   Every keystroke hits /api/search, which reads a local SQLite
   FTS5 index. There is no network round trip to anywhere else,
   which is why the elapsed time is worth putting on screen.

   Two things guard the input: a 90ms debounce so a fast typist
   doesn't queue twenty requests, and a monotonic request counter
   so a slow response can never overwrite a newer one.
   ══════════════════════════════════════════════════════════ */

(() => {
  const PAGE_SIZE = 30;
  const DEBOUNCE_MS = 90;

  const el = {
    input:   document.getElementById('q'),
    clear:   document.getElementById('clear'),
    chips:   document.getElementById('chips'),
    readout: document.getElementById('readout'),
    results: document.getElementById('results'),
    more:    document.getElementById('more'),
    tally:   document.getElementById('tally'),
    dot:     document.getElementById('conn-dot'),
  };

  const state = {
    query: '',
    category: '',
    offset: 0,
    total: 0,
    sequence: 0,
    books: [],
  };

  const nf = new Intl.NumberFormat('he-IL');

  /* ── escaping ─────────────────────────────────────────── */

  const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const escapeHtml = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (char) => ESCAPES[char]);

  /* ── highlighting ─────────────────────────────────────────
     Mirrors common/hebrew.normalize on the client so the marked
     span lines up with what the index actually matched: strip
     geresh/gershayim and nikud, then find each token.          */

  const MARKS = /[\u0591-\u05C7]/g;
  const QUOTES = /['"\u05F3\u05F4\u2018\u2019\u201C\u201D`]/g;

  const fold = (text) =>
    String(text ?? '').replace(MARKS, '').replace(QUOTES, '').toLowerCase();

  function highlight(text) {
    const source = String(text ?? '');
    if (!state.query.trim()) return escapeHtml(source);

    const tokens = fold(state.query).split(/\s+/).filter(Boolean);
    if (!tokens.length) return escapeHtml(source);

    /* Fold each character independently so indices stay aligned
       with the original string. Combining marks fold to ''.     */
    const folded = [...source].map(fold);
    const flat = folded.join('');

    /* Map a position in `flat` back to an index in `source`. */
    const map = [];
    folded.forEach((piece, index) => {
      for (let i = 0; i < piece.length; i += 1) map.push(index);
    });

    const ranges = [];
    for (const token of tokens) {
      let from = 0;
      for (;;) {
        const at = flat.indexOf(token, from);
        if (at === -1) break;
        ranges.push([map[at], (map[at + token.length - 1] ?? map[map.length - 1]) + 1]);
        from = at + token.length;
      }
    }

    if (!ranges.length) return escapeHtml(source);

    ranges.sort((a, b) => a[0] - b[0]);
    const merged = [ranges[0]];
    for (const [start, end] of ranges.slice(1)) {
      const last = merged[merged.length - 1];
      if (start <= last[1]) last[1] = Math.max(last[1], end);
      else merged.push([start, end]);
    }

    const chars = [...source];
    let out = '';
    let cursor = 0;
    for (const [start, end] of merged) {
      out += escapeHtml(chars.slice(cursor, start).join(''));
      out += '<mark>' + escapeHtml(chars.slice(start, end).join('')) + '</mark>';
      cursor = end;
    }
    out += escapeHtml(chars.slice(cursor).join(''));
    return out;
  }

  /* ── rendering ────────────────────────────────────────── */

  function cardHtml(book) {
    const line = [book.author, book.year, book.city, book.category]
      .filter(Boolean)
      .map((part) => highlight(part))
      .join('<span class="card__sep">·</span>');

    return `
      <button class="card" type="button" data-id="${book.id}">
        <span class="card__mark">${book.id}</span>
        <h3 class="card__title">${highlight(book.title)}</h3>
        ${line ? `<p class="card__line">${line}</p>` : ''}
      </button>`;
  }

  function renderReadout(payload) {
    if (payload.error) {
      el.readout.innerHTML =
        `<span class="readout__count" style="color:var(--rubric)">${escapeHtml(payload.error)}</span>`;
      return;
    }
    const count = nf.format(payload.total);
    const noun = payload.total === 1 ? 'תוצאה' : 'תוצאות';
    el.readout.innerHTML =
      `<span class="readout__count">${count} ${noun}</span>` +
      `<span class="readout__ms">${payload.took_ms} ms</span>`;
  }

  function renderEmpty() {
    const term = escapeHtml(state.query);
    el.results.innerHTML = `
      <div class="note">
        <strong>אין ספר בשם הזה במפתח</strong>
        ${term ? `נסה איות אחר, או חפש לפי שם המחבר.` : 'התחל להקליד כדי לחפש.'}
      </div>`;
  }

  function renderCatalogMissing() {
    el.results.innerHTML = `
      <div class="note">
        <strong>המפתח עדיין לא נבנה</strong>
        הרץ <code>python -m indexer.run</code> כדי לבנות את הקטלוג,
        ואז רענן את הדף.
      </div>`;
  }

  /* ── fetching ─────────────────────────────────────────── */

  async function run({ append = false } = {}) {
    const ticket = ++state.sequence;
    el.results.setAttribute('aria-busy', 'true');

    const params = new URLSearchParams({
      q: state.query,
      category: state.category,
      limit: String(PAGE_SIZE),
      offset: String(state.offset),
    });

    let payload;
    try {
      const response = await Api.fetch(`/api/search?${params}`);
      if (response.status === 503) {
        if (ticket === state.sequence) renderCatalogMissing();
        return;
      }
      payload = await response.json();
    } catch (err) {
      if (ticket === state.sequence) {
        renderReadout({
          error: err.name === 'AbortError'
            ? 'השרת לא הגיב — נסה שוב'
            : 'השרת לא זמין',
        });
      }
      return;
    } finally {
      el.results.setAttribute('aria-busy', 'false');
    }

    /* A stale response arriving late must not clobber a newer one. */
    if (ticket !== state.sequence) return;

    state.total = payload.total;
    state.books = append ? state.books.concat(payload.books) : payload.books;

    renderReadout(payload);

    if (!state.books.length) {
      renderEmpty();
      el.more.hidden = true;
      return;
    }

    const html = state.books.map(cardHtml).join('');
    el.results.innerHTML = html;
    el.more.hidden = state.books.length >= state.total;
  }

  /* ── chips ────────────────────────────────────────────── */

  async function loadChips() {
    let info;
    try {
      const response = await Api.fetch('/api/stats');
      if (!response.ok) throw new Error('no catalog');
      info = await response.json();
    } catch {
      el.tally.textContent = '—';
      renderCatalogMissing();
      return;
    }

    el.tally.textContent = nf.format(info.total);
    el.dot.dataset.state = 'local';
    el.dot.title = `מפתח מקומי · ${info.size_mb} MB`;

    const chips = [{ name: 'הכל', value: '', count: info.total }].concat(
      info.categories.slice(0, 8).map((c) => ({
        name: c.name, value: c.name, count: c.count,
      })),
    );

    el.chips.innerHTML = chips
      .map(
        (chip) => `
        <button class="chip" type="button"
                data-value="${escapeHtml(chip.value)}"
                aria-pressed="${chip.value === state.category}">
          ${escapeHtml(chip.name)}
          <span class="chip__n">${nf.format(chip.count)}</span>
        </button>`,
      )
      .join('');
  }

  /* ── events ───────────────────────────────────────────── */

  let debounce;

  el.input.addEventListener('input', () => {
    state.query = el.input.value;
    state.offset = 0;
    el.clear.hidden = !state.query;

    clearTimeout(debounce);
    debounce = setTimeout(run, DEBOUNCE_MS);
  });

  el.clear.addEventListener('click', () => {
    el.input.value = '';
    state.query = '';
    state.offset = 0;
    el.clear.hidden = true;
    el.input.focus();
    run();
  });

  el.chips.addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;

    state.category = chip.dataset.value;
    state.offset = 0;

    el.chips.querySelectorAll('.chip').forEach((node) => {
      node.setAttribute('aria-pressed', String(node === chip));
    });
    run();
  });

  el.more.addEventListener('click', () => {
    state.offset += PAGE_SIZE;
    run({ append: true });
  });

  el.results.addEventListener('click', async (event) => {
    const card = event.target.closest('.card');
    if (!card) return;

    try {
      const response = await Api.fetch(`/api/book/${card.dataset.id}`);
      if (!response.ok) return;
      Reader.open(await response.json());
    } catch {
      /* offline and the reader needs the network — nothing to show */
    }
  });

  /* `/` focuses the search box, the way every catalog terminal works. */
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && document.activeElement !== el.input) {
      event.preventDefault();
      el.input.focus();
      el.input.select();
    }
  });

  /* ── boot ─────────────────────────────────────────────── */

  /* Render's free tier sleeps after ~15 minutes idle and takes
     30–60s to wake. Say so, rather than showing a dead screen. */
  Api.onWaking(() => {
    if (el.results.getAttribute('aria-busy') !== 'true') return;
    el.readout.innerHTML =
      '<span class="readout__count">מעיר את השרת…</span>' +
      '<span class="readout__ms">עד דקה בפעם הראשונה</span>';
  });

  loadChips().then(run);
  Reader.loadCapabilities().then((caps) => {
    if (caps.iframe || caps.proxy) el.dot.dataset.state = 'online';
  });
})();
