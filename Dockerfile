# Ganzach on Koyeb.
#
# Docker rather than the buildpack, for three reasons that matter on a
# 0.1 vCPU instance:
#
#   Startup time. A buildpack image carries a full build toolchain. This
#   one carries the interpreter and six wheels. Less to pull, less to
#   page in, and on a tenth of a core the difference is seconds.
#
#   The catalog. data/books.db must be in the image — Koyeb Free
#   Instances cannot attach Volumes, so there is nowhere else to put it.
#   Docker makes that explicit instead of hoping the buildpack copies it.
#
#   Reproducibility. The Python version, the install flags, and the
#   command are all in the repo, not in a web form that someone edits
#   at 2am and cannot diff.

# ── build ───────────────────────────────────────────────────────────────
FROM python:3.13-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .

# Wheels into a venv the runtime stage copies wholesale. curl_cffi and
# lxml both ship manylinux wheels, so no compiler is needed here — if a
# build ever starts compiling, that is the signal a wheel went missing
# for the target platform.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ── runtime ─────────────────────────────────────────────────────────────
FROM python:3.13-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GANZACH_DB=/app/data/books.db \
    PORT=8000

COPY --from=build /opt/venv /opt/venv

WORKDIR /app

# Ordered by volatility: dependencies change rarely, the catalog changes
# when re-crawled, the code changes constantly. Later layers invalidate
# fewer cached ones.
COPY common/   ./common/
COPY backend/  ./backend/
COPY frontend/ ./frontend/
COPY data/     ./data/

# Fail the build, not the deploy, when the catalog is missing. Without
# this the image builds fine and every request 500s in production —
# which is exactly how the Render deploy broke.
RUN test -s data/books.db || (echo "\
ERROR: data/books.db is missing or empty.\n\
Koyeb Free Instances have no persistent volume, so the catalog must be\n\
in the image. Build it and commit it:\n\
    python -m indexer.run\n\
    git add -f data/books.db && git commit -m 'Add catalog' && git push\n\
" >&2; exit 1)

# Non-root. Koyeb does not require it; it costs nothing and means a
# path-traversal bug in static file serving cannot reach anything.
RUN useradd --create-home --uid 10001 ganzach \
 && chown -R ganzach:ganzach /app
USER ganzach

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health', timeout=4).status==200 else 1)"

# One worker. On 0.1 vCPU a second worker cannot run in parallel — it
# only duplicates ~60 MB of interpreter and its own SQLite page cache
# inside a 512 MB budget. FastAPI already runs sync endpoints in a
# thread pool, which is the concurrency that actually helps here.
#
# ${PORT:-8000} rather than a literal, so the container is correct
# whether or not the platform injects PORT.
CMD ["sh", "-c", "exec uvicorn backend.main:app \
--host 0.0.0.0 --port ${PORT:-8000} \
--workers 1 --no-access-log --timeout-keep-alive 65"]
