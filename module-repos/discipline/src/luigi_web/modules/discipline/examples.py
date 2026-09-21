"""Synthetic Discipline history with disposable browser-only corrections."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...auth import require_auth
from ...core import templating as core

router = APIRouter()
templates = core.create_templates()


def example_state():
    return {
        "name": "Example reading practice",
        "weeklyTarget": 3,
        "sampleToday": "2030-04-24",
        "sampleNow": "2030-04-24T20:00:00Z",
        "weekStarts": ["2030-03-18", "2030-03-25", "2030-04-01", "2030-04-08", "2030-04-15", "2030-04-22"],
        "completions": [
            {"date": "2030-03-18", "completedAt": "2030-03-18T18:30:00Z", "loggedAt": "2030-03-18T18:31:00Z"},
            {"date": "2030-03-20", "completedAt": "2030-03-20T19:15:00Z", "loggedAt": "2030-03-20T19:16:00Z"},
            {"date": "2030-03-23", "completedAt": "2030-03-23T10:00:00Z", "loggedAt": "2030-03-24T11:00:00Z"},
            {"date": "2030-03-26", "completedAt": "2030-03-26T18:30:00Z", "loggedAt": "2030-03-26T18:31:00Z"},
            {"date": "2030-03-29", "completedAt": "2030-03-29T18:00:00Z", "loggedAt": "2030-03-30T09:00:00Z"},
            {"date": "2030-04-01", "completedAt": "2030-04-01T18:30:00Z", "loggedAt": "2030-04-01T18:31:00Z"},
            {"date": "2030-04-03", "completedAt": "2030-04-03T18:30:00Z", "loggedAt": "2030-04-03T18:31:00Z"},
            {"date": "2030-04-05", "completedAt": "2030-04-05T18:30:00Z", "loggedAt": "2030-04-05T18:31:00Z"},
            {"date": "2030-04-07", "completedAt": "2030-04-07T10:00:00Z", "loggedAt": "2030-04-07T10:01:00Z"},
            {"date": "2030-04-12", "completedAt": "2030-04-12T18:30:00Z", "loggedAt": "2030-04-13T09:00:00Z"},
            {"date": "2030-04-15", "completedAt": "2030-04-15T18:30:00Z", "loggedAt": "2030-04-15T18:31:00Z"},
            {"date": "2030-04-18", "completedAt": "2030-04-18T18:30:00Z", "loggedAt": "2030-04-18T18:31:00Z"},
            {"date": "2030-04-20", "completedAt": "2030-04-20T10:00:00Z", "loggedAt": "2030-04-21T09:00:00Z"},
            {"date": "2030-04-22", "completedAt": "2030-04-22T18:30:00Z", "loggedAt": "2030-04-22T18:31:00Z"},
            {"date": "2030-04-23", "completedAt": "2030-04-23T18:30:00Z", "loggedAt": "2030-04-24T09:00:00Z"},
        ],
    }


@router.get("/discipline/history-preview", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def history_example(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="history_example.html",
        context={"page_title": "Discipline history example", "active_nav": "discipline", "example_seed": example_state()},
        headers={"Cache-Control": "no-store"},
    )