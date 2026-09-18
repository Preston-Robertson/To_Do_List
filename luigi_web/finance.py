"""Compatibility alias for luigi_web.modules.finance.repository."""

import sys

from .modules.finance import repository as _relocated_module

sys.modules[__name__] = _relocated_module
