"""Tasks HTTP controllers."""
from __future__ import annotations
from fastapi import APIRouter, FastAPI
from typing import Any
from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from ...auth import require_auth
from . import occurrences
from .examples import router as examples_router

router = APIRouter()
router.include_router(examples_router)


def _task_write_error(exc: ValueError | occurrences.OccurrenceStorageError, status: int = 422) -> HTTPException:
    if isinstance(exc, occurrences.OccurrenceStorageError):
        return HTTPException(503, "Recurring task storage is unavailable. Please try again.")
    if str(exc) == occurrences.HISTORY_MESSAGE:
        return HTTPException(409, "Completed occurrence history cannot be changed.")
    return HTTPException(status, str(exc))


def _recurring_row(row_uuid: str) -> dict[str, Any] | None:
    from ... import application as host

    try:
        return host.db.get_recurring(row_uuid)
    except occurrences.OccurrenceStorageError as exc:
        raise _task_write_error(exc) from None


def startup(app: FastAPI | None = None) -> None:
    from ... import application as host

    host.operations.init_db()


async def _form_dict(request: Request) -> dict[str, Any]:
    """Read a form into a dict, preserving multi-value ``recurring_days``.

    ``dict(await request.form())`` collapses repeated keys to just the last
    value, which would silently drop every weekday except the last-checked
    one. The DB layer's ``parse_recurring_days`` accepts either a list or a
    CSV string, so we hand it the raw list.
    """
    form = await request.form()
    data = dict(form)
    if "recurring_days" in form:
        data["recurring_days"] = form.getlist("recurring_days")
    return data


