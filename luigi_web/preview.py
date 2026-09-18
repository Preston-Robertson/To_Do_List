"""Compatibility alias for luigi_web.modules.preview.service."""

import sys

from .modules.preview import service as _relocated_module

sys.modules[__name__] = _relocated_module
