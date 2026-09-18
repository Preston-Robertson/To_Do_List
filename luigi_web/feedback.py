"""Compatibility alias for luigi_web.modules.feedback.repository."""

import sys

from .modules.feedback import repository as _relocated_module

sys.modules[__name__] = _relocated_module
