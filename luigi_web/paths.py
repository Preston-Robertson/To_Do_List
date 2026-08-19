"""Filesystem locations shared by Luigi Web modules."""
from __future__ import annotations

from pathlib import Path
import os

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
DATA_DIR = PROJECT_ROOT / "data"
COPILOT_DATA_DIR = DATA_DIR / "copilot"
FEEDBACK_DB_PATH = DATA_DIR / "feedback.db"
TASK_METADATA_PATH = Path(
	os.environ.get(
		"LUIGI_WEB_TASK_METADATA_FILE",
		str(PROJECT_ROOT / "task-web-metadata.json"),
	)
).expanduser().resolve()
GNW_CREDENTIALS_PATH = PROJECT_ROOT / "gnw-credentials.json"