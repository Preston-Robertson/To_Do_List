"""Compatibility alias for luigi_web.modules.cards.routes."""

import sys

from .modules.cards import routes as _relocated_module

sys.modules[__name__] = _relocated_module
