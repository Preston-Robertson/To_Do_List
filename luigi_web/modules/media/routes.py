"""Media HTTP controllers."""
from __future__ import annotations
from fastapi import APIRouter
from typing import Any
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from ...auth import require_auth

router = APIRouter()


def _gnw_section(section: str) -> str:
    if section not in ("games", "shows"):
        raise HTTPException(404, "unknown section")
    return section


def _gnw_columns(section: str, profile: str | None):
    """Bucket items by status into the section's fixed status order."""    from ... import application as host

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
    from ... import application as host

    return host._gnw_board(request, "games", "Games")


@router.get("/shows", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def shows_page(request: Request):
    from ... import application as host

    return host._gnw_board(request, "shows", "Shows")


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
    if not reason:
        try:
            profiles = host.gnw.list_profiles()
            items = host.gnw.list_items(section, profile or None)
            insights = host.gnw.media_insights(section, items)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) or type(exc).__name__
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
            "disabled_reason": reason,
            "status_labels": host.gnw.STATUS_LABELS,
        },
    )


@router.get("/gnw/{section}/new", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def gnw_new_form(section: str, request: Request):
    from ... import application as host

    host._gnw_section(section)
    try:
        profiles = host.gnw.list_profiles()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, str(exc)) from exc
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
        results = host.gnw.search_catalog(section, query)
        error = None
    except Exception as exc:  # noqa: BLE001
        results = []
        error = f"{type(exc).__name__}: {exc}"
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
            ok, message = host.gnw.add_manual_item(
                section, profile, str(form.get("title") or ""),
                status=status, priority=priority,
            )
        else:
            metadata = host.gnw.catalog_lookup(section, source, external_id)
            if not metadata:
                raise RuntimeError("The selected catalog result is no longer available")
            ok, message = host.gnw.add_catalog_item(
                section, profile, metadata, status=status, priority=priority,
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Could not add item: {type(exc).__name__}: {exc}") from exc
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
        stats = host.gnw.steam_stats(app_id)
        # Keep the existing sheet's Hours Played field useful to the bot too.
        host.gnw.update_item("games", profile, title, {"hours_played": stats["hours_played"]})
        error = None
    except Exception as exc:  # noqa: BLE001
        stats = None
        error = f"{type(exc).__name__}: {exc}"
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
    try:
        ok = host.gnw.set_status(section, profile, title, status)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "item not found")
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
    item = host.gnw.get_item(section, profile, title)
    if not item:
        raise HTTPException(404, "item not found")
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
    int_fields = {"priority", "rating", "current_episode", "current_season", "total_episodes"}
    fields: dict[str, Any] = {}
    for key in editable:
        if key not in form:
            continue
        val = form[key]
        if key in int_fields:
            raw = str(val).strip()
            val = int(raw) if raw.lstrip("-").isdigit() else None
        if key == "rating" and val is not None and not 0 <= val <= 10:
            raise HTTPException(422, "rating must be between 0 and 10")
        fields[key] = val
    ok = host.gnw.update_item(section, profile, title, fields)
    if not ok:
        raise HTTPException(404, "item not found")
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
