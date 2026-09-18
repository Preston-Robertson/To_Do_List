"""Compatibility alias for luigi_web.modules.cards.importer."""

import sys

from .modules.cards import importer as _relocated_module

sys.modules[__name__] = _relocated_module
