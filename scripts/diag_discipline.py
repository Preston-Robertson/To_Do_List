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
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)
        return str(p)
    return None


def main() -> int:
    used = _load_env_file()
    print(f"→ env file: {used or '(none found — relying on current environment)'}")

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
        print("  ✖ LUIGI_WEB_PG_PASSWORD is not set. Run as a user that can read\n"
              "    /etc/luigi-web.env (e.g. `sudo -u luigi-web ...`), or export it first.")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
