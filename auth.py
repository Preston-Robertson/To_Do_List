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
import secrets
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

COOKIE_NAME = "luigi_session"


def _expected_token() -> str:
    token = os.environ.get("LUIGI_WEB_UI_TOKEN")
    if not token:
        raise RuntimeError("LUIGI_WEB_UI_TOKEN is not set")
    return token


def _token_matches(candidate: Optional[str]) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate, _expected_token())


def is_authenticated(
    luigi_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> bool:
    if _token_matches(luigi_session):
        return True
    if authorization and authorization.lower().startswith("bearer "):
        if _token_matches(authorization.split(None, 1)[1].strip()):
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
        samesite="lax",
        # NOTE: not marking Secure — LAN-only, plain HTTP. Flip on if put behind TLS.
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return resp


def logout_response() -> RedirectResponse:
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
