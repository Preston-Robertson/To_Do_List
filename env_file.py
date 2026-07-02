"""Read + write the .env file that supplies LUIGI_WEB_* env vars.

Scope: this module is used only by the ``/admin`` env-editor UI. Runtime
config still comes from ``os.environ`` — changes here take effect only after
a restart.

Design rules:

* **Line-based edit.** For each key in ``updates``, replace the first line
  matching ``KEY=...``; otherwise append ``KEY=VALUE`` at the end. Comments,
  blank lines, and unknown keys stay untouched. We never rewrite the whole
  file from scratch.
* **Schema-driven writes.** Only keys present in ``KNOWN_KEYS`` are writable
  via the UI. Anything else is rejected at the ``update_env_file`` layer —
  defence in depth against an admin fat-fingering random names.
* **Atomic replace.** Write to a sibling tempfile, then ``os.replace``. Keeps
  the file valid even if the process is killed mid-write.
* **Value discipline.** Newlines and control chars are rejected. Values
  containing spaces, ``"`` , ``'``, ``#`` or ``=`` are quoted with double
  quotes and internal ``"``/``\\`` are escaped. Bare simple values are written
  unquoted so the file stays readable.
* **Secret handling.** ``is_secret=True`` keys are masked in the UI. The save
  handler treats an *empty* submission for a secret as "keep the current
  value" so an admin can't accidentally blank a password by not typing it.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import OrderedDict as OrderedDictT

# Keys must be ASCII: [A-Z_][A-Z0-9_]*
_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Full "KEY=..." line matcher (ignores leading spaces so `#` comments are safe).
_LINE_RE = re.compile(r"^(?P<key>[A-Z_][A-Z0-9_]*)\s*=\s*(?P<val>.*)$")


@dataclass(frozen=True)
class EnvKey:
    name: str
    label: str
    description: str
    group: str
    is_secret: bool = False
    input_type: str = "text"           # text | number | url


# The exact set of keys we let the admin UI edit. Order = render order.
# Anything else in the .env file is left alone (comments, unrecognised keys).
KNOWN_KEYS: tuple[EnvKey, ...] = (
    # Postgres --------------------------------------------------------------
    EnvKey("LUIGI_WEB_PG_HOST",     "Postgres host",     "LuigiBot DB host (e.g. 10.0.0.202).",           "Postgres"),
    EnvKey("LUIGI_WEB_PG_PORT",     "Postgres port",     "Default 5432.",                                  "Postgres", input_type="number"),
    EnvKey("LUIGI_WEB_PG_DB",       "Postgres database", "Usually 'luigi_todo'.",                          "Postgres"),
    EnvKey("LUIGI_WEB_PG_USER",     "Postgres user",     "Role the GUI connects as (e.g. luigi_web).",     "Postgres"),
    EnvKey("LUIGI_WEB_PG_PASSWORD", "Postgres password", "Password for the DB role. Blank = keep current.", "Postgres", is_secret=True),
    # Web / auth ------------------------------------------------------------
    EnvKey("LUIGI_WEB_UI_TOKEN",    "UI token",          "Shared login token. Blank = keep current.",      "Web",      is_secret=True),
    EnvKey("LUIGI_WEB_BIND",        "Bind address",      "Uvicorn bind address (default 0.0.0.0).",        "Web"),
    EnvKey("LUIGI_WEB_PORT",        "Bind port",         "Uvicorn port (default 8080).",                   "Web",      input_type="number"),
    # LLM chat --------------------------------------------------------------
    EnvKey("LUIGI_WEB_LLM_PROVIDER",           "LLM provider",  "'openai' (any OpenAI-compatible) or 'disabled'.", "LLM"),
    EnvKey("LUIGI_WEB_LLM_BASE_URL",           "LLM base URL",  "OpenAI-compatible /chat/completions endpoint.",   "LLM", input_type="url"),
    EnvKey("LUIGI_WEB_LLM_API_KEY",            "LLM API key",   "Blank disables the chat panel. Blank on save = keep current.", "LLM", is_secret=True),
    EnvKey("LUIGI_WEB_LLM_MODEL",              "LLM model",     "e.g. openai/gpt-4o-mini, llama3.1:8b.",           "LLM"),
    EnvKey("LUIGI_WEB_LLM_TIMEOUT",            "LLM timeout",   "HTTP timeout in seconds (default 60).",           "LLM", input_type="number"),
    EnvKey("LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS","LLM tool cap",  "Max tool round-trips per user turn (default 5).", "LLM", input_type="number"),
)

_KEYS_BY_NAME: dict[str, EnvKey] = {k.name: k for k in KNOWN_KEYS}


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

def env_file_path(repo_dir: Path) -> Path:
    """Where the .env file lives.

    Priority: ``LUIGI_WEB_ENV_FILE`` env var (set to ``/etc/luigi-web.env`` on
    the LXC) → ``<repo_dir>/.env`` locally.
    """
    override = os.environ.get("LUIGI_WEB_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return repo_dir / ".env"


def env_file_writable(path: Path) -> tuple[bool, str]:
    """Return (writable, reason). Reason is a short human-readable string.

    ``writable`` is only True if we can atomically replace the file: the file
    itself and its parent directory must both be writable by the process user.
    """
    if not path.exists():
        # If the parent is writable we could create it, but the UI's contract
        # is 'edit existing settings' — refuse to create files at admin whim.
        return False, f"{path} does not exist"
    if not os.access(path, os.W_OK):
        return False, f"{path} is not writable by user {os.getlogin() if hasattr(os, 'getlogin') else os.geteuid()}"
    if not os.access(path.parent, os.W_OK):
        return False, f"parent directory {path.parent} is not writable (needed for atomic replace)"
    return True, ""


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

def read_env_file(path: Path) -> "OrderedDictT[str, str]":
    """Return an ordered dict of KEY → unquoted string value.

    Malformed lines and comments are ignored. Duplicates: the *last* value
    wins, matching how most dotenv loaders resolve the file.
    """
    from collections import OrderedDict
    out: OrderedDictT[str, str] = OrderedDict()
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key = m.group("key")
        val = _unquote(m.group("val"))
        out[key] = val
    return out


def _unquote(v: str) -> str:
    v = v.rstrip()
    # Strip an inline `# comment` when the value is unquoted.
    if v and v[0] not in ("'", '"'):
        hash_pos = v.find(" #")
        if hash_pos != -1:
            v = v[:hash_pos].rstrip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return v


# --------------------------------------------------------------------------- #
# Validate + write
# --------------------------------------------------------------------------- #

class EnvUpdateError(ValueError):
    """Raised when an update payload is invalid. Message is safe to show."""


def _validate_value(key: str, val: str) -> str:
    if not isinstance(val, str):
        raise EnvUpdateError(f"{key}: value must be a string")
    if "\n" in val or "\r" in val:
        raise EnvUpdateError(f"{key}: value must not contain newlines")
    if any(ord(c) < 32 and c != "\t" for c in val):
        raise EnvUpdateError(f"{key}: value contains control characters")
    return val


def _quote_value(val: str) -> str:
    """Serialize a value so it round-trips through _unquote."""
    if val == "":
        return ""
    needs_quotes = any(c in val for c in (" ", "\t", '"', "'", "#", "=", "$"))
    if not needs_quotes:
        return val
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env_file(
    path: Path,
    updates: dict[str, str],
    *,
    known_only: bool = True,
) -> list[str]:
    """Apply ``updates`` to ``path`` and return the list of keys actually
    written (order matches the file's final order).

    * Existing ``KEY=...`` lines are edited in place, preserving surrounding
      comments and blank lines.
    * New keys are appended at the end under a ``# --- Added by admin UI ---``
      comment on first append.
    * Any key not in ``KNOWN_KEYS`` raises ``EnvUpdateError`` when
      ``known_only`` is True (the default).
    * Atomic: writes to a sibling tempfile then ``os.replace``.
    """
    writable, reason = env_file_writable(path)
    if not writable:
        raise EnvUpdateError(f"cannot write env file: {reason}")

    validated: dict[str, str] = {}
    for key, val in updates.items():
        if not _KEY_RE.match(key or ""):
            raise EnvUpdateError(f"invalid key name: {key!r}")
        if known_only and key not in _KEYS_BY_NAME:
            raise EnvUpdateError(f"key {key!r} is not in the managed schema")
        validated[key] = _validate_value(key, val)

    lines = path.read_text(encoding="utf-8").splitlines(keepends=False)
    seen: set[str] = set()
    new_lines: list[str] = []

    for raw in lines:
        m = _LINE_RE.match(raw.strip())
        if not m:
            new_lines.append(raw)
            continue
        key = m.group("key")
        if key in validated and key not in seen:
            new_lines.append(f"{key}={_quote_value(validated[key])}")
            seen.add(key)
        else:
            new_lines.append(raw)

    remaining = [k for k in validated if k not in seen]
    if remaining:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append("# --- Added by admin UI ---")
        for key in remaining:
            new_lines.append(f"{key}={_quote_value(validated[key])}")

    content = "\n".join(new_lines) + "\n"

    # Atomic replace in the same directory (required for os.replace on Windows
    # when the source is on a different drive would fail; sibling avoids that).
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        # Match the mode of the original file so we don't accidentally widen it.
        try:
            os.chmod(tmp_path, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Final order: keys as they appear in the (now-updated) file.
    final = read_env_file(path)
    return [k for k in final.keys() if k in validated]


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #

def masked_display(val: str, *, keep: int = 2) -> str:
    """'sk-1234...def' → '••••••••ef' style mask for the UI."""
    if not val:
        return ""
    if len(val) <= keep:
        return "•" * 8
    return "•" * 8 + val[-keep:]


def grouped_view(current: dict[str, str]) -> list[dict[str, object]]:
    """Return [{group, entries: [{key, value, display, ...}, ...]}, ...] for
    rendering the admin form. Only KNOWN_KEYS appear.

    ``entries`` is deliberately named (not ``keys``) so Jinja's dot access
    doesn't collide with the builtin ``dict.keys`` method.
    """
    from collections import OrderedDict
    groups: "OrderedDictT[str, list[dict[str, object]]]" = OrderedDict()
    for k in KNOWN_KEYS:
        val = current.get(k.name, "")
        entry = {
            "spec": k,
            "value": val,
            "display": masked_display(val) if k.is_secret else val,
            "has_value": bool(val),
        }
        groups.setdefault(k.group, []).append(entry)
    return [{"group": g, "entries": ks} for g, ks in groups.items()]
