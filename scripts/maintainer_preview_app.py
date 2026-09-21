"""Audited image entrypoint. Candidate imports occur only inside the sandbox."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import importlib
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

PROFILES = ("workspace", "media", "cards", "finance")


class LoopbackScope:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 58120), "scheme": "http"}
            scope["headers"] = [(name, value) for name, value in scope["headers"]
                                if name.lower() not in {b"host", b"origin", b"forwarded", b"x-forwarded-host", b"x-forwarded-proto"}]
            scope["headers"].append((b"host", b"127.0.0.1:58120"))
            scope["headers"].append((b"origin", b"http://127.0.0.1:58120"))
        await self.app(scope, receive, send)


@contextmanager
def fixture_app(profile, public_origin):
    if profile not in PROFILES:
        raise ValueError("Unknown synthetic preview profile.")
    module = importlib.import_module("scripts.preview_" + profile)
    with ExitStack() as stack:
        if profile == "media":
            origin = SimpleNamespace(get=lambda: public_origin, set=lambda value: None, reset=lambda token: None)
            stack.enter_context(patch.object(module, "_ORIGIN", origin))
        fixture = stack.enter_context(module.preview_context())
        if profile == "cards":
            module.seed_cards()
            fixture = module.preview_app()
        yield LoopbackScope(fixture)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--public-origin", required=True)
    arguments = parser.parse_args(argv)
    if sys.platform != "linux" or Path.cwd() != Path("/workspace"):
        raise RuntimeError("Preview runner requires the isolated Linux image.")
    os.umask(0o007)
    sys.path.insert(0, "/workspace")
    import uvicorn
    with fixture_app(arguments.profile, arguments.public_origin) as application, socket.socket(socket.AF_UNIX) as listener:
        listener.bind("/run/preview/app.sock")
        os.chmod("/run/preview/app.sock", 0o660)
        listener.listen(64)
        server = uvicorn.Server(uvicorn.Config(application, lifespan="off", access_log=False,
                                              log_config=None, log_level="critical", proxy_headers=False,
                                              ws="none", limit_concurrency=16, timeout_keep_alive=5))
        server.run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
