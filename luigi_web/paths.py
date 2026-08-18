"""Filesystem locations shared by Luigi Web modules."""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
DATA_DIR = PROJECT_ROOT / "data"
COPILOT_DATA_DIR = DATA_DIR / "copilot"
TASK_METADATA_PATH = PROJECT_ROOT / "task-web-metadata.json"
GNW_CREDENTIALS_PATH = PROJECT_ROOT / "gnw-credentials.json"