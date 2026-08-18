"""Compatibility entry point for existing ``uvicorn app:app`` deployments."""
from __future__ import annotations

import os

from luigi_web.application import app

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("LUIGI_WEB_BIND", "0.0.0.0"),
        port=int(os.environ.get("LUIGI_WEB_PORT", "8080")),
        reload=False,
    )
