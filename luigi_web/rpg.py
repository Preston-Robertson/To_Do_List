"""Compatibility alias for luigi_web.modules.characters.repository."""

import sys

from .modules.characters import repository as _relocated_module

sys.modules[__name__] = _relocated_module
