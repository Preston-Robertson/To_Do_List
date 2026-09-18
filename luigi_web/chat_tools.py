"""Compatibility alias for luigi_web.modules.assistant.tools."""

import sys

from .modules.assistant import tools as _relocated_module

sys.modules[__name__] = _relocated_module
