"""Compatibility alias for luigi_web.modules.admin.environment."""

import sys

from .modules.admin import environment as _relocated_module

sys.modules[__name__] = _relocated_module
