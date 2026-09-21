"""Disposable synthetic Finance preview. Never run inside the production process."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import timedelta
import hashlib
from http.cookies import SimpleCookie
from importlib import import_module, util
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from luigi_web.core.static_assets import ModuleStaticFiles

_ACTIVE = False
PRIVATE_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "Expires": "0", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
}


def synthetic_csv():
    from luigi_web import clock

    return ("date,amount,category,memo\n"
            f"{clock.local_today().isoformat()},-12.34,Example import,Example merchant import\n").encode("ascii")


def seed_synthetic():
    from luigi_web import clock
    from luigi_web.modules.finance import plans, repository as finance
    from luigi_web.modules.finance.projections import add_months

    today = clock.local_today()
    selected = today.replace(day=1)
    accounts = [finance.create_account({"name": alias, "account_type": kind, "opening_balance": amount})
                for alias, kind, amount in (("Everyday account", "checking", "1234.50"),
                                            ("Brokerage", "investment", "0.00"),
                                            ("Credit account", "credit", "-123.45"))]
    for offset in (-2, -1, 0):
        month = add_months(selected, offset)
        for index in range(65 if offset == 0 else 8):
            finance.create_transaction({
                "account_id": accounts[2] if index % 7 == 0 else accounts[0],
                "transaction_date": (month + timedelta(days=index % 28)).isoformat(),
                "amount": "123.45" if index % 5 == 0 else "-12.34",
                "category": "Example income" if index % 5 == 0 else ("Example food" if index % 2 else "Example travel"),
                "memo": f"Example merchant {index + 1}",
            })
        finance.save_net_worth_snapshot(month.isoformat())
    for category, amount in (("Example food", "123.45"), ("Example travel", "1234.50")):
        finance.upsert_budget({"month": selected.strftime("%Y-%m"), "category": category, "limit": amount})
    for index, age in enumerate((0, 21)):
        finance.upsert_holding({"account_id": accounts[1], "symbol": f"DEMO{index + 1}",
                                "asset_name": f"Example holding {index + 1}", "quantity": "2",
                                "cost_basis": "123.45", "market_value": "234.56",
                                "as_of_date": (today - timedelta(days=age)).isoformat()})
    for name, amount, delta in (("Example scheduled income", "123.45", 1),
                                 ("Example scheduled bill", "-12.34", 3),
                                 ("Example overdue bill", "-12.34", -2)):
        finance.upsert_recurring_item({"account_id": accounts[0], "name": name, "category": "Example schedule",
                                      "amount": amount, "cadence": "monthly",
                                      "next_due_date": (today + timedelta(days=delta)).isoformat()})
    for index, cadence in enumerate(("monthly", "biweekly")):
        plans.save_income({"name": f"Example income stream {index + 1}", "amount_minor": 12345,
                           "cadence": cadence, "start_date": selected.isoformat(), "currency": "USD"})
    baseline = {"opening_cash_minor": 123450, "net_income_minor": 12345, "variable_expense_minor": 1234,
                "reserve_minor": 12345, "income_mode": "streams", "months": 12}
    plans.save_plan("cashflow", "Example baseline", baseline)
    plans.save_plan("cashflow", "Example alternative", {**baseline, "monthly_contribution_minor": 1234})
    plans.save_plan("housing", "Example housing", {"rent_minor": 1234, "home_price_minor": 123450,
                                                  "down_payment_minor": 12345, "years": 3})
    finance.save_report({"name": "Example monthly report", "report_type": "monthly"})


def ledger_metadata():
    from luigi_web.modules.finance import repository as finance

    tables = ("finance_accounts", "finance_transactions", "finance_budgets", "finance_holdings",
              "finance_recurring_items", "finance_net_worth_snapshots", "finance_saved_reports")
    digest = hashlib.sha256()
    counts = []
    with finance.connect() as connection:
        for table in tables:
            rows = [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            counts.append(len(rows))
            digest.update(json.dumps(rows, separators=(",", ":")).encode("utf-8"))
    return tuple(counts), digest.hexdigest()


def _blocked(*args, **kwargs):
    raise RuntimeError("Unavailable in synthetic Finance preview")


def cookie_names(port):
    return tuple(f"luigi_finance_preview_{kind}_{port}" for kind in ("ui", "finance", "csrf"))


def _matches(candidate, expected):
    return bool(candidate and candidate.isascii() and secrets.compare_digest(candidate, expected))


def configure_security(app, main_token, main_session, finance_session):
    from luigi_web import auth

    allowed = [(route.path_regex, route.methods) for route in app.routes if isinstance(route, APIRoute)]

    @app.middleware("http")
    async def security(request: Request, call_next):
        def deny(status):
            return JSONResponse({"detail": "Synthetic preview request unavailable"}, status_code=status,
                                headers=PRIVATE_HEADERS)

        try:
            try:
                port = request.url.port or 80
            except ValueError:
                return deny(403)
            valid_host = request.headers.get("host") == f"127.0.0.1:{port}"
            valid_port = app.state.preview_port is None or port == app.state.preview_port
            if (not valid_host or not valid_port or request.url.scheme != "http" or not request.client
                    or request.client.host not in {"127.0.0.1", "::1"}):
                return deny(403)
            if request.headers.get("sec-fetch-site", "") not in {"", "none", "same-origin"}:
                return deny(403)
            path = request.url.path
            if path == "/finance/restore":
                return deny(403)
            if path == "/finance/unlock" and request.method != "GET":
                return deny(403)
            static = path.startswith(("/static/", "/module-assets/finance/")) and request.method == "GET"
            bootstrap = path == "/" and request.method == "GET"
            if not (static or bootstrap or any(regex.fullmatch(path) and request.method in methods
                                               for regex, methods in allowed)):
                return deny(404)
            query = parse_qsl(request.url.query, keep_blank_values=True)
            if request.url.query and (not static or any(key != "v" or not value.isdecimal() for key, value in query)):
                return deny(400)
            ui_name, finance_name, csrf_name = cookie_names(port)
            cookies = request.cookies
            valid_ui = _matches(cookies.get(ui_name), main_session)
            valid_finance = _matches(cookies.get(finance_name), finance_session)
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            expected = f"http://127.0.0.1:{port}"
            if origin is not None and origin != expected:
                return deny(403)
            if referer is not None:
                source = urlsplit(referer)
                if source.username or source.password or f"{source.scheme}://{source.netloc}" != expected:
                    return deny(403)
            if request.method not in {"GET", "HEAD"}:
                if origin is None and referer is None:
                    return deny(403)
                if not valid_ui or not _matches(request.headers.get("x-csrf-token"), cookies.get(csrf_name, "")):
                    return deny(403)
            forwarded = SimpleCookie()
            if valid_ui:
                forwarded[auth.COOKIE_NAME] = main_token
            if valid_finance:
                forwarded[auth.FINANCE_COOKIE_NAME] = auth._finance_session_value()
            if cookies.get(csrf_name):
                forwarded[auth.CSRF_COOKIE_NAME] = cookies[csrf_name]
            request.scope["headers"] = [
                (name, value) for name, value in request.scope["headers"]
                if name.lower() not in {b"cookie", b"authorization"}
            ] + [(b"cookie", forwarded.output(header="", sep=";").strip().encode("ascii"))]
            if bootstrap:
                response = RedirectResponse("/finance", status_code=303)
                if not valid_ui:
                    response.set_cookie(ui_name, main_session, httponly=True, samesite="strict")
                if not valid_finance:
                    response.set_cookie(finance_name, finance_session, httponly=True, samesite="strict")
                if not valid_ui or not cookies.get(csrf_name):
                    response.set_cookie(csrf_name, auth.csrf_token(), samesite="strict")
            else:
                response = await call_next(request)
                raw_headers = []
                for name, value in response.raw_headers:
                    if name.lower() == b"set-cookie":
                        outgoing = SimpleCookie()
                        outgoing.load(value.decode("latin-1"))
                        if auth.FINANCE_COOKIE_NAME in outgoing:
                            continue
                        if any(key in outgoing for key in (auth.COOKIE_NAME, auth.CSRF_COOKIE_NAME)):
                            continue
                    raw_headers.append((name, value))
                response.raw_headers = raw_headers
                if path == "/finance/lock" and response.status_code == 303:
                    response.delete_cookie(finance_name, httponly=True, samesite="strict")
                if response.status_code >= 400:
                    return deny(response.status_code)
                if static and path.endswith(".js") and response.status_code == 200:
                    body = b"".join([part async for part in response.body_iterator])
                    body = body.replace(b"luigi_csrf", csrf_name.encode("ascii"))
                    response = Response(body, headers={key: value for key, value in response.headers.items()
                                                       if key not in {"content-length", "etag"}})
                if path == "/finance/unlock" and response.status_code == 200:
                    body = b"".join([part async for part in response.body_iterator])
                    body = body.replace(b"</body>", b'<a href="/">Start synthetic preview</a></body>')
                    response = HTMLResponse(body, headers={key: value for key, value in response.headers.items()
                                                          if key not in {"content-length", "content-type"}})
            response.headers.update(PRIVATE_HEADERS)
            return response
        except Exception:
            return deny(500)


class BoundedBody:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 2_100_000:
                response = JSONResponse({"detail": "Synthetic preview request unavailable"},
                                        status_code=413, headers=PRIVATE_HEADERS)
                return await response(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


@contextmanager
def preview_context():
    global _ACTIVE
    if _ACTIVE or "luigi_web.application" in sys.modules:
        raise RuntimeError("Finance preview requires a fresh process without the host")
    with tempfile.TemporaryDirectory(prefix="luigi-finance-preview-") as temporary, ExitStack() as stack:
        directory = Path(temporary).resolve()
        retained = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "COMSPEC"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in retained}
        main_token, unlock_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        environment.update({key: str(directory) for key in (
            "TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE",
        )})
        environment.update({
            "LUIGI_WEB_DATA_DIR": str(directory), "LUIGI_WEB_FINANCE_DB": str(directory / "finance.sqlite3"),
            "LUIGI_WEB_MODULES_FILE": str(directory / "absent-modules.json"), "LUIGI_WEB_MODULES": "finance",
            "LUIGI_WEB_UI_TOKEN": main_token, "LUIGI_WEB_FINANCE_TOKEN": unlock_token,
            "LUIGI_WEB_SECURE_COOKIES": "0", "LUIGI_WEB_TIMEZONE": "UTC", "PYTHON_DOTENV_DISABLED": "1",
        })
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch.object(tempfile, "tempdir", str(directory)))
        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        stack.callback(logging.disable, previous_logging)
        import dotenv
        import sqlalchemy
        import sqlalchemy.engine.create

        for name in ("load_dotenv", "dotenv_values", "find_dotenv"):
            stack.enter_context(patch.object(dotenv, name, _blocked))
            stack.enter_context(patch.object(dotenv.main, name, _blocked))
        for driver_name in ("psycopg", "psycopg2"):
            if util.find_spec(driver_name) is not None:
                driver = import_module(driver_name)
                stack.enter_context(patch.object(driver, "connect", _blocked))
                for class_name in ("Connection", "AsyncConnection"):
                    if hasattr(driver, class_name):
                        stack.enter_context(patch.object(getattr(driver, class_name), "connect", _blocked))
        stack.enter_context(patch.object(sqlalchemy, "create_engine", _blocked))
        stack.enter_context(patch.object(sqlalchemy.engine.create, "create_engine", _blocked))
        for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            stack.enter_context(patch.object(socket, name, _blocked))
        original_connect = socket.socket.connect
        socketpair_code = getattr(socket.socketpair, "__code__", None)

        def guarded_connect(connection, address):
            if (socketpair_code is not None and sys._getframe(1).f_code is socketpair_code
                    and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}):
                return original_connect(connection, address)
            return _blocked()

        stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        for name in ("connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, name, _blocked))
        connect = sqlite3.connect

        def temporary_sqlite(database, *args, **kwargs):
            if (kwargs.get("uri") or len(args) > 6 or (str(database) != ":memory:" and
                    not Path(database).resolve().is_relative_to(directory))):
                return _blocked()
            connection = connect(database, *args, **kwargs)
            connection.set_authorizer(lambda action, *unused: sqlite3.SQLITE_DENY
                                      if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK)
            return connection

        stack.enter_context(patch.object(sqlite3, "connect", temporary_sqlite))
        stack.enter_context(patch.object(sqlite3.dbapi2, "connect", temporary_sqlite))
        from luigi_web.core.module_registry import build_registry, mount_modules
        from luigi_web.modules.finance import plans, repository
        from luigi_web.paths import STATIC_DIR

        stack.enter_context(patch.object(repository, "_IMPORT_CACHE", {}))
        repository.init_db()
        plans.init_db()
        seed_synthetic()
        prepare_import = repository.prepare_csv_import

        def synthetic_import(account_id, content):
            if content != synthetic_csv():
                raise ValueError("Only the generated synthetic CSV fixture is accepted")
            return prepare_import(account_id, content)

        stack.enter_context(patch.object(repository, "prepare_csv_import", synthetic_import))
        app = FastAPI(title="Synthetic Finance preview", docs_url=None, redoc_url=None, openapi_url=None)
        app.mount("/static", ModuleStaticFiles(directory=str(STATIC_DIR)), name="static")
        mount_modules(app, build_registry("finance"))
        app.state.preview_directory = directory
        app.state.preview_port = None
        configure_security(app, main_token, secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        app.add_middleware(BoundedBody)
        _ACTIVE = True
        try:
            yield app
        finally:
            _ACTIVE = False


def preview_client(app, *, port=58111, client_host="127.0.0.1"):
    from fastapi.testclient import TestClient

    async def transport(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (client_host, 50000)}
        await app(scope, receive, send)

    return TestClient(transport, base_url=f"http://127.0.0.1:{port}")


def smoke_check(app):
    from luigi_web import clock
    from luigi_web.modules.finance import plans

    counts = {"views": 0, "mutations": 0}

    def expect(condition):
        if not condition:
            raise RuntimeError("Synthetic Finance check failed")

    with preview_client(app) as client:
        expect(client.get("/").status_code == 200)
        for path in ("/finance", "/finance/overview", "/finance/planning", "/finance/records",
                     "/finance/planning/state", "/finance/notifications", "/finance/backup", "/finance/export.csv",
                     "/finance/accounts/new", "/finance/transactions/new", "/finance/budgets/new",
                     "/finance/holdings/new", "/finance/recurring/new", "/finance/import"):
            response = client.get(path)
            expect(response.status_code == 200)
            expect(all(response.headers.get(key) == value for key, value in PRIVATE_HEADERS.items()))
            counts["views"] += 1
        ui_name, finance_name, csrf_name = cookie_names(58111)
        headers = {"origin": "http://127.0.0.1:58111", "x-csrf-token": client.cookies[csrf_name]}

        def post(path, **kwargs):
            response = client.post(path, headers=headers, follow_redirects=False, **kwargs)
            expect(response.status_code in {200, 204, 303})
            counts["mutations"] += 1
            return response

        before = ledger_metadata()
        for data in ({"page": "2"}, {"status": "inflows"}, {"query": "No matching synthetic category"}):
            post("/finance/overview/filter", data=data)
        saved = plans.list_plans("cashflow")
        for kind, config in (("cashflow", saved[0]["config"]), ("wealth", saved[0]["config"]),
                             ("housing", plans.list_plans("housing")[0]["config"])):
            post("/finance/planning/calculate", json={"kind": kind, "config": config})
        post("/finance/planning/compare", json={"kind": "cashflow", "plan_ids": [row["id"] for row in saved]})
        record = post("/finance/planning/save", json={"kind": "cashflow", "name": "Example smoke plan",
                                                      "config": saved[0]["config"]}).json()["record"]
        expect(plans.get_plan(record["id"]) == record)
        post("/finance/planning/delete", json={"id": record["id"], "expected_version": record["version"]})
        income = post("/finance/planning/income/save", json={"name": "Example smoke income", "amount_minor": 1234,
                      "cadence": "monthly", "start_date": clock.local_today().isoformat()}).json()["record"]
        expect(any(row["id"] == income["id"] for row in plans.list_income()))
        post("/finance/planning/income/delete", json={"id": income["id"], "expected_version": income["version"]})
        expect(ledger_metadata() == before)
        expect(client.post("/finance/snapshot").status_code == 403)
        expect(client.post("/finance/snapshot", headers={"x-csrf-token": client.cookies[csrf_name]}).status_code == 403)
        expect(client.get("/finance?token=synthetic-rejected").status_code == 400)
        expect(client.get("/command-palette").status_code == 404)
        post("/finance/lock")
        expect(ui_name in client.cookies and finance_name not in client.cookies)
        for path in ("/finance", "/finance/planning/state", "/finance/records", "/finance/backup"):
            expect(client.get(path).status_code == 403)
        expect(client.get("/finance/unlock").status_code == 200)
        expect(finance_name not in client.cookies)
        expect(client.get("/").status_code == 200)
        expect(ledger_metadata() == before)
        expect("luigi_web.application" not in sys.modules)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if not 0 <= arguments.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        with preview_context() as app:
            if arguments.check:
                counts = smoke_check(app)
                print(f"Synthetic Finance checks passed: {counts['views']} views, {counts['mutations']} operations; ledger unchanged.")
                return
            import uvicorn

            with socket.socket() as listener:
                listener.bind(("127.0.0.1", arguments.port))
                port = listener.getsockname()[1]
                app.state.preview_port = port
                print(f"http://127.0.0.1:{port}/", flush=True)
                uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off",
                                             access_log=False, log_level="critical", proxy_headers=False)).run(sockets=[listener])
    except Exception:
        print("Synthetic Finance preview unavailable.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()