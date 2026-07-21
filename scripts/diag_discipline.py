"""Diagnose why marking a discipline 'done' fails.

Read-only: introspects the live ``discipline_completions`` schema and then
*attempts* the exact INSERT ``mark_completion`` runs, but inside a transaction
that is ALWAYS rolled back — so it writes nothing, it only surfaces the real
Postgres error (column mismatch, NOT NULL, missing constraint, etc.).

It self-loads credentials from the same env file the systemd service uses, so
on the LXC you can just run:

    python3 scripts/diag_discipline.py

Resolution order for the env file: ``$LUIGI_WEB_ENV_FILE`` →
``/etc/luigi-web.env`` → ``<repo>/.env``. Values already in the environment
win, so this never clobbers a live config.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_env_file() -> str | None:
    candidates = [
        os.environ.get("LUIGI_WEB_ENV_FILE"),
        "/etc/luigi-web.env",
        str(REPO / ".env"),
    ]
    for path in candidates:
        if not path:
            continue
        p = Path(path)
        try:
            text_content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text_content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)
        return str(p)
    return None


def _load_from_service_environ() -> str | None:
    """Pull LUIGI_WEB_* vars from the running luigi-web service's process
    environment (root-only: reads /proc/<pid>/environ). Lets this run with no
    env setup at all, using the exact config the live app connects with."""
    try:
        pid = subprocess.check_output(
            ["systemctl", "show", "-p", "MainPID", "--value", "luigi-web"],
            text=True,
        ).strip()
    except Exception:
        return None
    if not pid or pid == "0":
        return None
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for chunk in raw.split(b"\0"):
        if not chunk or b"=" not in chunk:
            continue
        key, _, val = chunk.partition(b"=")
        k = key.decode("utf-8", "replace")
        if k.startswith("LUIGI_WEB_"):
            os.environ.setdefault(k, val.decode("utf-8", "replace"))
    return pid


def main() -> int:
    used = _load_env_file()
    print(f"→ env file: {used or '(none found)'}")

    # The non-secret PG vars are supplied to the systemd service via its
    # ``Environment=`` lines (see luigi-web.service), not necessarily the env
    # file. Mirror those known defaults so this runs standalone; only the
    # password/token genuinely have to come from /etc/luigi-web.env.
    for key, default in (
        ("LUIGI_WEB_PG_HOST", "10.0.0.202"),
        ("LUIGI_WEB_PG_PORT", "5432"),
        ("LUIGI_WEB_PG_DB", "luigi_todo"),
        ("LUIGI_WEB_PG_USER", "luigi_web"),
    ):
        os.environ.setdefault(key, default)

    if not os.environ.get("LUIGI_WEB_PG_PASSWORD"):
        pid = _load_from_service_environ()
        if pid:
            print(f"→ pulled credentials from running luigi-web service (pid {pid})")

    if not os.environ.get("LUIGI_WEB_PG_PASSWORD"):
        print("  ✖ Could not find LUIGI_WEB_PG_PASSWORD.\n"
              "    Run as root so I can read it from the running service:\n"
              "      sudo /opt/luigi-web/.venv/bin/python "
              "/opt/luigi-web/scripts/diag_discipline.py")
        return 1

    from sqlalchemy import text  # noqa: E402
    import db  # noqa: E402

    engine = db.get_engine()

    print("\n=== discipline_completions columns ===")
    with engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'discipline_completions'
            ORDER BY ordinal_position
        """)).fetchall()
        if not cols:
            print("  ✖ table 'discipline_completions' not found (or no columns).")
        for c in cols:
            print(f"  - {c.column_name:20s} {c.data_type:16s} "
                  f"null={c.is_nullable:3s} default={c.column_default}")

        print("\n=== constraints / unique indexes ===")
        cons = conn.execute(text("""
            SELECT conname, pg_get_constraintdef(oid) AS condef
            FROM pg_constraint
            WHERE conrelid = 'discipline_completions'::regclass
            ORDER BY conname
        """)).fetchall()
        for c in cons:
            print(f"  - {c.conname}: {c.condef}")
        idx = conn.execute(text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'discipline_completions'
        """)).fetchall()
        for i in idx:
            print(f"  - idx {i.indexname}: {i.indexdef}")

        print("\n=== sample existing rows (up to 3) ===")
        try:
            rows = conn.execute(text(
                "SELECT * FROM discipline_completions LIMIT 3"
            )).fetchall()
            for r in rows:
                print(f"  {dict(r._mapping)}")
            if not rows:
                print("  (table is empty)")
        except Exception as exc:
            print(f"  ✖ select failed: {exc}")

        print("\n=== first active discipline ===")
        disc = conn.execute(text("""
            SELECT task, catagory FROM discipline_list
            WHERE active = 1 ORDER BY task LIMIT 1
        """)).first()
        if disc is None:
            print("  ✖ no active disciplines to test with.")
            return 1
        print(f"  task={disc.task!r} catagory={disc.catagory!r}")

    today = db.today_iso()
    print(f"\n=== dry-run INSERT (rolled back) for {today} ===")
    ins = text("""
        INSERT INTO discipline_completions (task, catagory, completed_date, logged_at)
        SELECT :t, :c, :d, :ts
        WHERE NOT EXISTS (
            SELECT 1 FROM discipline_completions
            WHERE task = :t AND completed_date = :d
        )
    """)
    params = {"t": disc.task, "c": disc.catagory, "d": today, "ts": db.now_iso()}
    conn = engine.connect()
    trans = conn.begin()
    try:
        result = conn.execute(ins, params)
        print(f"  ✓ INSERT succeeded (rowcount={result.rowcount}) — the current "
              f"mark_completion SQL is valid against this schema.")
    except Exception:
        print("  ✖ INSERT raised — THIS is why 'Done' fails:\n")
        traceback.print_exc()
    finally:
        trans.rollback()
        conn.close()
    print("\n(dry run rolled back — no data was written.)")

    _http_self_test(disc.task, disc.catagory, today)
    return 0


def _http_self_test(task: str, catagory: str | None, day: str) -> None:
    """Hit the LIVE running endpoint the way the browser does, so we exercise
    the FastAPI route + Jinja template (not just the DB). This is the part the
    DB dry-run can't see. NOTE: on success this really marks ``day`` done for
    ``task`` — which is the intended action anyway."""
    import urllib.error
    import urllib.parse
    import urllib.request

    port = os.environ.get("LUIGI_WEB_PORT", "8080")
    token = os.environ.get("LUIGI_WEB_UI_TOKEN", "")
    print(f"\n=== live endpoint POST /discipline/toggle (127.0.0.1:{port}) ===")
    if not token:
        print("  ✖ no LUIGI_WEB_UI_TOKEN in env; skipping live test.")
        return
    url = f"http://127.0.0.1:{port}/discipline/toggle?token={urllib.parse.quote(token)}"
    data = urllib.parse.urlencode({
        "task": task, "catagory": catagory or "", "day": day, "action": "mark",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    status = None
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
            print(f"  ✓ status={status}")
            print("  body (first 800 chars):")
            print("    " + body[:800].replace("\n", "\n    "))
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8", "replace")
        print(f"  ✖ HTTP {e.code}")
        print("  body (first 800 chars):")
        print("    " + body[:800].replace("\n", "\n    "))
    except Exception as e:  # noqa: BLE001
        print(f"  ✖ request failed: {e}")

    if status != 200:
        print("\n=== last luigi-web log lines (server-side traceback, if any) ===")
        try:
            out = subprocess.check_output(
                ["journalctl", "-u", "luigi-web", "-n", "40", "--no-pager"],
                text=True, stderr=subprocess.STDOUT,
            )
            print("    " + out.replace("\n", "\n    "))
        except Exception as e:  # noqa: BLE001
            print(f"  (couldn't read journal: {e} — run as root to see it)")


if __name__ == "__main__":
    sys.exit(main())
