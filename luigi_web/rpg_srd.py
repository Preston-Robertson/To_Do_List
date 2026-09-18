"""Compatibility alias for luigi_web.modules.characters.srd."""

import sys

from .modules.characters import srd as _relocated_module

sys.modules[__name__] = _relocated_module
