"""Synthetic Tasks proposals with browser-only, disposable interaction state."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...auth import require_auth
from ...core import templating as core

router = APIRouter()
templates = core.create_templates()


def example_state():
    return {
        "tasks": [
            {"id": "example-task-1", "title": "Review example outline", "project": "Example project",
             "priority": "High", "due": "2030-04-10", "done": False, "recurring": False},
            {"id": "example-task-2", "title": "Draft example summary", "project": "Example project",
             "priority": "Normal", "due": "2030-04-12", "done": False, "recurring": False},
            {"id": "example-task-3", "title": "Sort example notes", "project": "Example project",
             "priority": "Low", "due": "2030-04-15", "done": False, "recurring": True},
            {"id": "example-task-4", "title": "Prepare example handoff", "project": "Example archive",
             "priority": "Normal", "due": "", "done": False, "recurring": False},
            {"id": "example-task-5", "title": "Check example references", "project": "Example archive",
             "priority": "Low", "due": "", "done": True, "recurring": False},
        ],
        "rules": [
            {"id": "example-rule-1", "type": "completion", "source": "example-task-1",
             "target": "example-task-2", "lead": 30, "enabled": True},
            {"id": "example-rule-2", "type": "completion", "source": "example-task-3",
             "target": "example-task-4", "lead": 30, "enabled": False},
            {"id": "example-rule-3", "type": "dependency", "source": "example-task-1",
             "target": "example-task-2", "lead": 30, "enabled": True},
            {"id": "example-rule-4", "type": "reminder", "source": "example-task-3",
             "target": "", "lead": 30, "enabled": True},
        ],
    }


@router.get("/tasks/preview", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def task_examples(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="task_examples.html",
        context={"page_title": "Tasks examples", "active_nav": "tasks", "example_seed": example_state()},
        headers={"Cache-Control": "no-store"},
    )