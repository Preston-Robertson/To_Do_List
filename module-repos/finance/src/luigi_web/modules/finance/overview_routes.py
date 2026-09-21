"""Isolated private overview routes, ready for Finance manifest composition."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ...auth import (CSRF_COOKIE_NAME, csrf_matches, is_authenticated,
                     require_auth, require_finance_auth)
from ...core.templating import create_templates
from . import overview
from . import repository as finance

PRIVATE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Expires": "0",
                   "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
templates = create_templates()


def _private_error(status: int, message: str, *, location: str | None = None):
    headers = dict(PRIVATE_HEADERS)
    if location in {"/login", "/finance/unlock"}:
        headers["Location"] = location
    return JSONResponse({"detail": message}, status_code=status, headers=headers)


async def overview_privacy_middleware(request: Request, call_next):
    """Parent installs OUTSIDE host CSRF to cover its early rejection responses."""
    if not (request.url.path == "/finance" or request.url.path.startswith("/finance/")):
        return await call_next(request)
    try:
        response = await call_next(request)
    except Exception:
        return _private_error(500, "Finance is temporarily unavailable")
    response.headers.update(PRIVATE_HEADERS)
    return response


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request):
            try:
                if request.url.query:
                    return _private_error(400, "Use the private filter form")
                response = await handler(request)
            except RequestValidationError:
                return _private_error(422, "Invalid overview request")
            except StarletteHTTPException as error:
                message = {401: "Authentication required", 403: "Finance access denied",
                           400: "Invalid overview request", 413: "Overview request is too large",
                           415: "Use the private filter form", 422: "Invalid overview filters"}.get(
                               error.status_code, "Finance request could not be completed")
                return _private_error(error.status_code, message,
                                      location=(error.headers or {}).get("Location"))
            except Exception:
                return _private_error(500, "Finance is temporarily unavailable")
            response.headers.update(PRIVATE_HEADERS)
            return response

        return private_handler

    async def handle(self, scope, receive, send):
        if self.methods and scope["method"] not in self.methods:
            response = _private_error(405, "Method not allowed")
            response.headers["Allow"] = ", ".join(sorted(self.methods))
            await response(scope, receive, send)
            return
        await super().handle(scope, receive, send)


def _require_private_post(request: Request):
    if request.method != "POST":
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        try:
            parsed = urlsplit(origin)
            expected = urlsplit(str(request.base_url))
        except ValueError:
            raise HTTPException(403) from None
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            raise HTTPException(403)
    if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
        raise HTTPException(403)
    if is_authenticated(None, request.headers.get("authorization"), None):
        return
    if not origin or not csrf_matches(request.cookies.get(CSRF_COOKIE_NAME), request.headers.get("x-csrf-token")):
        raise HTTPException(403)


router = APIRouter(route_class=PrivateRoute,
                   dependencies=[Depends(require_auth), Depends(require_finance_auth), Depends(_require_private_post)])


def overview_context(request: Request, **filters):
    state = overview.overview_state(**filters)
    chart_seed = {
        "currency": state["currency"],
        "trend": [{"month": row["month"], "inflow_minor": str(row["inflow_minor"]),
                   "outflow_minor": str(row["outflow_minor"])} for row in state["trend"]],
        "snapshots": [{"date": row["snapshot_date"], "value_minor": str(row["total_minor"])}
                      for row in state["snapshots"]],
        "allocation": [{"label": row["label"], "value_minor": str(row["value_minor"])}
                       for row in state["allocation_accounts"]],
    }
    return {"request": request, "active_nav": "finance", "page_title": "Finance overview",
            "state": state, "chart_seed": chart_seed, "minor_text": finance.from_minor}


@router.get("/finance")
@router.get("/finance/overview")
def finance_overview_page(request: Request):
    return templates.TemplateResponse("finance_overview.html", overview_context(request))


@router.post("/finance/overview/filter")
async def finance_overview_filter(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise HTTPException(413)
        body.extend(chunk)
    try:
        entries = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                            encoding="utf-8", errors="strict", max_num_fields=9)
        form = dict(entries)
        allowed = {"month", "account_id", "category", "query", "include_memo", "page", "page_size", "status"}
        if len(entries) != len(form) or not set(form) <= allowed or form.get("include_memo", "0") not in {"0", "1"}:
            raise ValueError("Invalid overview filters")
        context = await run_in_threadpool(
            overview_context, request, month=form.get("month") or None, account_id=form.get("account_id", ""),
            category=form.get("category", ""), query=form.get("query", ""),
            include_memo=form.get("include_memo") == "1", page=int(form.get("page", "1")),
            page_size=int(form.get("page_size", "50")), status=form.get("status", "all"))
    except (ValueError, UnicodeError, OverflowError):
        raise HTTPException(422) from None
    return templates.TemplateResponse("finance_overview.html", context)