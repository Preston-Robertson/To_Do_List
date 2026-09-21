"""Authenticated, explicitly invoked Home preference endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ...auth import require_auth
from . import home_preferences as preferences

router = APIRouter(dependencies=[Depends(require_auth)])
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/home/layout")
def get_layout():
    try:
        layout = preferences.load()
    except preferences.LayoutStorageError:
        raise HTTPException(503, "Home layout is unavailable.", headers=_NO_STORE) from None
    return JSONResponse({"saved": layout is not None, "layout": layout}, headers=_NO_STORE)


@router.put("/home/layout")
async def put_layout(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Expected JSON.", headers=_NO_STORE)
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            length = int(declared_length)
        except ValueError:
            raise HTTPException(400, "Invalid Home layout.", headers=_NO_STORE) from None
        if length < 0 or length > preferences.MAX_BYTES:
            raise HTTPException(413, "Home layout is too large.", headers=_NO_STORE)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > preferences.MAX_BYTES:
            raise HTTPException(413, "Home layout is too large.", headers=_NO_STORE)
        body.extend(chunk)
    try:
        layout = preferences.decode(bytes(body))
        saved = await run_in_threadpool(preferences.save, layout)
    except preferences.LayoutValidationError:
        raise HTTPException(422, "Invalid Home layout.", headers=_NO_STORE) from None
    except preferences.LayoutStorageError:
        raise HTTPException(503, "Home layout could not be saved.", headers=_NO_STORE) from None
    return JSONResponse({"saved": True, "layout": saved}, headers=_NO_STORE)