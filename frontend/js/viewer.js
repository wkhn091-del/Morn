/* ══════════════════════════════════════════════════════════
   Reader

   Three ways to show a sefer, tried in order:

     1. iframe of hebrewbooks.org/pdfpager.aspx
        Cheapest and best — their own paged viewer, nothing
        passes through our server.

     2. <embed> of /api/read/{id}
        Used when the upstream sends X-Frame-Options. Our backend
        streams the PDF through, so it's same-origin and the
        browser's built-in PDF viewer renders it.

     3. A link out.
        When both fail, say so plainly and hand over the URL.

   Which of 1 and 2 to try is decided by the server at
   /api/capabilities — a framing block is invisible to JavaScript,
   so the client cannot work this out for itself.
   ══════════════════════════════════════════════════════════ */

const Reader = (() => {
  const el = {
    root:     document.getElementById('reader'),
    stage:    document.getElementById('reader-stage'),
    title:    document.getElementById('reader-title'),
    sub:      document.getElementById('reader-sub'),
    close:    document.getElementById('reader-close'),
    external: document.getElementById('reader-external'),
  };

  let capabilities = null;
  let lastFocused = null;

  async function loadCapabilities() {
    if (capabilities) return capabilities;
    try {
      const response = await Api.fetch('/api/capabilities');
      capabilities = await response.json();
    } catch {
      capabilities = { iframe: true, proxy: true, reason: 'probe failed' };
    }
    return capabilities;
  }

  function clearStage() {
    el.stage.innerHTML =
      '<div class="reader__loading">' +
      '<span class="spinner" aria-hidden="true"></span>' +
      'טוען מהשרת של HebrewBooks</div>';
  }

  function mount(node) {
    el.stage.innerHTML = '';
    el.stage.appendChild(node);
  }

  function showFallback(book) {
    const wrap = document.createElement('div');
    wrap.className = 'reader__blocked';
    wrap.innerHTML = `
      <strong>הספר לא נפתח כאן</strong>
      <p>
        השרת של HebrewBooks לא מאפשר להציג את הספר בתוך האתר.
        הקטלוג והחיפוש ממשיכים לעבוד כרגיל.
      </p>
      <div class="reader__tools">
        <a class="btn btn--solid" href="${book.viewer_url}" target="_blank" rel="noopener">
          פתח ב-HebrewBooks
        </a>
      </div>`;
    mount(wrap);
  }

  /* The upstream viewer, framed. */
  function tryIframe(book) {
    return new Promise((resolve) => {
      const frame = document.createElement('iframe');
      frame.src = book.viewer_url;
      frame.title = book.title;
      frame.referrerPolicy = 'no-referrer';
      frame.setAttribute('loading', 'eager');

      /* A blocked frame fires no error event, so treat silence as failure. */
      const timer = setTimeout(() => resolve(false), 4000);
      frame.addEventListener('load', () => {
        clearTimeout(timer);
        resolve(true);
      });
      frame.addEventListener('error', () => {
        clearTimeout(timer);
        resolve(false);
      });

      mount(frame);
    });
  }

  /* Our own stream, rendered by the browser's PDF plugin. */
  async function tryProxy(book) {
    const proxyUrl = await Api.url(book.proxy_url);
    return new Promise((resolve) => {
      const object = document.createElement('object');
      object.data = proxyUrl;
      object.type = 'application/pdf';

      const timer = setTimeout(() => resolve(true), 2500);
      object.addEventListener('error', () => {
        clearTimeout(timer);
        resolve(false);
      });

      mount(object);
    });
  }

  async function open(book) {
    lastFocused = document.activeElement;

    el.title.textContent = book.title;
    el.sub.textContent = [book.author, book.year, book.category]
      .filter(Boolean)
      .join(' · ') || `מזהה ${book.id}`;
    el.external.href = book.viewer_url;

    el.root.hidden = false;
    document.body.style.overflow = 'hidden';
    el.close.focus();
    clearStage();

    const caps = await loadCapabilities();

    if (caps.iframe && await tryIframe(book)) return;
    if (caps.proxy && book.proxy_url && await tryProxy(book)) return;

    showFallback(book);
  }

  function close() {
    el.root.hidden = true;
    el.stage.innerHTML = '';
    document.body.style.overflow = '';
    if (lastFocused) lastFocused.focus();
  }

  el.close.addEventListener('click', close);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !el.root.hidden) close();
  });

  return { open, close, loadCapabilities };
})();
