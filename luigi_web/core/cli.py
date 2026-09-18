"""Console entry point with application-free argument parsing."""
from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Luigi Web host.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    arguments = parser.parse_args(argv)

    import uvicorn

    uvicorn.run("luigi_web.application:app", host=arguments.host, port=arguments.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())