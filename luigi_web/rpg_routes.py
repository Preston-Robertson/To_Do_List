"""Compatibility alias for luigi_web.modules.characters.routes."""

import sys

from .modules.characters import routes as _relocated_module

sys.modules[__name__] = _relocated_module
