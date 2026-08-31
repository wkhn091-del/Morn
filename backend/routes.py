"""
HTTP surface.

Everything under /api/search, /api/suggest, /api/book is served entirely from
the local SQLite file — no network, no upstream dependency. Those endpoints
work with the machine offline.

/api/read and /api/capabilities are the only places that touch hebrewbooks.org,
and only when a reader actually opens a sefer.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from backend import db, search

router = APIRouter(prefix="/api")

# The page wrapper — an HTML viewer, suitable for an <iframe>.
VIEWER_URL = "https://hebrewbooks.org/pdfpager.aspx?req={id}"

# The raw PDF stream — suitable for PDF.js or the proxy below.
PDF_URL = "https://download.hebrewbooks.org/downloadhandler.ashx?req={id}"

UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://hebrewbooks.org/",
}

# Set GANZACH_PROXY=0 to disable the streaming proxy entirely.
PROXY_ENABLED = os.environ.get("GANZACH_PROXY", "1") != "0"

_capabilities_cache: dict | None = None
_capabilities_lock = asyncio.Lock()


# ------------------------------------------------------------------ search


@router.get("/search")
def api_search(
    q: str = Query("", description="search text"),
    category: str = Query("", description="exact category filter"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return search.search(q=q, category=category, limit=limit, offset=offset)


@router.get("/suggest")
def api_suggest(
    q: str = Query("", min_length=1),
    limit: int = Query(8, ge=1, le=20),
):
    return {"suggestions": search.suggest(q, limit)}


@router.get("/book/{book_id}")
def api_book(book_id: int):
    book = search.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="No sefer with that ID")

    book["viewer_url"] = VIEWER_URL.format(id=book_id)
    book["pdf_url"] = PDF_URL.format(id=book_id)
    book["proxy_url"] = f"/api/read/{book_id}" if PROXY_ENABLED else None
    book["related"] = search.related(book_id)
    return book


@router.get("/stats")
def api_stats():
    return db.catalog_info()


@router.get("/categories")
def api_categories():
    return {"categories": db.catalog_info()["categories"]}


# ------------------------------------------------------------ capabilities


async def _probe_embedding() -> dict:
    """
    Ask hebrewbooks.org once whether it permits being framed.

    You cannot detect an X-Frame-Options block from JavaScript — the parent
    page gets no event, just a blank frame. So the server checks the headers
    and tells the client which viewer to build. This is cached for the
    process lifetime; it changes about as often as the site is redesigned.
    """
    result = {"iframe": True, "reason": "assumed", "proxy": PROXY_ENABLED}

    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            response = await client.get(
                VIEWER_URL.format(id=1), headers=UPSTREAM_HEADERS
            )
    except httpx.HTTPError as exc:
        result.update(iframe=False, reason=f"unreachable: {type(exc).__name__}")
        return result

    # A non-200 means we reached something, but not necessarily HebrewBooks —
    # a captive portal or corporate proxy will happily return a page with no
    # framing headers, which would otherwise read as "framing is fine".
    if response.status_code != 200:
        result.update(
            iframe=False, reason=f"probe returned {response.status_code}"
        )
        return result

    xfo = response.headers.get("x-frame-options", "").lower()
    csp = response.headers.get("content-security-policy", "").lower()

    if "deny" in xfo or "sameorigin" in xfo:
        result.update(iframe=False, reason=f"x-frame-options: {xfo}")
    elif "frame-ancestors" in csp and "*" not in csp:
        result.update(iframe=False, reason="csp frame-ancestors")
    else:
        result.update(iframe=True, reason="no framing restriction")

    return result


@router.get("/capabilities")
async def api_capabilities():
    global _capabilities_cache
    async with _capabilities_lock:
        if _capabilities_cache is None:
            _capabilities_cache = await _probe_embedding()
    return _capabilities_cache


# -------------------------------------------------------------- pdf stream


@router.get("/read/{book_id}")
async def api_read(book_id: int):
    """
    Stream the PDF through this server.

    The fallback for when the upstream refuses to be framed. Bytes are piped
    straight through — nothing is written to disk, nothing is cached here.
    The file still lives on hebrewbooks.org; this process is a pipe.
    """
    if not PROXY_ENABLED:
        raise HTTPException(status_code=404, detail="Proxy disabled")

    if not search.get_book(book_id):
        raise HTTPException(status_code=404, detail="No sefer with that ID")

    url = PDF_URL.format(id=book_id)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0))

    try:
        request = client.build_request("GET", url, headers=UPSTREAM_HEADERS)
        upstream = await client.send(request, stream=True, follow_redirects=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502, detail=f"Upstream unreachable: {exc}"
        ) from exc

    if upstream.status_code != 200:
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream returned {status}")

    async def pump():
        try:
            async for chunk in upstream.aiter_bytes(65_536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        pump(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="hebrewbooks-{book_id}.pdf"',
            "Cache-Control": "public, max-age=3600",
            "X-Ganzach-Source": url,
        },
    )


@router.get("/health")
def api_health():
    try:
        total = db.scalar("SELECT COUNT(*) FROM books")
        return {"status": "ok", "books": total}
    except db.CatalogMissing as exc:
        return Response(
            content=f'{{"status":"no-catalog","detail":"{exc}"}}',
            status_code=503,
            media_type="application/json",
        )
