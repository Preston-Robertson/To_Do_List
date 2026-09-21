"""Trusted live-preview controller and separately deployed authentication gateway.

Parent review routes must authenticate/CSRF-check the admin before create_ticket,
then POST the ticket in a form body to preview_origin() + '/session'. Never put
capabilities in URLs or serve candidate assets from the production origin.
"""
from __future__ import annotations

import asyncio
import argparse
import base64
from contextlib import asynccontextmanager, contextmanager
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import maintainer, review, review_worker, sandbox

PORT = 58120
COOKIE = "__Host-maintainer_preview"
BACKEND = "http://127.0.0.1:58120"
MAX_BODY = 1024 * 1024
MAX_RESPONSE = 5 * 1024 * 1024
MAX_HEADER = 4096
HTMX_REQUEST_HEADERS = ("hx-request", "hx-target", "hx-trigger", "hx-trigger-name", "hx-prompt")
READY_STATES = {"testing", "release_queued", "releasing"}
PRIVATE_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-src 'none'; "
    "frame-ancestors 'none'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; font-src 'self'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'; worker-src 'none'",
}


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (not value.isascii() or any(character.isspace() for character in value)
                or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or "?" in value or "#" in value
                or "\\" in value or "%" in value or parsed.hostname.endswith(".")):
            raise ValueError
        port = parsed.port
        hostname = parsed.hostname
        if not all(character.isalnum() or character in "-." for character in hostname):
            raise ValueError
        return "https://" + hostname + (f":{port}" if port and port != 443 else "")
    except (ValueError, TypeError):
        raise ValueError("A dedicated HTTPS root origin is required.") from None


def preview_origin() -> str:
    origin = _origin(os.environ.get("LUIGI_MAINTAINER_PREVIEW_URL", ""))
    production = _origin(os.environ.get("LUIGI_MAINTAINER_UI_URL", ""))
    if urlsplit(origin).hostname == urlsplit(production).hostname:
        raise ValueError("Preview and production must use different hostnames.")
    return origin


def _state_root() -> Path:
    queue = maintainer.db_path().absolute().parent
    return review_worker._protected_root(Path(os.environ.get(
        "LUIGI_MAINTAINER_PREVIEW_STATE_DIR", str(queue / "review-preview"))))


def _active(*, allow_expired: bool = False) -> dict:
    state = json.loads(review_worker._read(_state_root() / "active.json", 4096))
    review_worker._identifier(state["run_id"])
    review_worker._identifier(state["instance"])
    review._commit(state["head_commit"])
    review._digest(state["source_digest"])
    if (state["container_name"] != "luigi-preview-" + state["instance"].replace("-", "")
            or type(state["expires"]) is not int or state["expires"] > time.time() + 3600
            or not allow_expired and state["expires"] <= time.time()):
        raise ValueError("Preview is unavailable.")
    return state


def _binding(state: dict, *, recorded: bool = True, connection=None) -> dict:
    run = review._run(connection, state["run_id"]) if connection else review.get_run(state["run_id"])
    if not run or run["state"] not in READY_STATES or run["head_commit"] != state["head_commit"]:
        raise ValueError("Preview is unavailable.")
    option = review._selected(connection, run) if connection else review_worker._selected(run)
    if (option["tree_sha256"] != state["source_digest"]
            or (recorded or run["state"] != "testing") and run["preview_commit"] != state["head_commit"]):
        raise ValueError("Preview is unavailable.")
    return run


def _key() -> bytes:
    value = os.environ.get("LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY", "")
    if not re.fullmatch(r"[0-9a-f]{64,128}", value) or len(value) % 2:
        raise ValueError("A protected random gateway key is required.")
    return bytes.fromhex(value)


def _sign(claims: dict, kind: str) -> str:
    payload = base64.urlsafe_b64encode(review_worker._json(claims)).rstrip(b"=")
    signature = hmac.new(_key(), kind.encode() + b":" + payload, hashlib.sha256).hexdigest()
    return payload.decode("ascii") + "." + signature


