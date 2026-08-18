"""Shared-token auth for luigi-web.

Single-user, single-token model. The token comes from ``LUIGI_WEB_UI_TOKEN``
and can be presented three ways:

* HttpOnly cookie ``luigi_session`` (set by ``POST /login``)
* ``Authorization: Bearer <token>``
* ``?token=<token>`` query string (convenient for curl / bookmarks)

Comparisons use ``secrets.compare_digest`` to avoid timing side-channels.
"""
from __future__ import annotations

import os
import hashlib
import secrets
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

COOKIE_NAME = "luigi_session"
FINANCE_COOKIE_NAME = "luigi_finance_session"
CSRF_COOKIE_NAME = "luigi_csrf"


def secure_cookies() -> bool:
    return os.environ.get("LUIGI_WEB_SECURE_COOKIES", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _expected_token() -> str:
    token = os.environ.get("LUIGI_WEB_UI_TOKEN")
    if not token:
        raise RuntimeError("LUIGI_WEB_UI_TOKEN is not set")
    return token


def _token_matches(candidate: Optional[str]) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate, _expected_token())


def _expected_finance_token() -> str:
    token = os.environ.get("LUIGI_WEB_FINANCE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("LUIGI_WEB_FINANCE_TOKEN is not set")
    return token


def _finance_session_value() -> str:
    token = _expected_finance_token().encode("utf-8")
    return hashlib.sha256(b"luigi-finance-session\0" + token).hexdigest()


def finance_is_configured() -> bool:
    return bool(os.environ.get("LUIGI_WEB_FINANCE_TOKEN", "").strip())


def is_finance_authenticated(candidate: Optional[str]) -> bool:
    if not candidate or not finance_is_configured():
        return False
    return secrets.compare_digest(candidate, _finance_session_value())


def csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: Optional[str], header_value: Optional[str]) -> bool:
    if not cookie_value or not header_value:
        return False
    return secrets.compare_digest(cookie_value, header_value)


def is_authenticated(
    luigi_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> bool:
    if _token_matches(luigi_session):
        return True
    if authorization and authorization.lower().startswith("bearer "):
        parts = authorization.split(None, 1)
        if len(parts) == 2 and _token_matches(parts[1].strip()):
            return True
    if _token_matches(token):
        return True
    return False


def require_auth(
    request: Request,
    luigi_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
):
    """FastAPI dependency: redirect browsers to /login, 401 API clients."""
    if is_authenticated(luigi_session, authorization, token):
        return True

    accept = request.headers.get("accept", "")
    if "text/html" in accept and request.method == "GET":
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")


def login_response(supplied_token: str) -> RedirectResponse:
    """Validate ``supplied_token`` and, if OK, redirect to / with cookie set."""
    if not _token_matches(supplied_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=supplied_token,
        httponly=True,
        samesite="strict",
        secure=secure_cookies(),
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return resp


def logout_response() -> RedirectResponse:
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(COOKIE_NAME, path="/")
    resp.delete_cookie(FINANCE_COOKIE_NAME, path="/")
    return resp


def finance_unlock_response(supplied_token: str) -> RedirectResponse:
    if not supplied_token or not secrets.compare_digest(
        supplied_token, _expected_finance_token()
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")
    response = RedirectResponse(url="/finance", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=FINANCE_COOKIE_NAME,
        value=_finance_session_value(),
        httponly=True,
        samesite="strict",
        secure=secure_cookies(),
        max_age=60 * 60 * 8,
        path="/",
    )
    return response


def finance_lock_response() -> RedirectResponse:
    response = RedirectResponse(url="/finance/unlock", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(FINANCE_COOKIE_NAME, path="/")
    return response


def require_finance_auth(
    request: Request,
    luigi_finance_session: Optional[str] = Cookie(default=None),
):
    if is_finance_authenticated(luigi_finance_session):
        return True
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/finance/unlock"},
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="finance is locked")
