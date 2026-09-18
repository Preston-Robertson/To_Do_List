"""Compatibility alias for luigi_web.modules.tasks.backup."""

import sys

from .modules.tasks import backup as _relocated_module

sys.modules[__name__] = _relocated_module
