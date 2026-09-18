"""Compatibility alias for luigi_web.modules.planning.repository."""

import sys

from .modules.planning import repository as _relocated_module

sys.modules[__name__] = _relocated_module
