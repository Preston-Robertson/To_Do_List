"""Allow-listed tools the chat agent can call.

**Security contract**

* The LLM sees only the tools registered in ``build_registry()``. Every one is
  a small Python function that wraps calls into ``db.py``. There is no shell,
  no ``eval``/``exec``, no filesystem access, no dynamic code loading, no way
  for the model to reach anything not in this file.
* Arguments arrive as a JSON dict decoded from the model's response and are
  passed through explicit key lookups + type coercion. Anything unexpected
  raises and gets sent back to the model as an error message on the next
  turn — Python code paths are not taken with bad input.
* Every tool returns a JSON-serializable value. We never return internal
  objects, exceptions, or SQLAlchemy rows.

Adding a new tool: define ``handle_X``, add a ``Tool(...)`` entry in
``build_registry()``. That's it — the loop in ``llm.py`` picks it up.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import db
from llm import Tool


# --------------------------------------------------------------------------- #
# Small helpers for pulling typed values out of the JSON args dict
# --------------------------------------------------------------------------- #

def _str(args: dict[str, Any], key: str, *, required: bool = False, default: str | None = None) -> str | None:
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f"missing required argument: {key}")
        return default
    return str(v).strip()


def _int(args: dict[str, Any], key: str, default: int | None = None) -> int | None:
    v = args.get(key)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"argument {key!r} must be an integer, got {v!r}")


def _bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
    v = args.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y")
    return default


def _iso_date(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    v = args.get(key)
    if v is None or v == "":
        return default
    s = str(v).strip()
    # Accept "today"/"tomorrow" shortcuts — the LLM often uses them.
    today = date.today()
    if s.lower() == "today":
        return today.isoformat()
    if s.lower() == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    # Reject anything that isn't a YYYY-MM-DD prefix — no free-form text into
    # the DB, no injection surface.
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise ValueError(f"argument {key!r} must be YYYY-MM-DD or 'today'/'tomorrow', got {v!r}")


def _resolve_task_uuid(args: dict[str, Any]) -> tuple[str, str]:
    """Return (uuid, table_hint) for a task referenced by uuid or by name.

    ``table_hint`` is ``"tasks"`` or ``"recurring_tasks"`` so the caller can
    dispatch to the right helper. If a bare uuid is supplied we default to
    ``"tasks"`` and let the caller retry against ``recurring_tasks``.
    """
    row_uuid = _str(args, "uuid")
    if row_uuid:
        # Trust an explicit uuid: caller (LLM) got it from a prior search.
        # We still need to know which table — probe both.
        if db.get_task(row_uuid):
            return row_uuid, "tasks"
        if db.get_recurring(row_uuid):
            return row_uuid, "recurring_tasks"
        raise ValueError(f"no task or recurring row with uuid {row_uuid}")

    name = _str(args, "task_name", required=True)
    matches = db.find_tasks_by_name(name, include_completed=False, limit=5)
    if not matches:
        raise ValueError(f"no open task matches {name!r} — search first with search_tasks")
    if len(matches) > 1:
        preview = ", ".join(f"{m['task']!r}" for m in matches[:3])
        raise ValueError(
            f"multiple tasks match {name!r} ({len(matches)}): {preview}. "
            "Call search_tasks and pass the exact uuid."
        )
    m = matches[0]
    return m["uuid"], "tasks" if m["source"] == "task" else "recurring_tasks"


# --------------------------------------------------------------------------- #
# Normalization: title-case new names, reuse existing category spellings
# --------------------------------------------------------------------------- #

# Chicago-style small words that stay lowercase mid-title. First and last
# word are always capitalized regardless.
_TITLE_LOWERCASE: frozenset[str] = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "en", "for", "from",
    "if", "in", "into", "nor", "of", "on", "onto", "or", "over",
    "per", "so", "the", "to", "up", "upon", "via", "vs", "with", "yet",
})


def _title_case(s: str | None) -> str | None:
    """Book-title capitalization for user-facing names.

    * All-caps runs of 2+ letters are preserved (acronyms like ``TPS``).
    * Small function words (``the``, ``of``, ``and`` …) stay lowercase
      unless they're the first or last word.
    * Hyphenated compounds get each segment capitalized.
    """
    if not s:
        return s
    s = s.strip()
    if not s:
        return s
    words = s.split()
    n = len(words)
    out: list[str] = []
    for i, word in enumerate(words):
        parts = word.split("-")
        m = len(parts)
        titled: list[str] = []
        for j, part in enumerate(parts):
            if not part:
                titled.append(part)
                continue
            # Preserve intentional acronyms like "AWS", "PR", "TPS".
            if len(part) >= 2 and part.isupper():
                titled.append(part)
                continue
            lower = part.lower()
            # Capitalize the very first and very last atom of the whole
            # title (word 0 segment 0, and last word's last segment).
            # Everything else follows the small-word rule.
            at_title_start = (i == 0 and j == 0)
            at_title_end = (i == n - 1 and j == m - 1)
            if lower in _TITLE_LOWERCASE and not at_title_start and not at_title_end:
                titled.append(lower)
            else:
                titled.append(lower[:1].upper() + lower[1:])
        out.append("-".join(titled))
    return " ".join(out)


def _canonical_categorical(field: str, value: str | None) -> str | None:
    """Prefer the DB's existing spelling for a category / group / sub_group;
    fall back to title-casing a fresh value so newly-invented labels look
    consistent with the rest of the tracker."""
    if not value:
        return value
    existing = db.find_existing_categorical(field, value)
    return existing if existing else _title_case(value)


# --------------------------------------------------------------------------- #
# Tool handlers
# --------------------------------------------------------------------------- #

def handle_list_open_tasks(args: dict[str, Any]) -> dict[str, Any]:
    limit = _int(args, "limit", 15) or 15
    rows = db.list_open_tasks(limit=min(max(limit, 1), 50))
    return {"count": len(rows), "tasks": _slim_tasks(rows)}


def handle_list_overdue(args: dict[str, Any]) -> dict[str, Any]:
    limit = _int(args, "limit", 10) or 10
    rows = db.list_overdue_tasks(limit=min(max(limit, 1), 50))
    return {"count": len(rows), "tasks": _slim_tasks(rows)}


def handle_list_upcoming(args: dict[str, Any]) -> dict[str, Any]:
    days = _int(args, "days", 7) or 7
    limit = _int(args, "limit", 10) or 10
    rows = db.list_upcoming_tasks(days=min(max(days, 1), 60), limit=min(max(limit, 1), 50))
    return {"count": len(rows), "tasks": _slim_tasks(rows)}


def handle_search_tasks(args: dict[str, Any]) -> dict[str, Any]:
    query = _str(args, "query", required=True)
    include_completed = _bool(args, "include_completed", default=False)
    rows = db.find_tasks_by_name(query, include_completed=include_completed, limit=15)
    return {"count": len(rows), "tasks": _slim_tasks(rows)}


def handle_suggest_task_fields(args: dict[str, Any]) -> dict[str, Any]:
    task_name = _str(args, "task", required=True)
    suggestions = db.suggest_task_defaults(task_name)
    return {"task": task_name, "suggestions": suggestions}


def handle_create_task(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "task": _title_case(_str(args, "task", required=True)),
        "priority": _int(args, "priority", 0) or 0,
        "status": _str(args, "status", default="Not Started"),
        "due_date": _iso_date(args, "due_date"),
        "catagory": _canonical_categorical("catagory", _str(args, "catagory")),
        "task_group": _canonical_categorical("task_group", _str(args, "task_group")),
        "sub_group": _canonical_categorical("sub_group", _str(args, "sub_group")),
        "relevant_link": _str(args, "relevant_link"),
        "estimated_time": args.get("estimated_time"),
    }
    if payload["status"] not in db.STATUS_VALUES:
        raise ValueError(f"status must be one of {list(db.STATUS_VALUES)}")
    row_uuid = db.create_task(payload)
    return {"uuid": row_uuid, "task": payload["task"], "status": payload["status"]}


def handle_complete_task(args: dict[str, Any]) -> dict[str, Any]:
    row_uuid, table = _resolve_task_uuid(args)
    if table == "tasks":
        db.set_task_status(row_uuid, "Completed")
    else:
        db.set_recurring_status(row_uuid, "Completed")
    return {"uuid": row_uuid, "table": table, "status": "Completed"}


def handle_update_task_status(args: dict[str, Any]) -> dict[str, Any]:
    status = _str(args, "status", required=True)
    if status not in db.STATUS_VALUES:
        raise ValueError(f"status must be one of {list(db.STATUS_VALUES)}")
    row_uuid, table = _resolve_task_uuid(args)
    if table == "tasks":
        db.set_task_status(row_uuid, status)
    else:
        db.set_recurring_status(row_uuid, status)
    return {"uuid": row_uuid, "table": table, "status": status}


def handle_delete_task(args: dict[str, Any]) -> dict[str, Any]:
    row_uuid, table = _resolve_task_uuid(args)
    if table == "tasks":
        db.delete_task(row_uuid)
    else:
        db.delete_recurring(row_uuid)
    return {"uuid": row_uuid, "table": table, "deleted": True}


def handle_list_disciplines_pending(args: dict[str, Any]) -> dict[str, Any]:
    rows = db.list_disciplines_pending_today()
    return {"count": len(rows), "disciplines": [
        {"uuid": r["uuid"], "task": r["task"], "catagory": r["catagory"],
         "frequency_per_week": r["frequency_per_week"]}
        for r in rows
    ]}


def handle_plan_my_day(args: dict[str, Any]) -> dict[str, Any]:
    """One-shot 'what should I do today?' snapshot.

    Aggregates overdue + due-today tasks, disciplines pending today, and any
    at-risk streaks, then produces a single ranked focus list. Ordering:
    at-risk disciplines first (streaks are perishable), then overdue tasks
    by priority DESC + days_overdue DESC, then due-today tasks by priority
    DESC, then remaining pending disciplines.
    """
    limit = _int(args, "limit", 10) or 10
    limit = min(max(limit, 1), 25)
    today = date.today().isoformat()

    overdue = db.list_overdue_tasks(limit=50)
    upcoming = db.list_upcoming_tasks(days=1, limit=50)  # includes today
    due_today = [r for r in upcoming if (r.get("due_date") or "")[:10] == today]
    pending = db.list_disciplines_pending_today()
    at_risk = db.list_disciplines_at_risk()
    at_risk_names = {r["task"] for r in at_risk}

    focus: list[dict[str, Any]] = []
    for r in at_risk:
        focus.append({
            "type": "discipline_at_risk",
            "task": r["task"],
            "catagory": r.get("catagory"),
            "current_streak": r.get("current_streak"),
            "days_since": r.get("days_since"),
            "reason": f"Streak of {r.get('current_streak')} at risk — last done {r.get('days_since')}d ago",
        })
    for r in sorted(
        overdue,
        key=lambda x: (-(int(x.get("priority") or 0)),
                       (x.get("due_date") or "9999-12-31")),
    ):
        focus.append({
            "type": "overdue",
            "uuid": r["uuid"],
            "task": r["task"],
            "priority": r.get("priority"),
            "due_date": r.get("due_date"),
            "catagory": r.get("catagory"),
            "source": r.get("source"),
            "reason": f"Overdue since {r.get('due_date')}",
        })
    for r in sorted(due_today, key=lambda x: -(int(x.get("priority") or 0))):
        focus.append({
            "type": "due_today",
            "uuid": r["uuid"],
            "task": r["task"],
            "priority": r.get("priority"),
            "due_date": r.get("due_date"),
            "catagory": r.get("catagory"),
            "source": r.get("source"),
            "reason": "Due today",
        })
    for r in pending:
        if r["task"] in at_risk_names:
            continue  # already surfaced above
        focus.append({
            "type": "discipline_pending",
            "task": r["task"],
            "catagory": r.get("catagory"),
            "frequency_per_week": r.get("frequency_per_week"),
            "reason": "Discipline not yet marked today",
        })

    return {
        "today": today,
        "counts": {
            "at_risk": len(at_risk),
            "overdue": len(overdue),
            "due_today": len(due_today),
            "disciplines_pending": len(pending),
        },
        "focus": focus[:limit],
        "focus_truncated": len(focus) > limit,
    }


def handle_mark_discipline_done(args: dict[str, Any]) -> dict[str, Any]:
    name = _str(args, "task", required=True)
    day = _iso_date(args, "day", default=date.today().isoformat())
    row = db.find_discipline_by_name(name)
    if row is None:
        raise ValueError(
            f"no unique active discipline matches {name!r} — "
            "call list_disciplines_pending to see exact names."
        )
    db.mark_completion(task=row["task"], catagory=row["catagory"], day=day)
    return {"task": row["task"], "day": day, "marked": True}


def handle_add_discipline(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "task": _title_case(_str(args, "task", required=True)),
        "catagory": _canonical_categorical("catagory", _str(args, "catagory")),
        "frequency_per_week": _int(args, "frequency_per_week", 0) or 0,
        "active": 1,
    }
    row_uuid = db.create_discipline(payload)
    return {"uuid": row_uuid, "task": payload["task"]}


def handle_create_follow_up(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "trigger_task": _title_case(_str(args, "trigger_task", required=True)),
        "follow_up_task": _title_case(_str(args, "follow_up_task", required=True)),
        "catagory": _canonical_categorical("catagory", _str(args, "catagory")),
        "task_group": _canonical_categorical("task_group", _str(args, "task_group")),
        "subgroup": _title_case(_str(args, "subgroup")),
        "relevant_link": _str(args, "relevant_link"),
        "priority": _int(args, "priority", 0) or 0,
        "estimated_time": args.get("estimated_time"),
        "due_offset_days": _int(args, "due_offset_days"),
    }
    row_uuid = db.create_follow_up(payload)
    return {"uuid": row_uuid, "follow_up_task": payload["follow_up_task"]}


# --------------------------------------------------------------------------- #
# Result trimming — keep responses small so the LLM context stays cheap
# --------------------------------------------------------------------------- #

def _slim_tasks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ("uuid", "task", "priority", "status", "due_date", "catagory", "source")
    return [{k: r.get(k) for k in keep if k in r} for r in rows]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def build_registry() -> dict[str, Tool]:
    """Return the full allow-list. The LLM sees ``name`` and ``parameters``;
    the ``handler`` is only reachable by name via this registry."""
    _STATUSES = list(db.STATUS_VALUES)
    return {t.name: t for t in [
        Tool(
            name="list_open_tasks",
            description="List currently open tasks (Not Started or In Progress) "
                        "across tasks and recurring_tasks.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                              "description": "Max rows (default 15)."},
                },
                "additionalProperties": False,
            },
            handler=handle_list_open_tasks,
        ),
        Tool(
            name="list_overdue_tasks",
            description="List open tasks with a due_date strictly before today.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
            handler=handle_list_overdue,
        ),
        Tool(
            name="list_upcoming_tasks",
            description="List open tasks due in the next N days (default 7).",
            parameters={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 60},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
            handler=handle_list_upcoming,
        ),
        Tool(
            name="search_tasks",
            description="Case-insensitive substring search over tasks + "
                        "recurring_tasks. Use this to look up a uuid before "
                        "mutating a specific task.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "include_completed": {"type": "boolean", "default": False},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=handle_search_tasks,
        ),
        Tool(
            name="suggest_task_fields",
            description=(
                "Look up past tasks with a similar name and suggest values "
                "for category / group / sub_group / priority / estimated_time "
                "/ relevant_link. Returns {matches: int, suggestions: {...}}. "
                "ALWAYS call this before create_task so you can pre-fill "
                "fields the user didn't explicitly mention. If matches == 0 "
                "there is no history to lean on — just create with what the "
                "user gave you."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "Task name to look up history for."},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler=handle_suggest_task_fields,
        ),
        Tool(
            name="create_task",
            description=(
                "Create a new one-off task in the tasks table. Fill in as "
                "many fields as you can — use values from suggest_task_fields "
                "for anything the user did not specify. NEVER guess a due_date; "
                "if the user did not give one, leave it unset."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task title (required)."},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 5},
                    "status": {"type": "string", "enum": _STATUSES},
                    "due_date": {"type": "string",
                                 "description": "YYYY-MM-DD, 'today', or 'tomorrow'."},
                    "catagory": {"type": "string"},
                    "task_group": {"type": "string"},
                    "sub_group": {"type": "string"},
                    "relevant_link": {"type": "string"},
                    "estimated_time": {"type": "number",
                                       "description": "Hours."},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler=handle_create_task,
        ),
        Tool(
            name="complete_task",
            description="Mark a task complete. Provide `uuid` (from search) OR "
                        "`task_name` for a unique substring match.",
            parameters={
                "type": "object",
                "properties": {
                    "uuid": {"type": "string"},
                    "task_name": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=handle_complete_task,
        ),
        Tool(
            name="update_task_status",
            description="Change the status of a task. Same lookup rules as "
                        "complete_task.",
            parameters={
                "type": "object",
                "properties": {
                    "uuid": {"type": "string"},
                    "task_name": {"type": "string"},
                    "status": {"type": "string", "enum": _STATUSES},
                },
                "required": ["status"],
                "additionalProperties": False,
            },
            handler=handle_update_task_status,
        ),
        Tool(
            name="delete_task",
            description="Delete a task or recurring row. Same lookup rules as "
                        "complete_task. Confirm with the user first when the "
                        "match is ambiguous.",
            parameters={
                "type": "object",
                "properties": {
                    "uuid": {"type": "string"},
                    "task_name": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=handle_delete_task,
        ),
        Tool(
            name="list_disciplines_pending",
            description="List active disciplines not yet marked done today.",
            parameters={"type": "object", "properties": {},
                        "additionalProperties": False},
            handler=handle_list_disciplines_pending,
        ),
        Tool(
            name="plan_my_day",
            description=(
                "Build the user's focus list for today: at-risk streaks, "
                "overdue tasks, tasks due today, and remaining pending "
                "disciplines — merged and ranked so the top of the list is "
                "the single best next thing. Call this whenever the user "
                "asks 'what should I do', 'plan my day', 'what's on today', "
                "or similar."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25,
                              "description": "Max focus rows (default 10)."},
                },
                "additionalProperties": False,
            },
            handler=handle_plan_my_day,
        ),
        Tool(
            name="mark_discipline_done",
            description="Mark a discipline complete for a given day (default "
                        "today). Matches by exact or unique substring name.",
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "day": {"type": "string",
                            "description": "YYYY-MM-DD or 'today'/'yesterday'."},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler=handle_mark_discipline_done,
        ),
        Tool(
            name="add_discipline",
            description="Create a new discipline item (habit tracker row).",
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "catagory": {"type": "string"},
                    "frequency_per_week": {"type": "integer",
                                           "minimum": 0, "maximum": 7},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler=handle_add_discipline,
        ),
        Tool(
            name="create_follow_up",
            description="Create a follow-up template that fires when a trigger "
                        "task completes.",
            parameters={
                "type": "object",
                "properties": {
                    "trigger_task": {"type": "string"},
                    "follow_up_task": {"type": "string"},
                    "catagory": {"type": "string"},
                    "task_group": {"type": "string"},
                    "subgroup": {"type": "string"},
                    "relevant_link": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 5},
                    "estimated_time": {"type": "number"},
                    "due_offset_days": {"type": "integer"},
                },
                "required": ["trigger_task", "follow_up_task"],
                "additionalProperties": False,
            },
            handler=handle_create_follow_up,
        ),
    ]}


SYSTEM_PROMPT = (
    "You are the LuigiBot to-do agent, embedded in a personal task tracker.\n"
    "Use the provided tools to add, update, complete, or query tasks, "
    "disciplines, and follow-ups on the user's behalf.\n"
    "Rules:\n"
    "  - Prefer calling a tool over guessing. If you need a uuid, call "
    "search_tasks first.\n"
    "  - When creating a task: ALWAYS call suggest_task_fields FIRST with "
    "the task name. The tool returns values weighted by recency, so "
    "recently-picked priority / category / group / estimated_time from "
    "similar tasks are what you should reuse. Merge the returned "
    "suggestions into your create_task arguments, then override with "
    "anything the user explicitly said. Never overwrite an explicit user "
    "value with a suggestion. The user expects auto-fill of category / "
    "group / sub_group / priority / estimated_time / relevant_link from "
    "past similar tasks so they don't have to re-enter them.\n"
    "  - Task, category, group, and sub-group names are stored in book-"
    "title capitalization (\"Fix Kitchen Sink\", not \"fix kitchen sink\"). "
    "The server normalizes casing on save and reuses existing category "
    "spellings, so pass names naturally and don't fight the formatter.\n"
    "  - NEVER guess a due_date. Only set due_date when the user explicitly "
    "gave one — do not use dates from suggest_task_fields output.\n"
    "  - Dates must be YYYY-MM-DD, 'today', or 'tomorrow'.\n"
    "  - After a mutating call succeeds, reply briefly with what changed, "
    "including which fields you auto-filled from history (so the user can "
    "correct them). Do not restate the whole task list.\n"
    "  - Your replies may be read aloud via text-to-speech. Prefer a single "
    "short sentence for confirmations (e.g. \"Created 'Fix Kitchen Sink', "
    "priority 3, due tomorrow.\"). Put optional extra detail on a second "
    "line the user can skim visually.\n"
    "  - If a tool returns an error, read it and either retry with fixed "
    "arguments or ask the user for the missing info.\n"
    "  - Never invent uuids. Never claim to change something you did not "
    "actually call a tool for.\n"
    "  - When the user asks 'plan my day', 'what should I do today', "
    "'what's on today', or similar, call plan_my_day once and summarize "
    "the top items in a short numbered list. Do not re-query overdue / "
    "upcoming / disciplines separately — plan_my_day already merged them."
)
