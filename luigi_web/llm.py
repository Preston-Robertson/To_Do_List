"""Compatibility alias for luigi_web.modules.assistant.providers."""

import sys

from .modules.assistant import providers as _relocated_module

sys.modules[__name__] = _relocated_module
