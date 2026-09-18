"""Compatibility alias for luigi_web.modules.finance.routes."""

import sys

from .modules.finance import routes as _relocated_module

sys.modules[__name__] = _relocated_module
