"""Filesystem locations shared by Luigi Web modules."""
from __future__ import annotations

from pathlib import Path
import os
import sys

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "core" / "static"
TEMPLATES_DIR = PACKAGE_DIR / "core" / "templates"
_SOURCE_CHECKOUT = all(
	(PROJECT_ROOT / filename).is_file() for filename in ("app.py", "requirements.txt")
)


def _default_data_dir() -> Path:
	if _SOURCE_CHECKOUT:
		return PROJECT_ROOT / "data"
	if sys.platform == "win32":
		return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "luigi-web"
	if sys.platform == "darwin":
		return Path.home() / "Library" / "Application Support" / "luigi-web"
	return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "luigi-web"


DATA_DIR = Path(os.environ.get("LUIGI_WEB_DATA_DIR") or _default_data_dir()).expanduser().resolve()
_LEGACY_DATA_DIR = (
	PROJECT_ROOT if _SOURCE_CHECKOUT and not os.environ.get("LUIGI_WEB_DATA_DIR") else DATA_DIR
)
COPILOT_DATA_DIR = DATA_DIR / "copilot"
FEEDBACK_DB_PATH = DATA_DIR / "feedback.db"
REVIEW_DB_PATH = DATA_DIR / "review.db"
OPERATIONS_DB_PATH = DATA_DIR / "operations.db"
CARDS_DB_PATH = DATA_DIR / "cards.db"
RPG_DB_PATH = DATA_DIR / "rpg.db"
TASK_METADATA_PATH = Path(
	os.environ.get(
		"LUIGI_WEB_TASK_METADATA_FILE",
		str(_LEGACY_DATA_DIR / "task-web-metadata.json"),
	)
).expanduser().resolve()
GNW_CREDENTIALS_PATH = _LEGACY_DATA_DIR / "gnw-credentials.json"