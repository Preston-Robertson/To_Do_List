"""Finance HTTP routes.

All data routes require both the main application session and the separate
Finance unlock session. Finance is deliberately absent from chat tools and
global search.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import finance
from .auth import (
    finance_is_configured,
    finance_lock_response,
    finance_unlock_response,
    require_auth,
    require_finance_auth,
)

from .paths import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_AUTH = [Depends(require_auth), Depends(require_finance_auth)]


def _trigger(message: str) -> str:
    return json.dumps({"flashSuccess": {"message": message}, "closeModal": None})


def _refresh(message: str) -> Response:
    return Response(
        status_code=204,
        headers={"HX-Trigger": _trigger(message), "HX-Refresh": "true"},
    )


def _form_data(form: Any) -> dict[str, Any]:
    return {str(key): value for key, value in dict(form).items()}


@router.get(
    "/finance/unlock",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
def finance_unlock_page(request: Request):
    return templates.TemplateResponse(
        "finance_unlock.html",
        {
            "request": request,
            "active_nav": "finance",
            "page_title": "Unlock Finance",
            "configured": finance_is_configured(),
            "error": None,
        },
    )


@router.post(
    "/finance/unlock",
    dependencies=[Depends(require_auth)],
)
def finance_unlock(request: Request, token: str = Form(...)):
    try:
        response = finance_unlock_response(token)
        if request.headers.get("HX-Request") == "true":
            response.status_code = 200
            del response.headers["location"]
            response.headers["HX-Redirect"] = "/finance"
        return response
    except (HTTPException, RuntimeError) as exc:
        missing_host_token = isinstance(exc, RuntimeError)
        is_htmx = request.headers.get("HX-Request") == "true"
        template_name = "partials/finance_unlock_panel.html" if is_htmx else "finance_unlock.html"
        return templates.TemplateResponse(
            template_name,
            {
                "request": request,
                "active_nav": "finance",
                "page_title": "Unlock Finance",
                "configured": finance_is_configured(),
                "error": (
                    "Finance is not configured on this host yet. This field "
                    "unlocks an existing host token; it does not create or save one."
                    if missing_host_token else
                    "The Finance token is incorrect."
                ),
            },
            status_code=200 if is_htmx else (503 if missing_host_token else 401),
        )


@router.post("/finance/lock", dependencies=_AUTH)
def finance_lock():
    return finance_lock_response()


@router.get("/finance", response_class=HTMLResponse, dependencies=_AUTH)
def finance_page(request: Request, month: str | None = None):
    try:
        state = finance.dashboard(month)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return templates.TemplateResponse(
        "finance.html",
        {
            "request": request,
            "active_nav": "finance",
            "page_title": "Finance",
            "state": state,
            "snapshots": finance.list_net_worth_snapshots(),
            "reports": finance.list_saved_reports(),
            "audit_events": finance.list_audit_events(20),
        },
    )


@router.get("/finance/notifications", response_class=HTMLResponse, dependencies=_AUTH)
def finance_notifications(request: Request):
    return templates.TemplateResponse(
        "partials/finance_notifications.html",
        {"request": request, "alerts": finance.finance_alerts()},
    )


@router.get("/finance/accounts/new", response_class=HTMLResponse, dependencies=_AUTH)
def finance_account_form(request: Request):
    return templates.TemplateResponse(
        "partials/finance_account_form.html",
        {"request": request, "account": None, "base_currency": finance.base_currency()},
    )


@router.get("/finance/accounts/{account_id}/edit", response_class=HTMLResponse, dependencies=_AUTH)
def finance_account_edit(request: Request, account_id: str):
    account = finance.get_account(account_id)
    if not account:
        raise HTTPException(404, "account not found")
    account["opening_balance"] = finance.from_minor(account["opening_balance_minor"])
    return templates.TemplateResponse(
        "partials/finance_account_form.html",
        {"request": request, "account": account, "base_currency": finance.base_currency()},
    )


@router.post("/finance/accounts", dependencies=_AUTH)
async def finance_account_create(request: Request):
    try:
        finance.create_account(_form_data(await request.form()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh("Account alias created")


@router.post("/finance/accounts/{account_id}", dependencies=_AUTH)
async def finance_account_update(request: Request, account_id: str):
    try:
        if not finance.update_account(account_id, _form_data(await request.form())):
            raise HTTPException(404, "account not found")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh("Account updated")


@router.get("/finance/transactions/new", response_class=HTMLResponse, dependencies=_AUTH)
def finance_transaction_form(request: Request):
    return templates.TemplateResponse(
        "partials/finance_transaction_form.html",
        {"request": request, "accounts": finance.list_accounts(), "today": finance.iso_date(str(time.strftime("%Y-%m-%d")))},
    )


@router.post("/finance/transactions", dependencies=_AUTH)
async def finance_transaction_create(request: Request):
    try:
        finance.create_transaction(_form_data(await request.form()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh("Transaction recorded")


@router.post("/finance/transactions/{transaction_id}/delete", dependencies=_AUTH)
def finance_transaction_delete(transaction_id: str):
    if not finance.delete_transaction(transaction_id):
        raise HTTPException(404, "transaction not found")
    return _refresh("Transaction deleted")


@router.get("/finance/budgets/new", response_class=HTMLResponse, dependencies=_AUTH)
def finance_budget_form(request: Request, month: str | None = None):
    return templates.TemplateResponse(
        "partials/finance_budget_form.html",
        {"request": request, "month": finance.month_value(month)},
    )


@router.post("/finance/budgets", dependencies=_AUTH)
async def finance_budget_save(request: Request):
    try:
        finance.upsert_budget(_form_data(await request.form()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh("Budget saved")


@router.post("/finance/budgets/{budget_id}/delete", dependencies=_AUTH)
def finance_budget_delete(budget_id: str):
    if not finance.delete_budget(budget_id):
        raise HTTPException(404, "budget not found")
    return _refresh("Budget deleted")


@router.get("/finance/holdings/new", response_class=HTMLResponse, dependencies=_AUTH)
def finance_holding_form(request: Request):
    return templates.TemplateResponse(
        "partials/finance_holding_form.html",
        {"request": request, "accounts": finance.list_accounts(), "today": time.strftime("%Y-%m-%d")},
    )


@router.post("/finance/holdings", dependencies=_AUTH)
async def finance_holding_save(request: Request):
    try:
        finance.upsert_holding(_form_data(await request.form()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh("Holding saved")


@router.post("/finance/holdings/{holding_id}/delete", dependencies=_AUTH)
def finance_holding_delete(holding_id: str):
    if not finance.delete_holding(holding_id):
        raise HTTPException(404, "holding not found")
    return _refresh("Holding deleted")


@router.get("/finance/recurring/new", response_class=HTMLResponse, dependencies=_AUTH)
def finance_recurring_form(request: Request):
    return templates.TemplateResponse(
        "partials/finance_recurring_form.html",
        {"request": request, "accounts": finance.list_accounts(), "today": time.strftime("%Y-%m-%d")},
    )


@router.post("/finance/recurring", dependencies=_AUTH)
async def finance_recurring_save(request: Request):
    try:
        finance.upsert_recurring_item(_form_data(await request.form()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh("Recurring finance item saved")


@router.post("/finance/recurring/{item_id}/delete", dependencies=_AUTH)
def finance_recurring_delete(item_id: str):
    if not finance.delete_recurring_item(item_id):
        raise HTTPException(404, "recurring item not found")
    return _refresh("Recurring finance item deleted")


@router.get("/finance/import", response_class=HTMLResponse, dependencies=_AUTH)
def finance_import_form(request: Request):
    return templates.TemplateResponse(
        "partials/finance_import_form.html",
        {"request": request, "accounts": finance.list_accounts()},
    )


@router.post("/finance/import/preview", response_class=HTMLResponse, dependencies=_AUTH)
async def finance_import_preview(
    request: Request,
    account_id: str = Form(...),
    file: UploadFile = File(...),
):
    content = await file.read(2_000_001)
    try:
        preview = finance.prepare_csv_import(account_id, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return templates.TemplateResponse(
        "partials/finance_import_preview.html",
        {"request": request, "preview": preview},
    )


@router.post("/finance/import/commit", dependencies=_AUTH)
async def finance_import_commit(request: Request):
    form = await request.form()
    try:
        result = finance.commit_csv_import(str(form.get("token") or ""))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh(f"Imported {result['imported']} transactions; skipped {result['skipped']}")


@router.get("/finance/export.csv", dependencies=_AUTH)
def finance_export(month: str | None = None):
    content = finance.transactions_csv(month)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=finance-export.csv",
            "Cache-Control": "no-store",
        },
    )


@router.get("/finance/backup", dependencies=_AUTH)
def finance_backup():
    return JSONResponse(
        finance.backup_payload(),
        headers={
            "Content-Disposition": "attachment; filename=finance-backup.json",
            "Cache-Control": "no-store",
        },
    )


@router.get("/finance/restore", response_class=HTMLResponse, dependencies=_AUTH)
def finance_restore_form(request: Request):
    return templates.TemplateResponse("partials/finance_restore_form.html", {"request": request})


@router.post("/finance/restore", dependencies=_AUTH)
async def finance_restore(file: UploadFile = File(...)):
    content = await file.read(5_000_001)
    if len(content) > 5_000_000:
        raise HTTPException(422, "backup must be 5 MB or smaller")
    try:
        payload = json.loads(content.decode("utf-8"))
        counts = finance.restore_backup(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh(f"Finance backup restored ({sum(counts.values())} rows merged)")


@router.post("/finance/snapshot", dependencies=_AUTH)
def finance_snapshot():
    finance.save_net_worth_snapshot()
    return _refresh("Net worth snapshot saved")


@router.post("/finance/reports", dependencies=_AUTH)
async def finance_report_save(request: Request):
    try:
        finance.save_report(_form_data(await request.form()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _refresh("Report saved")


@router.post("/finance/reports/{report_id}/delete", dependencies=_AUTH)
def finance_report_delete(report_id: str):
    if not finance.delete_saved_report(report_id):
        raise HTTPException(404, "report not found")
    return _refresh("Saved report deleted")
