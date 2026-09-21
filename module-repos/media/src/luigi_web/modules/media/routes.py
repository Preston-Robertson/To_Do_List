"""Media HTTP controllers."""
from __future__ import annotations
import json
from fastapi import APIRouter
from typing import Any
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from ...auth import require_auth
from . import workspace

router = APIRouter()
router.include_router(workspace.router)


def _gnw_section(section: str) -> str:
    if section not in ("games", "shows"):
        raise HTTPException(404, "unknown section")
    return section


def _gnw_columns(section: str, profile: str | None):
    """Bucket items by status into the section's fixed status order."""
    from ... import application as host

    items = host.gnw.list_items(section, profile or None)
    columns: dict[str, list[dict[str, Any]]] = {s: [] for s in host.gnw.statuses_for(section)}
    for it in items:
        columns.setdefault(it["status"], []).append(it)
    return columns


def _gnw_board(request: Request, section: str, page_title: str):
    from ... import application as host

    reason = host.gnw.disabled_reason()
    ctx: dict[str, Any] = {
        "request": request,
        "active_nav": section,
        "page_title": page_title,
        "section": section,
        "disabled_reason": reason,
        "profiles": [],
        "profile": "",
        "columns": {},
        "statuses": host.gnw.statuses_for(section),
        "status_labels": host.gnw.STATUS_LABELS,
    }
    if not reason:
        profile = (request.query_params.get("profile") or "").strip()
        ctx["profile"] = profile
        try:
            # These hit Google over the network. Static checks in
            # disabled_reason() can't catch a wrong Sheet ID, a sheet that
            # isn't shared with the service account, the Sheets API being
            # disabled, a revoked key, or a transient network error — surface
            # any of those as a friendly notice instead of a raw 500.
            ctx["profiles"] = host.gnw.list_profiles()
            ctx["columns"] = host._gnw_columns(section, profile)
        except Exception as exc:  # noqa: BLE001
            # gnw now raises RuntimeError with a precise, self-contained reason
            # (bad credentials file vs. Google API/network failure), so surface
            # it verbatim rather than wrapping it in a second generic guess.
            ctx["disabled_reason"] = str(exc) or f"{type(exc).__name__}"
    return host.templates.TemplateResponse("media_board.html", ctx)