def _validate_recurring_form(data: dict[str, Any]) -> None:
    """Reject an enabled recurring row that can never reactivate."""
    from ... import application as host

    enabled = str(data.get("recurring") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not enabled:
        return
    schedule_type = str(data.get("recurring_schedule_type") or "").strip().lower()
    if schedule_type and schedule_type not in {"interval", "weekdays", "monthly"}:
        raise HTTPException(422, "Choose a valid recurrence schedule")
    if schedule_type == "monthly":
        if host.recurrence.parse_monthly_schedule(
            data.get("recurring_month_ordinal"),
            data.get("recurring_month_weekday"),
        ) is None:
            raise HTTPException(422, "Choose a valid monthly position and weekday")
        return
    if schedule_type == "weekdays":
        if not host.db.parse_recurring_days(data.get("recurring_days")):
            raise HTTPException(422, "Choose at least one weekday")
        return
    interval_raw = data.get("recurring_interval")
    if interval_raw not in (None, ""):
        try:
            if int(interval_raw) < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(422, "Repeat interval must be a positive number of days")
    if interval_raw in (None, "") and not host.db.parse_recurring_days(data.get("recurring_days")):
        raise HTTPException(
            422,
            "Enter a repeat interval",
        )


def _kanban_columns(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket task-like rows by status, preserving the fixed enum order."""
    from ... import application as host

    columns = {s: [] for s in host.db.STATUS_VALUES}
    for row in rows:
        status = row.get("status") or "Not Started"
        columns.setdefault(status, []).append(row)
    return columns


@router.get("/tasks", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def tasks_page(request: Request):
    from ... import application as host

    host._require_v2()
    host._reactivate_recurring()
    rows = host.db.list_tasks()
    for row in rows:
        row["_endpoint_root"] = "/tasks"
        row["_source"] = "task"
    recurring_rows = host.db.list_recurring()
    for row in recurring_rows:
        row["_endpoint_root"] = "/recurring"
        row["_source"] = "recurring"
    rows.extend(recurring_rows)
    dependency_map: dict[tuple[str, str], list[str]] = {}
    try:
        host.operations.reconcile_task_records(rows, prune_missing=False)
        for edge in host.operations.list_dependencies():
            key = (edge["dependent_source"], edge["dependent_uuid"])
            dependency_map.setdefault(key, []).append(edge["blocker_label"])
    except Exception:
        dependency_map = {}
    for row in rows:
        row["_blockers"] = dependency_map.get(
            (row["_source"], str(row.get("uuid") or "")), []
        )
    rows.sort(
        key=lambda row: (
            int(row.get("completed") or 0),
            -int(row.get("priority") or 0),
            row.get("due_date") or "9999-12-31",
            (row.get("task") or "").lower(),
        )
    )
    return host.templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "active_nav": "tasks",
            "rows": rows,
            "columns": host._kanban_columns(rows),
            "statuses": host.db.STATUS_DISPLAY_ORDER,
            "endpoint_root": "/tasks",
            "page_title": "Tasks",
            "consolidated": True,
            "filter_date": host.clock.local_today().isoformat(),
            "recurrence_status": host.recurrence_status(),
        },
    )


def _operation_task_rows() -> list[dict[str, Any]]:
    from ... import application as host

    rows = []
    for source, found in (("task", host.db.list_tasks()), ("recurring", host.db.list_recurring())):
        for row in found:
            shaped = dict(row)
            shaped["source"] = source
            rows.append(shaped)
    return rows


def _reconcile_operation_records(rows: list[dict[str, Any]]) -> None:
    from ... import application as host

    known = {
        (str(row.get("source") or ""), str(row.get("uuid") or "")): row
        for row in rows
    }
    references = {
        (edge["dependent_source"], edge["dependent_uuid"])
        for edge in host.operations.list_dependencies()
    } | {
        (edge["blocker_source"], edge["blocker_uuid"])
        for edge in host.operations.list_dependencies()
    } | {
        (rule["task_source"], rule["task_uuid"])
        for rule in host.operations.list_reminder_rules()
    }
    for source, row_uuid in references - set(known):
        row = host.db.get_recurring(row_uuid) if source == "recurring" else host.db.get_task(row_uuid)
        if row:
            known[(source, row_uuid)] = {**row, "source": source}
    host.operations.reconcile_task_records(list(known.values()))


def _operation_task(task_ref: str) -> dict[str, Any]:
    from ... import application as host

    try:
        source, row_uuid = str(task_ref).split(":", 1)
    except ValueError as exc:
        raise ValueError("invalid task reference") from exc
    if source not in host.operations.SOURCES or not row_uuid:
        raise ValueError("invalid task reference")
    row = host.db.get_recurring(row_uuid) if source == "recurring" else host.db.get_task(row_uuid)
    if not row:
        raise ValueError("task not found")
    return {"uuid": row_uuid, "source": source, "task": row.get("task") or "Task"}


def _evaluate_notifications() -> int:
    from ... import application as host

    return host.operations.evaluate_notifications(host._operation_task_rows())


@router.get("/task-rules", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def task_rules_page(request: Request):
    from ... import application as host

    host._require_v2()
    rows = host._operation_task_rows()
    host._reconcile_operation_records(rows)
    return host.templates.TemplateResponse("task_rules.html", {
        "request": request, "active_nav": "task-rules", "page_title": "Task rules",
        "tasks": rows,
        "dependencies": host.operations.list_dependencies(),
        "reminder_rules": host.operations.list_reminder_rules(),
    })


@router.post("/task-rules/dependencies", dependencies=[Depends(require_auth)])
async def dependency_create(request: Request):
    from ... import application as host

    form = dict(await request.form())
    try:
        dependent = host._operation_task(str(form.get("dependent") or ""))
        blocker = host._operation_task(str(form.get("blocker") or ""))
        host.operations.add_dependency(
            dependent_uuid=dependent["uuid"], dependent_source=dependent["source"],
            dependent_label=str(dependent["task"]), blocker_uuid=blocker["uuid"],
            blocker_source=blocker["source"], blocker_label=str(blocker["task"]),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/task-rules/dependencies/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def dependency_delete(row_uuid: str):
    from ... import application as host

    if not host.operations.delete_dependency(row_uuid):
        raise HTTPException(404, "dependency not found")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/task-rules/reminders", dependencies=[Depends(require_auth)])
async def reminder_rule_create(request: Request):
    from ... import application as host

    form = dict(await request.form())
    try:
        task = host._operation_task(str(form.get("task_ref") or ""))
        host.operations.create_reminder_rule({
            **form, "task_uuid": task["uuid"], "task_source": task["source"],
            "task_label": task["task"],
        })
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/task-rules/reminders/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def reminder_rule_delete(row_uuid: str):
    from ... import application as host

    if not host.operations.delete_reminder_rule(row_uuid):
        raise HTTPException(404, "reminder rule not found")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.get("/reminders/count", dependencies=[Depends(require_auth)])
def reminders_count():
    from ... import application as host

    try:
        count = host._evaluate_notifications()
    except Exception:
        host.logger.exception("Reminder evaluation failed")
        count = 0
    return Response(str(count), media_type="text/plain", headers={"Cache-Control": "no-store"})


@router.get("/reminders", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def reminders_page(request: Request):
    from ... import application as host

    host._require_v2()
    host._evaluate_notifications()
    return host.templates.TemplateResponse("reminders.html", {
        "request": request, "active_nav": "reminders", "page_title": "Reminders",
        "rows": host.operations.list_notifications(),
    }, headers={"Cache-Control": "no-store"})


@router.post("/reminders/{row_uuid}/dismiss", dependencies=[Depends(require_auth)])
def reminder_dismiss(row_uuid: str):
    from ... import application as host

    if not host.operations.dismiss_notification(row_uuid):
        raise HTTPException(404, "reminder not found")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/reminders/{row_uuid}/snooze", dependencies=[Depends(require_auth)])
def reminder_snooze(row_uuid: str, days: int = Form(default=1)):
    from ... import application as host

    if not host.operations.snooze_notification(row_uuid, days):
        raise HTTPException(404, "reminder not found")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/tasks", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def tasks_create(request: Request):
    from ... import application as host

    host._require_v2()
    form = await host._form_dict(request)
    repeat = str(form.get("recurring") or "0").strip().lower()
    if repeat not in {"0", "1", "false", "true", "off", "on", "no", "yes"}:
        raise HTTPException(422, "Invalid Repeat setting")
    repeating = repeat in {"1", "true", "on", "yes"}
    if not str(form.get("task") or "").strip():
        raise HTTPException(422, "Task name is required")
    endpoint = "/recurring" if repeating else "/tasks"
    try:
        if repeating:
            host._validate_recurring_form(form)
            row_uuid = host.db.create_recurring(form)
            row = host.db.get_recurring(row_uuid)
        else:
            form = {key: value for key, value in form.items() if not key.startswith("recurring")}
            row_uuid = host.db.create_task(form)
            row = host.db.get_task(row_uuid)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Task values are invalid") from exc
    if not row or str(row.get("uuid")) != str(row_uuid):
        raise HTTPException(503, "Task creation could not be verified. Reload before retrying.")
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": endpoint},
        headers={"HX-Trigger": host._hx_trigger(
            flashSuccess={"message": "Recurring task created" if repeating else "Task created"},
            closeModal=None,
            reloadBoard=None,
        )},
    )


@router.post("/tasks/quick", dependencies=[Depends(require_auth)])
async def tasks_quick_create(request: Request):
    """Small header form for the common one-off task creation path."""
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    task = str(form.get("task") or "").strip()
    if not task:
        raise HTTPException(422, "Task name is required")
    payload = {
        "task": task,
        "priority": form.get("priority") or 0,
        "due_date": form.get("due_date") or None,
        "project": form.get("project") or None,
        "status": "Not Started",
    }
    try:
        host.db.create_task(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Task added"}),
        "HX-Refresh": "true",
    })


@router.post("/tasks/bulk", dependencies=[Depends(require_auth)])
async def tasks_bulk(request: Request):
    from ... import application as host

    host._require_v2()
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "bulk request must be JSON") from exc
    action = str(payload.get("action") or "").strip()
    value = payload.get("value")
    items = payload.get("items")
    if action not in {"status", "archive", "project", "catagory", "delete"}:
        raise HTTPException(422, "invalid bulk action")
    if not isinstance(items, list) or not items or len(items) > 500:
        raise HTTPException(422, "select between 1 and 500 tasks")
    if action == "status" and value not in host.db.STATUS_VALUES:
        raise HTTPException(422, "invalid task status")
    if action in {"project", "catagory"} and len(str(value or "")) > 240:
        raise HTTPException(422, "bulk value is too long")

    results: list[dict[str, Any]] = []
    for item in items:
        row_uuid = str(item.get("uuid") or "") if isinstance(item, dict) else ""
        source = str(item.get("source") or "") if isinstance(item, dict) else ""
        result = {"uuid": row_uuid, "source": source, "ok": False, "error": None}
        if not row_uuid or source not in {"task", "recurring"}:
            result["error"] = "invalid task reference"
            results.append(result)
            continue
        try:
            recurring = source == "recurring"
            if action == "status":
                transition = (
                    host.db.set_recurring_status(row_uuid, str(value))
                    if recurring else host.db.set_task_status(row_uuid, str(value))
                )
                result["generated_task_uuids"] = list(
                    transition.generated_task_uuids
                )
            elif action == "archive":
                saved = (
                    host.db.archive_recurring(row_uuid, True)
                    if recurring else host.db.archive_task(row_uuid, True)
                )
                if not saved:
                    raise LookupError("task not found")
            elif action in {"project", "catagory"}:
                saved = (
                    host.db.set_recurring_metadata(
                        row_uuid, field=action, value=str(value or "")
                    )
                    if recurring else host.db.set_task_metadata(
                        row_uuid, field=action, value=str(value or "")
                    )
                )
                if not saved:
                    raise LookupError("task not found")
            else:
                if recurring:
                    if host.db.get_recurring(row_uuid) is None:
                        raise LookupError("task not found")
                    host.db.delete_recurring(row_uuid)
                else:
                    if host.db.get_task(row_uuid) is None:
                        raise LookupError("task not found")
                    host.db.delete_task(row_uuid)
            result["ok"] = True
        except Exception:  # noqa: BLE001 - returned per row
            host.logger.exception(
                "Bulk %s failed for %s task %s", action, source, row_uuid
            )
            result["error"] = "Operation failed"
        results.append(result)
    succeeded = sum(1 for result in results if result["ok"])
    return JSONResponse({
        "action": action,
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    })


@router.get(
    "/tasks/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def tasks_edit_form(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    row = host.db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    return host.templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": row,
            "statuses": host.db.STATUS_VALUES,
            "endpoint_root": "/tasks",
            "is_new": False,
        },
    )


@router.get(
    "/tasks/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def tasks_new_form(request: Request):
    from ... import application as host

    return host.templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": {},
            "statuses": host.db.STATUS_VALUES,
            "endpoint_root": "/tasks",
            "is_new": True,
        },
    )


@router.post(
    "/tasks/{row_uuid}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def tasks_update(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    try:
        host.db.update_task(row_uuid, form)
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc) from None
    row = host.db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/tasks"},
        headers={"HX-Trigger": host._hx_trigger(
            flashSuccess={"message": "Task saved"},
            closeModal=None,
            reloadBoard=None,
        )},
    )


@router.post("/tasks/{row_uuid}/status", dependencies=[Depends(require_auth)])
async def tasks_set_status(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    new_status = form.get("status", "")
    try:
        host.db.set_task_status(row_uuid, new_status)
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc, 400) from None
    return Response(status_code=204)


@router.post(
    "/tasks/{row_uuid}/complete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def tasks_toggle_complete(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    before = host.db.get_task(row_uuid)
    if not before:
        raise HTTPException(404)
    form = await request.form()
    effective_date = str(form.get("effective_date") or "").strip() or None
    try:
        transition = host.db.toggle_task_completed(
            row_uuid, effective_date=effective_date
        )
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc) from None
    row = host.db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    verb = "Completed" if int(row.get("completed") or 0) == 1 else "Reopened"
    op_id = host._stash_undo(
        "tasks", before, f"{verb} ‘{before.get('task','')}’",
        list(transition.generated_task_uuids), transition.event_uuid,
        transition.event_type,
    )
    trigger = host._hx_trigger(
        showUndo={"op_id": op_id, "label": f"{verb} ‘{before.get('task','')}’",
                  "ttl_ms": host._UNDO_TTL_SECONDS * 1000},
        reloadBoard=None,
    )
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/tasks"},
        headers={"HX-Trigger": trigger},
    )


@router.post("/tasks/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def tasks_delete(row_uuid: str):
    from ... import application as host

    host._require_v2()
    before = host.db.get_task(row_uuid)
    if not before:
        raise HTTPException(404)
    operation_records = host.db.delete_task(row_uuid)
    op_id = host._stash_undo(
        "tasks", before, f"Deleted ‘{before.get('task','')}’",
        operation_records=operation_records,
    )
    trigger = host._hx_trigger(
        showUndo={"op_id": op_id, "label": f"Deleted ‘{before.get('task','')}’",
                  "ttl_ms": host._UNDO_TTL_SECONDS * 1000},
        closeModal=None,
    )
    # HTMX swaps the card with an empty response, removing it from the DOM.
    return Response(status_code=200, content="", headers={"HX-Trigger": trigger})


@router.post("/tasks/{row_uuid}/archive", dependencies=[Depends(require_auth)])
def tasks_archive(row_uuid: str):
    from ... import application as host

    host._require_v2()
    if not host.db.archive_task(row_uuid, True):
        raise HTTPException(404, "task not found")
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Task archived"}),
        "HX-Refresh": "true",
    })


@router.post("/tasks/{row_uuid}/restore", dependencies=[Depends(require_auth)])
def tasks_restore(row_uuid: str):
    from ... import application as host

    host._require_v2()
    if not host.db.archive_task(row_uuid, False):
        raise HTTPException(404, "task not found")
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Task restored"}),
        "HX-Refresh": "true",
    })


@router.post(
    "/tasks/{row_uuid}/snooze",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def tasks_snooze(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    before = host.db.get_task(row_uuid)
    if not before:
        raise HTTPException(404)
    form = dict(await request.form())
    try:
        days = int(form.get("days", "1"))
    except ValueError:
        raise HTTPException(400, "days must be an integer")
    try:
        if not host.db.snooze_task(row_uuid, days):
            raise HTTPException(404)
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc) from None
    row = host.db.get_task(row_uuid)
    if not row:
        raise HTTPException(404)
    op_id = host._stash_undo("tasks", before, f"Snoozed ‘{before.get('task','')}’ {days}d")
    trigger = host._hx_trigger(
        showUndo={"op_id": op_id,
                  "label": f"Snoozed ‘{before.get('task','')}’ by {days}d",
                  "ttl_ms": host._UNDO_TTL_SECONDS * 1000},
    )
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/tasks"},
        headers={"HX-Trigger": trigger},
    )


@router.get("/recurring", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def recurring_page(request: Request):
    """Legacy bookmark: recurring tasks now live on the Tasks board."""
    from ... import application as host

    host._require_v2()
    return RedirectResponse(url="/tasks", status_code=303)


@router.post("/recurring", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def recurring_create(request: Request):
    from ... import application as host

    host._require_v2()
    form = await host._form_dict(request)
    host._validate_recurring_form(form)
    row_uuid = host.db.create_recurring(form)
    row = host.db.get_recurring(row_uuid)
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
        headers={"HX-Trigger": host._hx_trigger(
            flashSuccess={"message": "Recurring task created"},
            closeModal=None,
            reloadBoard=None,
        )},
    )


@router.get(
    "/recurring/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def recurring_new_form(request: Request):
    from ... import application as host

    return host.templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": {},
            "statuses": host.db.STATUS_VALUES,
            "endpoint_root": "/recurring",
            "is_new": True,
        },
    )


@router.get(
    "/recurring/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def recurring_edit_form(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    row = _recurring_row(row_uuid)
    if not row:
        raise HTTPException(404)
    return host.templates.TemplateResponse(
        "partials/task_form.html",
        {
            "request": request,
            "t": row,
            "statuses": host.db.STATUS_VALUES,
            "endpoint_root": "/recurring",
            "is_new": False,
        },
    )


@router.post(
    "/recurring/{row_uuid}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def recurring_update(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    form = await host._form_dict(request)
    host._validate_recurring_form(form)
    try:
        host.db.update_recurring(row_uuid, form)
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc) from None
    row = _recurring_row(row_uuid)
    if not row:
        raise HTTPException(404)
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
        headers={"HX-Trigger": host._hx_trigger(
            flashSuccess={"message": "Recurring task saved"},
            closeModal=None,
            reloadBoard=None,
        )},
    )


@router.post("/recurring/{row_uuid}/status", dependencies=[Depends(require_auth)])
async def recurring_set_status(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    new_status = form.get("status", "")
    try:
        host.db.set_recurring_status(row_uuid, new_status)
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc, 400) from None
    return Response(status_code=204)


@router.post(
    "/recurring/{row_uuid}/complete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def recurring_toggle_complete(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    before = _recurring_row(row_uuid)
    if not before:
        raise HTTPException(404)
    form = await request.form()
    effective_date = str(form.get("effective_date") or "").strip() or None
    try:
        transition = host.db.toggle_recurring_completed(
            row_uuid, effective_date=effective_date
        )
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc) from None
    row = _recurring_row(row_uuid)
    if not row:
        raise HTTPException(404)
    verb = "Completed" if int(row.get("completed") or 0) == 1 else "Reopened"
    op_id = host._stash_undo(
        "recurring_tasks", before, f"{verb} ‘{before.get('task','')}’",
        list(transition.generated_task_uuids), transition.event_uuid,
        transition.event_type,
    )
    trigger = host._hx_trigger(
        showUndo={"op_id": op_id, "label": f"{verb} ‘{before.get('task','')}’",
                  "ttl_ms": host._UNDO_TTL_SECONDS * 1000},
        reloadBoard=None,
    )
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
        headers={"HX-Trigger": trigger},
    )


@router.post("/recurring/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def recurring_delete(row_uuid: str):
    from ... import application as host

    host._require_v2()
    before = host.db.get_recurring(row_uuid)
    if not before:
        raise HTTPException(404)
    operation_records = host.db.delete_recurring(row_uuid)
    op_id = host._stash_undo("recurring_tasks", before,
                        f"Deleted ‘{before.get('task','')}’",
                        operation_records=operation_records)
    trigger = host._hx_trigger(
        showUndo={"op_id": op_id, "label": f"Deleted ‘{before.get('task','')}’",
                  "ttl_ms": host._UNDO_TTL_SECONDS * 1000},
        closeModal=None,
    )
    return Response(status_code=200, content="", headers={"HX-Trigger": trigger})


@router.post("/recurring/{row_uuid}/archive", dependencies=[Depends(require_auth)])
def recurring_archive(row_uuid: str):
    from ... import application as host

    host._require_v2()
    if not host.db.archive_recurring(row_uuid, True):
        raise HTTPException(404, "recurring task not found")
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Recurring task archived"}),
        "HX-Refresh": "true",
    })


@router.post("/recurring/{row_uuid}/restore", dependencies=[Depends(require_auth)])
def recurring_restore(row_uuid: str):
    from ... import application as host

    host._require_v2()
    if not host.db.archive_recurring(row_uuid, False):
        raise HTTPException(404, "recurring task not found")
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(flashSuccess={"message": "Recurring task restored"}),
        "HX-Refresh": "true",
    })


@router.get("/archive", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def archive_page(request: Request):
    from ... import application as host

    host._require_v2()
    return host.templates.TemplateResponse(
        "archive.html",
        {
            "request": request,
            "active_nav": "archive",
            "page_title": "Archive",
            "rows": host.db.list_archived(),
            "archive_enabled": True,
        },
    )


@router.post(
    "/recurring/{row_uuid}/snooze",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def recurring_snooze(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    before = _recurring_row(row_uuid)
    if not before:
        raise HTTPException(404)
    form = dict(await request.form())
    try:
        days = int(form.get("days", "1"))
    except ValueError:
        raise HTTPException(400, "days must be an integer")
    try:
        if not host.db.snooze_recurring(row_uuid, days):
            raise HTTPException(404)
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        raise _task_write_error(exc) from None
    row = _recurring_row(row_uuid)
    if not row:
        raise HTTPException(404)
    op_id = host._stash_undo("recurring_tasks", before,
                        f"Snoozed ‘{before.get('task','')}’ {days}d")
    trigger = host._hx_trigger(
        showUndo={"op_id": op_id,
                  "label": f"Snoozed ‘{before.get('task','')}’ by {days}d",
                  "ttl_ms": host._UNDO_TTL_SECONDS * 1000},
    )
    return host.templates.TemplateResponse(
        "partials/task_card.html",
        {"request": request, "t": row, "endpoint_root": "/recurring"},
        headers={"HX-Trigger": trigger},
    )


@router.get("/follow-ups", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def follow_ups_page(request: Request):
    from ... import application as host

    host._require_v2()
    rows = host.db.list_follow_ups()
    return host.templates.TemplateResponse(
        "follow_ups.html",
        {
            "request": request,
            "active_nav": "follow-ups",
            "page_title": "Follow-ups",
            "rows": rows,
        },
    )


@router.get("/follow-ups/panel", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def follow_ups_panel(request: Request):
    """The follow-up rules manager as a standalone partial, loaded into the
    Tasks-page modal (the feature lives there now rather than as a nav tab)."""
    from ... import application as host

    host._require_v2()
    return host.templates.TemplateResponse(
        "partials/follow_ups_panel.html",
        {"request": request, "rows": host.db.list_follow_ups()},
    )


@router.get("/follow-ups/new", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def follow_ups_new_form(request: Request):
    from ... import application as host

    return host.templates.TemplateResponse(
        "partials/follow_up_form.html",
        {
            "request": request,
            "f": {},
            "is_new": True,
            "trigger_tasks": host.db.list_task_names(),
        },
    )


@router.post("/follow-ups", dependencies=[Depends(require_auth)])
async def follow_ups_create(request: Request):
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    host.db.create_follow_up(form)
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(
            flashSuccess={"message": "Follow-up rule created"},
            closeModal=None,
        ),
        "HX-Refresh": "true",
    })


@router.get(
    "/follow-ups/{row_uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def follow_ups_edit_form(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    row = host.db.get_follow_up(row_uuid)
    if not row:
        raise HTTPException(404)
    return host.templates.TemplateResponse(
        "partials/follow_up_form.html",
        {
            "request": request,
            "f": row,
            "is_new": False,
            "trigger_tasks": host.db.list_task_names(),
        },
    )


@router.post("/follow-ups/{row_uuid}", dependencies=[Depends(require_auth)])
async def follow_ups_update(request: Request, row_uuid: str):
    from ... import application as host

    host._require_v2()
    form = dict(await request.form())
    host.db.update_follow_up(row_uuid, form)
    return Response(status_code=204, headers={
        "HX-Trigger": host._hx_trigger(
            flashSuccess={"message": "Follow-up rule saved"},
            closeModal=None,
        ),
        "HX-Refresh": "true",
    })


@router.post("/follow-ups/{row_uuid}/delete", dependencies=[Depends(require_auth)])
def follow_ups_delete(row_uuid: str):
    from ... import application as host

    host._require_v2()
    host.db.delete_follow_up(row_uuid)
    return Response(status_code=200, content="", headers={"HX-Trigger": "closeModal"})


@router.post("/undo/{op_id}", dependencies=[Depends(require_auth)])
def undo(op_id: str):
    from ... import application as host

    host._require_v2()
    entry = host._pop_undo(op_id)
    if entry is None:
        raise HTTPException(410, "undo window has expired")
    try:
        table = entry["table"]
        if table == "discipline_list":
            host.db.restore_discipline_row(entry["snapshot"])
        else:
            host.db.restore_task_row(
                table,
                entry["snapshot"],
                entry.get("generated_task_uuids", []),
                entry.get("task_event_uuid"),
                entry.get("task_event_type"),
            )
            host.operations.restore_task_records(entry.get("operation_records"))
    except (ValueError, occurrences.OccurrenceStorageError) as exc:
        if isinstance(exc, occurrences.OccurrenceStorageError) or str(exc) == occurrences.HISTORY_MESSAGE:
            with host._UNDO_LOCK:
                host._UNDO_QUEUE[op_id] = entry
                host._sweep_undo()
            raise _task_write_error(exc) from None
        raise HTTPException(409, "Undo could not be applied. Reload and try again.") from None
    except Exception:
        raise HTTPException(500, "Undo could not be completed.") from None
    return Response(
        status_code=200,
        content="",
        headers={"HX-Trigger": host._hx_trigger(reloadBoard=None, undoCleared=None)},
    )
