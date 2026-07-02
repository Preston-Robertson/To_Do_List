"""Read-only DB smoke test — run BEFORE building/pushing write paths.

Uses the exact env vars documented in the spec. Refuses to proceed if
schema_version isn't 2 or if the uuid column is missing/null.

Usage (PowerShell):
    $env:LUIGI_WEB_PG_HOST="10.0.0.202"
    $env:LUIGI_WEB_PG_PORT="5432"
    $env:LUIGI_WEB_PG_DB="luigi_todo"
    $env:LUIGI_WEB_PG_USER="luigi_web"
    $env:LUIGI_WEB_PG_PASSWORD="<paste-only-here-do-not-commit>"
    python scripts\smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root without installing the app as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

import db  # noqa: E402


def main() -> int:
    print("→ connecting…")
    engine = db.get_engine()

    with engine.connect() as conn:
        # 1. schema_version must be 2
        v = conn.execute(text("SELECT version FROM schema_version")).scalar_one()
        print(f"  schema_version = {v}")
        if int(v) < 2:
            print("  ✖ schema_version < 2 — LuigiBot v2 migration not deployed. STOP.")
            return 1

        # 2. tasks table sanity
        n = conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one()
        print(f"  tasks row count = {n}")

        # 3. every task must have a non-null uuid
        n_null_uuid = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE uuid IS NULL")
        ).scalar_one()
        print(f"  tasks with NULL uuid = {n_null_uuid}")
        if n_null_uuid:
            print("  ✖ uuid column has NULLs. STOP.")
            return 1

        # 4. sample rows to prove read shape
        print("  sample:")
        for row in conn.execute(
            text("SELECT uuid, task, status FROM tasks ORDER BY task LIMIT 5")
        ):
            print(f"    - {row.uuid[:8]}…  [{row.status:12s}]  {row.task}")

        # 5. the other three list tables must exist and be readable
        for tbl in ("recurring_tasks", "discipline_list",
                    "discipline_completions", "follow_up_tasks"):
            n = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar_one()
            print(f"  {tbl}: {n} rows")

    print("✓ smoke test passed — safe to build write paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
