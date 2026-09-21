"""Namespace for independently packaged features and local development roots."""
from pathlib import Path
from pkgutil import extend_path
from ..core.module_bootstrap import feature_paths

__path__ = [*feature_paths(), *extend_path(__path__, __name__)]
_checkout = Path(__file__).resolve().parents[2]
_repositories = _checkout / "module-repos"
if (_checkout / "app.py").is_file() and _repositories.is_dir():
	for _identifier in ("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback"):
		_source = _repositories / _identifier / "src" / "luigi_web" / "modules"
		if _source.is_dir() and not _source.is_symlink():
			__path__.append(str(_source))
