"""Compatibility alias for luigi_web.modules.tasks.events."""

import sys

from .modules.tasks import events as _relocated_module

sys.modules[__name__] = _relocated_module
