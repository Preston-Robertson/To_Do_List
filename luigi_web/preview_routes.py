"""Compatibility alias for luigi_web.modules.preview.routes."""

import sys

from .modules.preview import routes as _relocated_module

sys.modules[__name__] = _relocated_module
