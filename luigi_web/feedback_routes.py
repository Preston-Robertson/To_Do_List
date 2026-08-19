"""Authenticated local feedback inbox routes."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import feedback
from .auth import require_auth
from .paths import TEMPLATES_DIR

router = APIRouter(dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _safe_referer_path(request: Request) -> str:
    referer = request.headers.get("referer", "")
    if not referer:
        return ""
    path = urlsplit(referer).path
    return path if path.startswith("/") else ""


@router.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request, status: str = "", q: str = ""):
    feedback.init_db()
    try:
        rows = feedback.list_items(status=status, query=q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return templates.TemplateResponse("feedback.html", {
        "request": request, "active_nav": "feedback", "page_title": "Feedback",
        "rows": rows, "statuses": feedback.STATUSES, "status_filter": status,
        "query": q,
    })


@router.get("/feedback/new", response_class=HTMLResponse)
def feedback_new(request: Request):
    return templates.TemplateResponse("partials/feedback_form.html", {
        "request": request, "categories": feedback.CATEGORIES,
        "page_path": _safe_referer_path(request),
    })


@router.post("/feedback")
async def feedback_create(request: Request):
    form = dict(await request.form())
    try:
        feedback.create_item(form)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204, headers={
        "HX-Trigger": json.dumps({"flashSuccess": {"message": "Feedback saved"}, "closeModal": None}),
    })


@router.post("/feedback/{row_uuid}")
async def feedback_update(request: Request, row_uuid: str):
    try:
        saved = feedback.update_item(row_uuid, dict(await request.form()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not saved:
        raise HTTPException(404, "feedback item not found")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/feedback/{row_uuid}/delete")
def feedback_delete(row_uuid: str):
    if not feedback.delete_item(row_uuid):
        raise HTTPException(404, "feedback item not found")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.get("/feedback/export", response_class=HTMLResponse)
def feedback_export_review(request: Request):
    rows = feedback.list_items()
    return templates.TemplateResponse("feedback_export.html", {
        "request": request, "active_nav": "feedback",
        "page_title": "Review Feedback Export", "rows": rows,
    })


@router.get("/feedback/export.json")
def feedback_export_json():
    return JSONResponse(feedback.export_payload(), headers={
        "Content-Disposition": "attachment; filename=luigi-feedback.json",
        "Cache-Control": "no-store",
    })


@router.get("/feedback/export.md")
def feedback_export_markdown():
    return Response(feedback.export_markdown(), media_type="text/markdown", headers={
        "Content-Disposition": "attachment; filename=luigi-feedback.md",
        "Cache-Control": "no-store",
    })