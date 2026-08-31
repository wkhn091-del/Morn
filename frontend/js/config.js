/* ══════════════════════════════════════════════════════════
   API base

   The backend serves this page (backend/main.py mounts
   frontend/ as static), so relative paths already resolve to
   whatever host is serving — localhost in development,
   ganzach.onrender.com in production. Hardcoding an absolute
   URL would break local development for no gain.

   But the frontend can also be hosted separately (GitHub
   Pages, Netlify, an Electron shell, a file:// double-click),
   and then relative paths point at nothing. So the base is
   resolved once at boot:

     1. window.GANZACH_API, if set          — explicit override
     2. ?api=https://...                    — for quick testing
     3. same origin, if it answers /api/health
     4. REMOTE                              — the Render deploy

   Step 3 is one cheap request. It fails instantly when this
   page is on a static host, so the cost is nil in the case
   where it matters.

   ── Render free tier ──
   Instances spin down after ~15 minutes idle, and the next
   request takes 30–60s while it wakes. Nothing here can make
   that faster, but the UI can stop looking broken while it
   happens: requests get a long timeout and fire `onWaking`
   once a call passes SLOW_MS.
   ══════════════════════════════════════════════════════════ */

const Api = (() => {
  const REMOTE = 'https://ganzach.onrender.com';

  const TIMEOUT_MS = 15000;   // steady state
  const COLD_MS    = 75000;   // first call, cold Render instance
  const SLOW_MS    = 2500;    // past this, tell the user it's waking

  let base = null;            // resolved once, then cached
  let resolving = null;
  let firstCallDone = false;
  const wakingHandlers = [];

  const strip = (url) => String(url).replace(/\/+$/, '');

  function announceWaking() {
    wakingHandlers.forEach((fn) => {
      try { fn(); } catch { /* a bad listener must not break fetching */ }
    });
  }

  /* Does the origin serving this page also serve the API?
     503 counts as yes — the backend is there, it just has no
     catalog yet, which is a different problem with a different
     message. */
  async function originServesApi() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch('/api/health', { signal: controller.signal });
      return response.ok || response.status === 503;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  async function resolve() {
    if (base !== null) return base;
    if (resolving) return resolving;

    resolving = (async () => {
      if (window.GANZACH_API) return strip(window.GANZACH_API);

      const override = new URLSearchParams(location.search).get('api');
      if (override) return strip(override);

      // No origin to be relative to.
      if (location.protocol === 'file:') return REMOTE;

      return (await originServesApi()) ? '' : REMOTE;
    })();

    base = await resolving;
    return base;
  }

  /* fetch() against the API, with a timeout and cold-start awareness. */
  async function call(path, options = {}) {
    const prefix = await resolve();
    const limit = firstCallDone ? TIMEOUT_MS : COLD_MS;

    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), limit);
    const slowTimer = setTimeout(announceWaking, SLOW_MS);

    try {
      return await fetch(prefix + path, {
        ...options,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(abortTimer);
      clearTimeout(slowTimer);
      firstCallDone = true;
    }
  }

  return {
    fetch: call,
    resolve,
    onWaking: (fn) => wakingHandlers.push(fn),
    /* Absolute URL for a path — for <a href> and <object data>,
       which cannot go through fetch(). */
    url: async (path) => (await resolve()) + path,
    get base() { return base; },
    REMOTE,
  };
})();
