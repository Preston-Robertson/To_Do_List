"""Compatibility alias for luigi_web.modules.cards.pokemon."""

import sys

from .modules.cards import pokemon as _relocated_module

sys.modules[__name__] = _relocated_module
