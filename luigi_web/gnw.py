"""Compatibility alias for luigi_web.modules.media.service."""

import sys

from .modules.media import service as _relocated_module

sys.modules[__name__] = _relocated_module