@router.get("/games", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def games_page(request: Request):
    return _library_page(request, "games", "Games")


@router.get("/shows", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def shows_page(request: Request):
    return _library_page(request, "shows", "Shows")


def _library_page(request: Request, section: str, page_title: str):
    from ... import application as host

    state, reason = None, None
    try:
        if host.gnw.disabled_reason():
            reason = "Media integration is unavailable. Check integration settings in Admin."
        else:
            state = workspace.library_state(section, (request.query_params.get("profile") or "").strip())
    except Exception:
        reason = "The media library could not be loaded. Try again or check integration settings."
    return host.templates.TemplateResponse("library.html", {
        "request": request, "section": section, "active_nav": section,
        "page_title": page_title, "media_seed": state, "disabled_reason": reason,
    }, headers={"Cache-Control": "no-store"})


@router.get(
    "/media/insights",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def media_insights_page(
    request: Request,
    section: str = "games",
    profile: str = "",
):
    from ... import application as host

    section = host._gnw_section(section)
    reason = host.gnw.disabled_reason()
    profiles: list[str] = []
    insights: dict[str, Any] | None = None
    recorded_insights, history_error = None, None
    if not reason:
        try:
            profiles = host.gnw.list_profiles()
            items = host.gnw.list_items(section, profile or None)
            insights = host.gnw.media_insights(section, items)
        except Exception:
            reason = "Media insights are unavailable"
        try:
            recorded_insights = workspace.history.insights(section, profile)
        except Exception:
            history_error = "Recorded web activity is unavailable"
    return host.templates.TemplateResponse(
        "media_insights.html",
        {
            "request": request,
            "active_nav": section,
            "page_title": "Media Insights",
            "section": section,
            "profile": profile,
            "profiles": profiles,
            "insights": insights,
            "recorded_insights": recorded_insights,
            "history_error": history_error,
            "disabled_reason": reason,
            "status_labels": host.gnw.STATUS_LABELS,
        }, headers={"Cache-Control": "no-store"},
    )


@router.get("/gnw/{section}/new", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def gnw_new_form(section: str, request: Request):
    from ... import application as host

    host._gnw_section(section)
    try:
        profiles = host.gnw.list_profiles()
    except Exception:
        raise HTTPException(503, "Media profiles are unavailable") from None
    return host.templates.TemplateResponse(
        "partials/media_new.html",
        {
            "request": request,
            "section": section,
            "profiles": profiles,
            "statuses": host.gnw.statuses_for(section),
            "status_labels": host.gnw.STATUS_LABELS,
        },
    )


@router.post("/gnw/{section}/search", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def gnw_search(section: str, request: Request):
    from ... import application as host

    host._gnw_section(section)
    form = dict(await request.form())
    query = str(form.get("query") or "").strip()
    if not query:
        raise HTTPException(422, "Search text is required")
    try:
        results = await run_in_threadpool(host.gnw.search_catalog, section, query)
        error = None
    except Exception:
        results = []
        error = "Catalog search is unavailable"
    return host.templates.TemplateResponse(
        "partials/media_search_results.html",
        {
            "request": request,
            "section": section,
            "query": query,
            "profile": str(form.get("profile") or ""),
            "status": str(form.get("status") or "backlog"),
            "priority": str(form.get("priority") or "3"),
            "results": results,
            "error": error,
        },
    )


@router.post("/gnw/{section}/add", dependencies=[Depends(require_auth)])
async def gnw_add_item(section: str, request: Request):
    from ... import application as host

    host._gnw_section(section)
    form = dict(await request.form())
    profile = str(form.get("profile") or "").strip()
    status = str(form.get("status") or "backlog")
    try:
        priority = int(form.get("priority") or 3)
    except (TypeError, ValueError):
        raise HTTPException(422, "priority must be a number")
    source = str(form.get("source") or "manual")
    external_id = str(form.get("external_id") or "")
    try:
        if source == "manual":
            ok, message = await run_in_threadpool(host.gnw.add_manual_item,
                section, profile, str(form.get("title") or ""),
                status=status, priority=priority,
            )
        else:
            metadata = await run_in_threadpool(host.gnw.catalog_lookup, section, source, external_id)
            if not metadata:
                raise ValueError("The selected catalog result is no longer available")
            ok, message = await run_in_threadpool(host.gnw.add_catalog_item,
                section, profile, metadata, status=status, priority=priority,
            )
    except ValueError:
        raise HTTPException(422, "Invalid media item") from None
    except Exception:
        raise HTTPException(503, "Media creation could not be confirmed. Refresh before retrying.") from None
    if not ok:
        raise HTTPException(409, message)
    kind = "Game" if section == "games" else "Show"
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": f"{kind} added"}),
        "HX-Refresh": "true",
    })


@router.get("/gnw/games/steam-stats", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def gnw_steam_stats(request: Request, profile: str, title: str, app_id: str):
    from ... import application as host

    try:
        item = workspace._steam_item(profile, title)
        cached = workspace.steam.get_snapshot(profile, title, item["external_id"])
        stats = cached["snapshot"]
        error = None if stats else "Refresh Steam from this game's library details to load a snapshot."
    except Exception:
        stats = None
        error = "Steam statistics are unavailable"
    return host.templates.TemplateResponse(
        "partials/steam_stats.html",
        {"request": request, "stats": stats, "error": error, "profile": profile, "title": title},
    )


@router.post("/gnw/{section}/status", dependencies=[Depends(require_auth)])
async def gnw_set_status(section: str, request: Request):
    from ... import application as host

    host._gnw_section(section)
    form = dict(await request.form())
    profile = (form.get("profile") or "").strip()
    title = (form.get("title") or "").strip()
    status = (form.get("status") or "").strip()
    if not profile or not title:
        raise HTTPException(400, "profile and title required")
    result = await run_in_threadpool(workspace._response, lambda: workspace.change(
        section, profile, title, {"status": status}, str(form.get("expected_version") or "") or None,
    ))
    confirmed = json.loads(bytes(result.body))
    if confirmed.get("history_warning"):
        raise HTTPException(503, "Library saved; local history is unconfirmed. Refresh before another change.")
    # Full refresh so the card lands in its new column and counts update.
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Status updated"}),
        "HX-Refresh": "true",
    })


@router.get(
    "/gnw/{section}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def gnw_edit_form(section: str, request: Request, profile: str, title: str):
    from ... import application as host

    host._gnw_section(section)
    try:
        item = workspace._current(section, profile, title)
    except Exception:
        raise HTTPException(503, "Media details are unavailable") from None
    return host.templates.TemplateResponse(
        "partials/media_form.html",
        {"request": request, "section": section, "item": item,
         "statuses": host.gnw.statuses_for(section), "status_labels": host.gnw.STATUS_LABELS},
    )


@router.post(
    "/gnw/{section}/update",
    dependencies=[Depends(require_auth)],
)
async def gnw_update(section: str, request: Request):
    from ... import application as host

    host._gnw_section(section)
    form = dict(await request.form())
    profile = (form.get("profile") or "").strip()
    title = (form.get("title") or "").strip()
    if not profile or not title:
        raise HTTPException(400, "profile and title required")
    editable = host.gnw.GAME_EDITABLE if section == "games" else host.gnw.SHOW_EDITABLE
    fields: dict[str, Any] = {}
    for key in editable:
        if key not in form:
            continue
        fields[key] = form[key]
    result = await run_in_threadpool(workspace._response, lambda: workspace.change(
        section, profile, title, fields, str(form.get("expected_version") or "") or None,
    ))
    confirmed = json.loads(bytes(result.body))
    if confirmed.get("history_warning"):
        raise HTTPException(503, "Library saved; local history is unconfirmed. Refresh before another change.")
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Media details saved"}),
        "HX-Refresh": "true",
    })


@router.post(
    "/gnw/{section}/pick",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def gnw_pick(section: str, request: Request):
    from ... import application as host

    host._gnw_section(section)
    form = dict(await request.form())
    profile = (form.get("profile") or "").strip() or None
    pick = host.gnw.random_pick(section, profile)
    return host.templates.TemplateResponse(
        "partials/media_pick.html",
        {"request": request, "section": section, "item": pick,
         "status_labels": host.gnw.STATUS_LABELS},
    )
