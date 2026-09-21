"""Trusted image entry point, NOT a host-side test command.

Install this reviewed file at /opt/luigi-tests/check.py in the immutable image.
Invoke only through feedback.sandbox with python -I. It never imports workspace
code into its own interpreter; fixed child commands run inside the same secret-
free container. Candidate code can forge evidence, kill this runner or alter its
children. Passing is advisory, not proof, and requires separate baseline CI and
human approval. Raw process output never leaves the container.

The optional preview is a disposable synthetic server and Chromium client in
the container's network namespace. No port is published and no live application
or production records are used. Screenshots are local human-review only.
"""
from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
from importlib import import_module
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

WORKSPACE = Path("/workspace")
OUTPUT = Path("/output")
PYTHON = "/usr/local/bin/python"
PREVIEW_TARGETS = {
    "workspace": ("scripts/preview_workspace.py", "/home"),
    "media": ("scripts/preview_media.py", "/games"),
    "cards": ("scripts/preview_cards.py", "/cards/mtg/decks"),
    "finance": ("scripts/preview_finance.py", "/finance"),
}
TEST_COMMAND = (PYTHON, "-B", "-m", "unittest", "discover", "-s", "tests", "-v")
VALIDATE_COMMAND = (PYTHON, "-B", "scripts/validate_repo.py")
MAX_OUTPUT = 2 * 1024 * 1024
TAIL_BYTES = 16 * 1024
TEXT_SUFFIXES = frozenset({".py", ".html", ".js", ".css", ".json", ".toml", ".txt", ".sh", ".yaml", ".yml"})


def _inside_sandbox() -> bool:
    return (
        sys.platform == "linux" and os.geteuid() == 65534
        and Path(__file__) == Path("/opt/luigi-tests/check.py")
        and Path("/run/.containerenv").is_file()
        and WORKSPACE.is_dir() and OUTPUT.is_dir()
    )


def _kill_group(process: subprocess.Popen) -> None:
    try:
        getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
    except ProcessLookupError:
        pass


def _stop(process: subprocess.Popen) -> None:
    _kill_group(process)
    process.wait(timeout=5)


def _run(command: tuple[str, ...], timeout: int) -> tuple[int, str]:
    tail = bytearray()
    process = subprocess.Popen(
        command, cwd=WORKSPACE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )

    received = 0
    deadline = time.monotonic() + timeout
    code = 124
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as monitor:
            monitor.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not monitor.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    break
                received += len(chunk)
                tail.extend(chunk)
                del tail[:-TAIL_BYTES]
                if received > MAX_OUTPUT:
                    code = 125
                    break
    except subprocess.TimeoutExpired:
        code = 124
    finally:
        _stop(process)
        if process.stdout is not None:
            process.stdout.close()
    return code, tail.decode("utf-8", errors="replace")


def _test_result(code: int, output: str) -> dict:
    summary = re.search(r"\nRan ([0-9]+) tests? in [0-9.]+s\s+OK(?: \(skipped=([0-9]+)\))?\s*\Z", "\n" + output)
    count = int(summary[1]) if summary else None
    if code == 0 and (not count or summary is None or int(summary[2] or 0)):
        code = 1
    return {"name": "tests", "exit_code": code, "count": count}


def _validation_results(code: int, output: str) -> list[dict]:
    summary = re.search(r"Validated ([0-9]+) templates and ([0-9]+) unique method/path registrations\.\s*\Z", output)
    if code == 0 and (summary is None or not int(summary[1]) or not int(summary[2])):
        code = 1
    return [
        {"name": name, "exit_code": code, "count": int(summary[index]) if summary else None}
        for index, name in ((1, "templates"), (2, "routes"))
    ]


