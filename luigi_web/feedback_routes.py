"""Compatibility alias for luigi_web.modules.feedback.routes."""

import sys

from .modules.feedback import routes as _relocated_module

sys.modules[__name__] = _relocated_module