def _verify(token: str, kind: str, lifetime: int) -> dict:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}\.[0-9a-f]{64}", token):
        raise ValueError("Invalid preview authorization.")
    payload, signature = token.split(".")
    expected = hmac.new(_key(), kind.encode() + b":" + payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid preview authorization.")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    now = int(time.time())
    if (type(claims) is not dict or set(claims) != {"run_id", "head_commit", "instance", "nonce", "iat", "exp"}
            or type(claims["iat"]) is not int or type(claims["exp"]) is not int
            or not claims["iat"] <= now < claims["exp"] <= claims["iat"] + lifetime
            or not re.fullmatch(r"[0-9a-f]{64}", claims["nonce"])):
        raise ValueError("Expired preview authorization.")
    state = _active()
    current = _binding(state)
    if kind == "ticket" and current["state"] != "testing":
        raise ValueError("Preview authorization is stale.")
    if any(claims[field] != state[field] for field in ("run_id", "head_commit", "instance")):
        raise ValueError("Preview authorization is stale.")
    return claims


def create_ticket(run: dict) -> str:
    """Authenticated parent only: render in a POST body, never a URL or log."""
    preview_origin()
    state = _active()
    current = _binding(state)
    if (current["state"] != "testing"
            or any(run.get(field) != current[field] for field in ("id", "head_commit", "selected_option_id"))):
        raise ValueError("Preview authorization is stale.")
    now = int(time.time())
    claims = {field: state[field] for field in ("run_id", "head_commit", "instance")}
    claims.update(nonce=secrets.token_hex(32), iat=now, exp=min(now + 60, state["expires"]))
    ticket = _sign(claims, "ticket")
    with review._transaction() as connection:
        if _binding(state, connection=connection)["state"] != "testing":
            raise ValueError("Preview authorization is stale.")
        connection.execute("CREATE TABLE IF NOT EXISTS preview_tickets "
                           "(digest TEXT PRIMARY KEY, expires INTEGER NOT NULL)")
        connection.execute("DELETE FROM preview_tickets WHERE expires <= ?", (now,))
        if connection.execute("SELECT count(*) FROM preview_tickets").fetchone()[0] >= 256:
            raise ValueError("Preview ticket limit reached.")
        connection.execute("INSERT INTO preview_tickets VALUES (?, ?)",
                           (hashlib.sha256(ticket.encode()).hexdigest(), claims["exp"]))
    return ticket


def _redeem(ticket: str, run_id: str) -> str:
    claims = _verify(ticket, "ticket", 60)
    if claims["run_id"] != run_id:
        raise ValueError("Preview authorization is stale.")
    with review._transaction() as connection:
        if _binding(_active(), connection=connection)["state"] != "testing":
            raise ValueError("Preview authorization is stale.")
        deleted = connection.execute("DELETE FROM preview_tickets WHERE digest = ? AND expires > ?",
                                     (hashlib.sha256(ticket.encode()).hexdigest(), int(time.time())))
        if deleted.rowcount != 1:
            raise ValueError("Preview ticket has already been used.")
    claims.update(iat=int(time.time()), exp=min(int(time.time()) + 3600, _active()["expires"]))
    return _sign(claims, "session")


def _socket(state: dict) -> Path:
    path = review_worker._plain_path(_state_root() / "r" / state["instance"] / "app.sock")
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Preview socket is unavailable.")
    return path


@asynccontextmanager
async def _socket_address(state: dict):
    try:
        flags = getattr(os, "O_PATH") | getattr(os, "O_NOFOLLOW")
    except AttributeError:
        raise ValueError("Preview socket transport requires Linux no-follow descriptors.") from None
    descriptor = os.open(_socket(state), flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISSOCK(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Preview socket changed.")
        yield f"/proc/self/fd/{descriptor}"
    finally:
        os.close(descriptor)


def _synthetic_cookie(name: str) -> bool:
    return name == "luigi_csrf" or re.fullmatch(
        r"luigi_(?:preview_session|media_preview_session|cards_preview|finance_preview_(?:ui|finance|csrf))_58120",
        name) is not None


def _path(value: str) -> str:
    decoded = unquote(value, errors="strict")
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]*", decoded) or "//" in decoded
            or any(part in {".", ".."} for part in decoded.split("/"))):
        raise ValueError("Invalid preview path.")
    return decoded


def _header(value: str) -> str:
    if len(value) > MAX_HEADER or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Invalid preview header.")
    return value


def _redirect(value: str) -> str:
    parsed = urlsplit(_header(value))
    if (parsed.fragment or parsed.username or parsed.password or parsed.query
            or parsed.netloc and (parsed.scheme + "://" + parsed.netloc) != BACKEND
            or parsed.scheme and not parsed.netloc or value.startswith("//")):
        raise ValueError("External preview redirects are forbidden.")
    return _path(parsed.path)


def _htmx_location(value: str) -> str:
    value = _header(value)
    if not value.lstrip().startswith("{"):
        return _redirect(value)
    location = json.loads(value)
    if (not isinstance(location, dict) or "path" not in location
            or set(location) - {"path", "target", "swap", "select"}
            or any(not isinstance(item, str) for item in location.values())):
        raise ValueError("Invalid preview HTMX location.")
    location["path"] = _redirect(location["path"])
    for item in location.values():
        _header(item)
    return _header(json.dumps(location, separators=(",", ":"), ensure_ascii=True))


def _htmx_trigger(value: str) -> str:
    value = _header(value)
    if value.lstrip().startswith("{"):
        events = json.loads(value)
        if not isinstance(events, dict):
            raise ValueError("Invalid preview HTMX events.")
        result = json.dumps(events, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    else:
        events = [event.strip() for event in value.split(",")]
        result = ",".join(events)
    if (not events or len(events) > 64
            or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", event)
                   or event in {"constructor", "prototype"} for event in events)):
        raise ValueError("Invalid preview HTMX events.")
    return _header(result)


async def _body(request: Request, limit: int) -> bytes:
    result = bytearray()
    async for part in request.stream():
        result.extend(part)
        if len(result) > limit:
            raise ValueError("Preview body exceeds limit.")
    return bytes(result)


async def _proxy(request: Request, state: dict, body: bytes) -> Response:
    path = _path(request.scope.get("raw_path", b"/").decode("ascii"))
    query = request.scope.get("query_string", b"").decode("ascii")
    if len(query) > 4096 or any(name.casefold() in {"ticket", "token", "capability", "authorization"}
                              for name in parse_qs(query)):
        raise ValueError("Invalid preview query.")
    headers = {name: _header(request.headers[name])
               for name in ("accept", "content-type", "x-csrf-token", *HTMX_REQUEST_HEADERS)
               if name in request.headers}
    headers.update({"host": "127.0.0.1:58120", "accept-encoding": "identity"})
    if request.method not in {"GET", "HEAD"}:
        headers["origin"] = BACKEND
    cookies = SimpleCookie()
    for name, value in request.cookies.items():
        if _synthetic_cookie(name):
            cookies[name] = value
    headers["cookie"] = cookies.output(header="", sep=";").strip()
    async with _socket_address(state) as address, httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=address, retries=0), trust_env=False, timeout=30, follow_redirects=False,
    ) as client:
        async with client.stream(request.method, BACKEND + path + ("?" + query if query else ""),
                                 headers=headers, content=body) as upstream:
            content = bytearray()
            async for part in upstream.aiter_raw():
                content.extend(part)
                if len(content) > MAX_RESPONSE:
                    raise ValueError("Preview response exceeds limit.")
            if upstream.status_code >= 400 or upstream.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Preview response unavailable.")
            response = Response(bytes(content), status_code=upstream.status_code)
            for name in ("content-type", "content-language"):
                if name in upstream.headers:
                    response.headers[name] = upstream.headers[name]
            if location := upstream.headers.get("location"):
                response.headers["location"] = _redirect(location)
            if "hx-refresh" in upstream.headers:
                if upstream.headers["hx-refresh"] != "true":
                    raise ValueError("Invalid preview HTMX refresh.")
                response.headers["hx-refresh"] = "true"
            for name, validator in (("hx-redirect", _redirect), ("hx-location", _htmx_location),
                                    ("hx-trigger", _htmx_trigger), ("hx-trigger-after-swap", _htmx_trigger),
                                    ("hx-trigger-after-settle", _htmx_trigger)):
                if name in upstream.headers:
                    response.headers[name] = validator(upstream.headers[name])
            for value in upstream.headers.get_list("set-cookie"):
                outgoing = SimpleCookie()
                outgoing.load(value)
                for name, morsel in outgoing.items():
                    if _synthetic_cookie(name):
                        response.set_cookie(name, morsel.value, max_age=0 if morsel["max-age"] == "0" else None,
                                            secure=True, httponly=bool(morsel["httponly"]), samesite="strict", path="/")
    if _active() != state:
        raise ValueError("Preview changed during request.")
    _binding(state)
    return response