def _whitespace(root: Path) -> dict:
    """Full-source whitespace check; independent CI still owns git diff --check."""
    failures = 0
    for path in root.rglob("*"):
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            failures += 1
            continue
        for line in text.splitlines():
            if line.rstrip(" \t") != line or re.match(r"^(?:<{7}|={7}|>{7})(?: |$)", line):
                failures += 1
    return {"name": "whitespace", "exit_code": int(failures > 0), "count": failures}


def _preview(kind: str) -> dict:
    Image = import_module("PIL.Image")
    from playwright.sync_api import sync_playwright

    script, route = PREVIEW_TARGETS[kind]
    process = subprocess.Popen(
        (PYTHON, "-B", script, "--port", "8765"), cwd=WORKSPACE,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    count = 0
    try:
        origin = "http://127.0.0.1:8765"
        deadline = time.monotonic() + 30
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(CookieJar()),
        )
        while True:
            try:
                with opener.open(origin + "/", timeout=1) as bootstrap:
                    ready = bootstrap.status == 200
                if ready:
                    with opener.open(origin + route, timeout=1) as response:
                        ready = response.status == 200
                    if ready:
                        break
            except (OSError, urllib.error.URLError):
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                return {"name": "screenshots", "exit_code": 1, "count": count}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for filename, width, height in (("desktop.png", 1440, 900), ("mobile.png", 390, 844)):
                    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, service_workers="block")
                    try:
                        context.route("**/*", lambda request_route: request_route.continue_() if request_route.request.url.startswith(origin + "/") else request_route.abort())
                        page = context.new_page()
                        response = page.goto(origin + "/", wait_until="networkidle", timeout=30_000)
                        if response is None or response.status != 200:
                            raise ValueError("Synthetic preview bootstrap failed")
                        response = page.goto(origin + route, wait_until="networkidle", timeout=30_000)
                        if response is None or response.status != 200:
                            raise ValueError("Synthetic preview failed")
                        page.evaluate("document.fonts.ready")
                        layout = page.evaluate("({width: innerWidth, scroll: document.documentElement.scrollWidth, text: document.body.innerText.length})")
                        if layout["scroll"] > layout["width"] or not layout["text"]:
                            raise ValueError("Synthetic preview layout failed")
                        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
                            capture = Path(temporary) / "capture.png"
                            page.screenshot(path=str(capture), full_page=False, animations="disabled")
                            with Image.open(capture) as image:
                                converted = image.convert("RGB")
                                if converted.size != (width, height) or all(low == high for low, high in converted.getextrema()):
                                    raise ValueError("Synthetic preview capture is blank")
                                converted.save(OUTPUT / filename, format="PNG")
                        count += 1
                    finally:
                        context.close()
            finally:
                browser.close()
        return {"name": "screenshots", "exit_code": 0, "count": count}
    finally:
        _stop(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-target", choices=tuple(PREVIEW_TARGETS))
    arguments = parser.parse_args()
    if not _inside_sandbox():
        print("This runner requires the fixed rootless sandbox image.", file=sys.stderr)
        return 2
    for directory in ("/tmp/home", "/tmp/luigi", "/tmp/cache", "/tmp/config", "/tmp/data"):
        Path(directory).mkdir(parents=True, exist_ok=True)
    checks = []
    try:
        checks.append(_test_result(*_run(TEST_COMMAND, 540)))
        checks.extend(_validation_results(*_run(VALIDATE_COMMAND, 90)))
        checks.append(_whitespace(WORKSPACE))
        if arguments.preview_target:
            try:
                checks.append(_preview(arguments.preview_target))
            except Exception:
                checks.append({"name": "screenshots", "exit_code": 1, "count": 0})
        report = json.dumps({"version": 1, "checks": checks}, separators=(",", ":"))
        (OUTPUT / "report.json").write_text(report, encoding="ascii")
    except Exception:
        print("Sandbox checks did not complete.", file=sys.stderr)
        return 1
    return int(any(check["exit_code"] != 0 for check in checks))


if __name__ == "__main__":
    raise SystemExit(main())
