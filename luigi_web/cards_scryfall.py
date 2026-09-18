"""Compatibility alias for luigi_web.modules.cards.scryfall."""

import sys

from .modules.cards import scryfall as _relocated_module

sys.modules[__name__] = _relocated_module