async def _health(state: dict) -> None:
    async with _socket_address(state) as address, httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=address, retries=0), trust_env=False, timeout=3, follow_redirects=False,
    ) as client:
        path = "/"
        for attempt in range(2):
            async with client.stream("GET", BACKEND + path) as response:
                if response.status_code == 303 and attempt == 0:
                    path = _path(response.headers.get("location", ""))
                    continue
                content = bytearray()
                async for part in response.aiter_raw():
                    content.extend(part)
                    if len(content) > MAX_RESPONSE:
                        raise ValueError("Preview health response exceeds limit.")
                if response.status_code == 200 and b"<" in content:
                    return
    raise ValueError("Preview socket is not ready.")


def gateway_app() -> FastAPI:
    """Separate trusted process, never mounted into the production application."""
    origin = preview_origin()
    production = _origin(os.environ["LUIGI_MAINTAINER_UI_URL"])
    _key()
    if any(value and (name in {"GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "OPENAI_API_KEY"}
                     or name.startswith(("LUIGI_MAINTAINER_SMTP_", "SMTP_"))
                     or name.startswith(("LUIGI_WEB_", "LUIGI_MAINTAINER_", "LUIGI_RELEASE_"))
                     and name.endswith(("_TOKEN", "_PASSWORD", "_SECRET", "_API_KEY")))
           for name, value in os.environ.items()):
        raise ValueError("Gateway must run without application, publisher, agent or mail credentials.")
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @application.middleware("http")
    async def boundary(request: Request, call_next):
        try:
            if request.headers.get("host") not in {urlsplit(origin).netloc, f"127.0.0.1:{PORT}"}:
                raise ValueError("Invalid preview host.")
            async with asyncio.timeout(30):
                response = await call_next(request)
        except Exception:
            response = JSONResponse({"detail": "Preview unavailable"}, status_code=403)
        response.headers.update(PRIVATE_HEADERS)
        return response

    @application.get("/_maintainer/status")
    async def status():
        state = _active()
        _binding(state, recorded=False)
        await _health(state)
        if _active() != state:
            raise ValueError("Preview changed during health check.")
        _binding(state, recorded=False)
        return {"ready": True, **{name: state[name] for name in ("run_id", "head_commit", "instance")}}

    @application.post("/session")
    async def session(request: Request):
        if (request.headers.get("origin") != production or request.url.query
                or request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded"):
            raise ValueError("Invalid preview bootstrap origin.")
        data = parse_qs((await _body(request, 4096)).decode("ascii"), strict_parsing=True)
        if set(data) != {"run_id", "ticket"} or any(len(values) != 1 for values in data.values()):
            raise ValueError("Invalid preview bootstrap.")
        token = _redeem(data["ticket"][0], data["run_id"][0])
        response = HTMLResponse('<!doctype html><title>Test application</title>'
                                '<a href="/">Open test application</a>')
        response.set_cookie(COOKIE, token, max_age=3600, secure=True, httponly=True, samesite="strict", path="/")
        response.headers["Clear-Site-Data"] = '"cache", "storage"'
        return response

    @application.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(request: Request, path: str):
        if path == "session" or path.startswith("_maintainer/"):
            raise ValueError("Reserved preview path.")
        claims = _verify(request.cookies.get(COOKIE, ""), "session", 3600)
        state = _active()
        if any(claims[name] != state[name] for name in ("run_id", "head_commit", "instance")):
            raise ValueError("Preview authorization is stale.")
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != origin:
            raise ValueError("Same-origin preview mutations only.")
        return await _proxy(request, state, await _body(request, MAX_BODY))

    return application


@contextmanager
def _controller_lock():
    from importlib import import_module

    try:
        locking = import_module("fcntl")
        no_follow = getattr(os, "O_NOFOLLOW")
    except (ImportError, AttributeError):
        raise ValueError("Preview controller locking requires Linux.") from None
    root = review_worker._directory(_state_root(), shared=True)
    descriptor = os.open(review_worker._plain_path(root / "controller.lock"),
                         os.O_CREAT | os.O_RDWR | no_follow, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Invalid preview controller lock.")
        locking.flock(descriptor, locking.LOCK_EX | locking.LOCK_NB)
        yield root
    finally:
        os.close(descriptor)


def prepare_published_source(config, run: dict):
    """Fetch the fixed published branch into a fresh detached worktree; no host tests."""
    from . import release
    legacy = review_worker.legacy
    branch = "automation/review-" + uuid.UUID(review_worker._identifier(run["id"])).hex
    if run["branch"] != branch:
        raise ValueError("Invalid published review branch.")
    head = review._commit(run["head_commit"])
    legacy._repository_identity(config.repository_url)
    release._git_metadata(config, config.repository_root)
    ref = "refs/luigi-preview/" + uuid.uuid4().hex
    release._git(config, ["fetch", "--no-tags", "--no-recurse-submodules", "--", config.repository_url,
                          f"refs/heads/{branch}:{ref}"], cwd=config.repository_root, auth=True)
    try:
        fetched = release._git(config, ["rev-parse", "--verify", ref + "^{commit}"], cwd=config.repository_root).stdout.strip()
        if fetched != head:
            raise ValueError("Published review head changed.")
        directory = review_worker._directory(config.worktrees_root)
        path = review_worker._plain_path(directory / ("preview-" + uuid.uuid4().hex))
        release._git(config, ["worktree", "add", "--detach", str(path), head], cwd=config.repository_root)
        context = legacy.WorktreeContext(path, branch, head)
        try:
            actual = release._git(config, ["rev-parse", "HEAD"], cwd=path).stdout.strip()
            if actual != head:
                raise ValueError("Published worktree head changed.")
            return context
        except BaseException:
            _remove_worktree(config, context)
            raise
    finally:
        release._git(config, ["update-ref", "-d", ref], cwd=config.repository_root)


def _remove_worktree(config, context) -> None:
    from . import release
    release._git(config, ["worktree", "remove", "--force", str(context.path)], cwd=config.repository_root)


def _command(image: str, source: Path, runtime: Path, name: str, profile: str) -> list[str]:
    if (not re.fullmatch(r"luigi-preview-[0-9a-f]{32}", name) or profile not in sandbox.PREVIEW_KINDS
            or not re.fullmatch(sandbox.IMAGE_PATTERN, image)):
        raise ValueError("Invalid trusted preview command.")
    command = sandbox._command(image, source, runtime, name, None)
    command.insert(command.index("run") + 1, "--detach")
    command[command.index("--timeout=900")] = "--timeout=3600"
    command = [argument.replace("destination=/output,rw", "destination=/run/preview,rw")
               if argument.startswith("type=bind,") else argument for argument in command]
    command[command.index("/opt/luigi-tests/check.py")] = "/opt/luigi-tests/preview.py"
    command.extend(("--profile", profile, "--public-origin", preview_origin()))
    return command


def _podman(arguments: list[str]) -> None:
    result = subprocess.run(arguments, env=sandbox._runtime_env(os.environ), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    if result.returncode:
        raise ValueError("Preview container operation failed.")


def _gateway_ready(state: dict) -> bool:
    try:
        with httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client:
            with client.stream("GET", f"http://127.0.0.1:{PORT}/_maintainer/status") as response:
                content = bytearray()
                for part in response.iter_raw():
                    content.extend(part)
                    if len(content) > 4096:
                        return False
                expected = {"ready": True, **{name: state[name] for name in ("run_id", "head_commit", "instance")}}
                return response.status_code == 200 and json.loads(content) == expected
    except (httpx.HTTPError, ValueError):
        return False


def verify_ready(run: dict, *, gateway_check: bool = True) -> bool:
    """Read-only live lease gate: return True or raise ValueError; no signing key needed.

    Production callers must keep gateway_check enabled. Disabling it checks only
    local metadata/socket continuity and is intended for isolated contract tests.
    """
    try:
        state = _active()
        current = _binding(state)
        fields = ("id", "head_commit", "selected_option_id")
        if run.get("state") not in READY_STATES or any(run.get(field) != current[field] for field in fields):
            raise ValueError("Preview binding changed.")
        before = _socket(state).lstat()
        if gateway_check and not _gateway_ready(state):
            raise ValueError("Preview gateway is unavailable.")
        after = _socket(state).lstat()
        refreshed = _binding(state)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or _active() != state
            or any(refreshed[field] != current[field] for field in fields)):
            raise ValueError("Preview changed during readiness check.")
        return True
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        raise ValueError("Live preview is unavailable or stale.") from None


def _wait_ready(state: dict, *, gateway: bool = True) -> bool:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            _socket(state)
            if gateway and _gateway_ready(state):
                return True
            if not gateway:
                asyncio.run(_health(state))
                return True
        except (OSError, ValueError, httpx.HTTPError):
            pass
        time.sleep(0.1)
    return False


def start_preview(config, run: dict, option: dict) -> dict:
    """Inject into review_worker.preview_once; no gateway key in this worker role."""
    preview_origin()
    if any(os.environ.get(name) for name in ("LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY",
                                           "LUIGI_MAINTAINER_COPILOT_TOKEN", "LUIGI_RELEASE_GITHUB_TOKEN")):
        raise ValueError("Preview controller must run without gateway, agent or release secrets.")
    if sandbox.available().available is not True:
        return {"ready": False}
    current = review.get_run(run["id"])
    if (not current or current["state"] != "testing" or current["head_commit"] != run["head_commit"]
            or review_worker._selected(current) != option):
        raise ValueError("Approved preview binding changed.")
    candidate = review_worker.load_candidate(config, current, option)
    image = sandbox._image(os.environ)
    if image != candidate.metadata["image"]:
        raise ValueError("Preview image differs from approved checks.")
    job = maintainer.get_job(current["job_uuid"])
    profile = sandbox.preview_kind_for_path(job["page_path"] if job else "")
    with _controller_lock() as root:
        if (root / "active.json").exists():
            return {"ready": False}
        with review._transaction() as connection:
            refreshed = review._run(connection, current["id"])
            if refreshed["state"] != "testing" or refreshed["head_commit"] != current["head_commit"]:
                raise ValueError("Approved preview binding changed.")
            review._change(connection, refreshed, actor="worker", action="preview_starting",
                           preview_commit=None, preview_url=None, **review._clear_release())
        instance = str(uuid.uuid4())
        name = "luigi-preview-" + uuid.UUID(instance).hex
        runtime = review_worker._directory(root / "r" / instance, shared=True)
        if len(os.fsencode(runtime / "app.sock")) > 103:
            raise ValueError("Preview state directory is too long for a Unix socket.")
        runtime.chmod(0o2750)
        source_parent = review_worker._directory(root / "sources")
        source = source_parent / instance
        pending = root / (instance + ".json")
        context = None
        started = False
        try:
            context = prepare_published_source(config, current)
            snapshot = sandbox.export_candidate(context.path, source)
            if snapshot.source_digest != option["tree_sha256"]:
                raise ValueError("Published source differs from approved candidate.")
            _remove_worktree(config, context)
            context = None
            state = {"run_id": current["id"], "head_commit": current["head_commit"], "instance": instance,
                     "container_name": name, "source_digest": snapshot.source_digest, "expires": int(time.time()) + 3600}
            started = True
            _podman(_command(image, source, runtime, name, profile))
            if not _wait_ready(state, gateway=False):
                raise ValueError("Preview socket is not ready.")
            _binding(state, recorded=False)
            review_worker._write(pending, review_worker._json(state), shared=True)
            os.replace(pending, root / "active.json")
            if not _wait_ready(state) or sandbox._snapshot_digest(source) != snapshot.source_digest:
                raise ValueError("Live preview readiness could not be verified.")
            _binding(state, recorded=False)
            return {"ready": True, "head_commit": current["head_commit"], "preview_url": review.preview_path(current["id"])}
        except BaseException:
            if started:
                try:
                    _podman([sandbox.RUNTIME, "--remote=false", "rm", "--force", "--ignore", name])
                except Exception:
                    if not (root / "active.json").exists():
                        review_worker._write(root / "active.json", review_worker._json(state), shared=True)
                    raise ValueError("Preview cleanup requires protected controller attention.") from None
            (root / "active.json").unlink(missing_ok=True)
            shutil.rmtree(source, ignore_errors=True)
            shutil.rmtree(runtime, ignore_errors=True)
            raise
        finally:
            pending.unlink(missing_ok=True)
            if context is not None:
                _remove_worktree(config, context)


def stop_preview() -> None:
    """Protected controller command only; revoke evidence before stopping the fixed container."""
    with _controller_lock() as root:
        if not (root / "active.json").exists():
            return
        state = _active(allow_expired=True)
        with review._transaction() as connection:
            run = review._run(connection, state["run_id"])
            fields = {"preview_commit": None, "preview_url": None, **review._clear_release()}
            if run["state"] in {"release_queued", "releasing"}:
                fields["state"] = "needs_attention"
            review._change(connection, run, actor="worker", action="preview_stopped", **fields)
        _podman([sandbox.RUNTIME, "--remote=false", "rm", "--force", "--ignore", state["container_name"]])
        (root / "active.json").unlink()
        shutil.rmtree(root / "sources" / state["instance"], ignore_errors=True)
        shutil.rmtree(root / "r" / state["instance"], ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Trusted standalone preview gateway/controller")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--serve", action="store_true")
    commands.add_argument("--stop", action="store_true")
    parser.add_argument("--port", type=int, choices=[PORT], default=PORT)
    arguments = parser.parse_args(argv)
    if arguments.stop:
        stop_preview()
    else:
        import uvicorn
        uvicorn.run(gateway_app(), host="127.0.0.1", port=arguments.port, workers=1,
                    access_log=False, log_config=None, log_level="critical", proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
