"""Compatibility alias for luigi_web.modules.cards.repository."""

import sys

from .modules.cards import repository as _relocated_module

sys.modules[__name__] = _relocated_module
