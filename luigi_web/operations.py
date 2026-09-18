"""Compatibility alias for luigi_web.modules.tasks.operations."""

import sys

from .modules.tasks import operations as _relocated_module

sys.modules[__name__] = _relocated_module