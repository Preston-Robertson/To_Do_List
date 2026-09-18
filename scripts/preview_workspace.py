"""Loopback-only, disposable workspace preview with synthetic data."""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@contextmanager
def preview_context():
    with tempfile.TemporaryDirectory(prefix="luigi-workspace-preview-") as temporary, ExitStack() as stack:
        directory = Path(temporary)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LUIGI_WEB_")}
        environment.update({
            "LUIGI_WEB_UI_TOKEN": secrets.token_urlsafe(32),
            "LUIGI_WEB_FINANCE_TOKEN": secrets.token_urlsafe(32),
            "LUIGI_WEB_DATA_DIR": str(directory),
            "LUIGI_WEB_MODULES_FILE": str(directory / "modules.json"),
            "LUIGI_WEB_LLM_PROVIDER": "disabled",
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
            "LUIGI_WEB_SECURE_COOKIES": "0",
            "LUIGI_WEB_TIMEZONE": "UTC",
        })
        for key, filename in (
            ("LUIGI_WEB_CARDS_DB", "cards.db"), ("LUIGI_WEB_RPG_DB", "characters.db"),
            ("LUIGI_WEB_OPERATIONS_DB", "operations.db"), ("LUIGI_WEB_REVIEW_DB", "review.db"),
            ("LUIGI_WEB_FEEDBACK_DB", "feedback.db"), ("LUIGI_WEB_MAINTAINER_DB", "maintainer.db"),
            ("LUIGI_WEB_FINANCE_DB", "finance.db"), ("LUIGI_WEB_TASK_METADATA_FILE", "tasks.json"),
        ):
            environment[key] = str(directory / filename)
        stack.enter_context(patch.dict(os.environ, environment, clear=True))

        from fastapi import Request
        from fastapi.responses import JSONResponse, RedirectResponse
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        from luigi_web import application, auth, rpg
        from scripts.preview_cards import seed_cards

        seed_cards()
        rpg.init_db()
        application.operations.init_db()
        application.review.init_db()
        application.app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
        today = application.clock.local_today()
        tasks = [
            {
                "uuid": f"preview-task-{index}", "task": label, "priority": priority,
                "status": status, "completed": int(status == "Completed"), "catagory": category,
                "task_group": "Example project", "sub_group": "", "project_name": "Example project",
                "task_creation": (today - timedelta(days=5)).isoformat(),
                "start_time": (today - timedelta(days=2)).isoformat(),
                "due_date": (today + timedelta(days=offset)).isoformat(), "source": "task",
                "recurring": 0, "description": "", "relevant_link": "", "archived": 0,
            }
            for index, (label, status, priority, category, offset) in enumerate((
                ("Sketch a workspace layout", "In Progress", 3, "Creative", 0),
                ("Review the module contract", "Not Started", 2, "Work", 1),
                ("Plan the next release", "Not Started", 1, "Work", 3),
                ("Organize the example collection", "In Progress", 2, "Personal", 2),
                ("Check keyboard navigation", "Completed", 1, "Creative", -1),
            ), start=1)
        ]
        disciplines = [{
            "uuid": "preview-habit", "task": "Practice a skill", "catagory": "Learning",
            "frequency_per_week": 4, "current_streak": 3, "active": 1,
        }]
        week = [{
            "date": (today - timedelta(days=today.weekday()) + timedelta(days=offset)).isoformat(),
            "dow": label, "count": count,
        } for offset, (label, count) in enumerate(zip(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), (2, 3, 1, 0, 0, 0, 0)))]
        values = {
            "list_tasks": tasks, "list_recurring": [], "list_open_tasks": tasks[:4],
            "list_disciplines": disciplines, "list_disciplines_pending_today": disciplines,
            "weekly_discipline_counts": week, "weekly_task_completion_counts": week,
            "list_overdue_tasks": [], "list_upcoming_tasks": tasks[:4], "list_recent_completions": [],
            "list_discipline_streaks": disciplines, "list_follow_ups_preview": [], "list_follow_ups": [],
            "list_recent_activity": [], "list_disciplines_at_risk": [],
            "list_calendar_rows": tasks, "list_projects_with_open_tasks": [{"project": "Example project", "count": 4}],
            "list_project_rows": tasks, "project_grouping_enabled": True,
            "list_completion_tasks_for_day": set(), "list_completions_for_year": {"Practice a skill": {
                (today - timedelta(days=offset)).isoformat() for offset in (1, 2, 3)
            }},
            "weekly_review": {
                "start_iso": (today - timedelta(days=7)).isoformat(), "end_iso": (today - timedelta(days=1)).isoformat(),
                "completed_total": 6, "top_categories": [], "discipline_days": 3, "discipline_total": 3,
                "carried_over": 0, "upcoming_next_week": 4,
            },
        }
        for name, value in values.items():
            stack.enter_context(patch.object(application.db, name, side_effect=lambda *args, value=value, **kwargs: copy.deepcopy(value)))
        for name in ("list_task_completion_events", "list_calendar_activity_events", "list_activity_timeline"):
            stack.enter_context(patch.object(application.db, name, return_value=(application.task_events.Capability(False, "Synthetic preview"), [])))
        stack.enter_context(patch.object(application.db, "get_engine", side_effect=RuntimeError("Shared storage disabled in preview")))
        stack.enter_context(patch.object(application.db, "has_web_column", return_value=True))
        stack.enter_context(patch.object(application.db, "find_tasks_by_name", side_effect=lambda query, **kwargs: [copy.deepcopy(row) for row in tasks if query.lower() in row["task"].lower()]))
        stack.enter_context(patch.object(application.db, "search_disciplines", return_value=[]))
        stack.enter_context(patch.object(application, "_require_v2"))
        stack.enter_context(patch.object(application, "_reactivate_recurring"))
        stack.enter_context(patch.object(application, "_available_years", return_value=[today.year]))

        @application.app.middleware("http")
        async def preview_security(request: Request, call_next):
            if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
                return JSONResponse({"detail": "Loopback only"}, status_code=403)
            path = request.url.path
            if path.startswith(("/admin", "/gnw", "/feedback", "/chat")) or "/refresh" in path:
                return JSONResponse({"detail": "Integration and deployment actions are disabled in preview"}, status_code=403)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and path != "/modules" and not path.startswith(("/cards", "/characters")):
                return JSONResponse({"detail": "This preview action is read-only"}, status_code=403)
            if path == "/":
                response = RedirectResponse("/modules", status_code=303)
                response.set_cookie(auth.COOKIE_NAME, environment["LUIGI_WEB_UI_TOKEN"], httponly=True, samesite="strict")
                response.set_cookie(auth.CSRF_COOKIE_NAME, auth.csrf_token(), samesite="strict")
            elif path == "/reminders/count":
                response = JSONResponse(0)
            else:
                response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        yield application.app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    with preview_context() as app:
        if arguments.check:
            from fastapi.testclient import TestClient

            client = TestClient(app)
            client.get("/")
            for path in ("/modules", "/home", "/tasks", "/calendar", "/projects", "/discipline", "/cards/mtg/decks", "/characters", "/finance/unlock"):
                response = client.get(path)
                if response.status_code != 200:
                    raise RuntimeError(f"Synthetic preview check failed: {path} ({response.status_code})")
            print("Validated nine synthetic workspace views without external services.")
            return
        import uvicorn

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", arguments.port))
            port = listener.getsockname()[1]
            print(f"Synthetic workspace preview: http://127.0.0.1:{port}/", flush=True)
            uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning",
            )).run(sockets=[listener])


if __name__ == "__main__":
    main()