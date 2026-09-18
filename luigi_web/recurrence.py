"""Compatibility alias for luigi_web.modules.tasks.recurrence."""

import sys

from .modules.tasks import recurrence as _relocated_module

sys.modules[__name__] = _relocated_module
