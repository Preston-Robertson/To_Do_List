"""Compatibility alias for luigi_web.modules.cards.sparkline."""

import sys

from .modules.cards import sparkline as _relocated_module

sys.modules[__name__] = _relocated_module
