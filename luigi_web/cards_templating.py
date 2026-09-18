"""Compatibility alias for luigi_web.modules.cards.templating."""

import sys

from .modules.cards import templating as _relocated_module

sys.modules[__name__] = _relocated_module
