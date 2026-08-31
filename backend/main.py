#!/usr/bin/env python3
"""
Ganzach — offline catalog search, online reading.

    uvicorn backend.main:app --reload
    python -m backend.main --port 8000

The API is served from /api and the frontend from /. One process, one port,
no build step.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from backend import db  # noqa: E402
from backend.routes import router  # noqa: E402

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Touch the catalog at startup so a missing file is a loud message here
    rather than a 500 on the first search.
    """
    try:
        info = db.catalog_info()
        print(f"  catalog  {info['total']:,} seforim  ({info['size_mb']} MB)")
        print(f"  path     {info['path']}")
    except db.CatalogMissing as exc:
        print(f"\n  !  {exc}\n")
    yield


app = FastAPI(
    title="Ganzach",
    description="Local catalog of Hebrew seforim. Search offline, read online.",
    version="1.0.0",
    lifespan=lifespan,
)

# Open by default: this is meant to run on localhost, and locking it down
# would only get in the way of an Electron shell or a second dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Search responses are JSON full of Hebrew, which json.dumps escapes to
# \uXXXX — six ASCII bytes per character. That compresses extremely well,
# and on a 0.1 vCPU instance the CPU cost is far cheaper than the bytes.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

app.include_router(router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def main() -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="ganzach")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"\n  Ganzach on http://{args.host}:{args.port}\n")
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
