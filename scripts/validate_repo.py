#!/usr/bin/env python3
"""Offline template and route validation for development and PR CI."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from luigi_web import application  # noqa: E402
from luigi_web.core.templating import builtin_resource_directories  # noqa: E402


def main() -> int:
    template_names = sorted(
        name for name in application.templates.env.list_templates()
        if name.endswith(".html")
    )
    expected_templates = {
        template.relative_to(directory).as_posix()
        for directory in builtin_resource_directories("templates")
        for template in directory.rglob("*.html")
    }
    missing_templates = expected_templates.difference(template_names)
    if missing_templates:
        for name in sorted(missing_templates):
            print(f"Missing module template: {name}", file=sys.stderr)
        return 1
    for name in template_names:
        application.templates.get_template(name)

    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for route in application.app.routes:
        path = str(getattr(route, "path", "") or "")
        for method in sorted(getattr(route, "methods", None) or []):
            key = (method, path)
            if key in seen:
                duplicates.append(key)
            seen.add(key)

    if duplicates:
        for method, path in duplicates:
            print(f"Duplicate route: {method} {path}", file=sys.stderr)
        return 1

    print(
        f"Validated {len(template_names)} templates and "
        f"{len(seen)} unique method/path registrations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
