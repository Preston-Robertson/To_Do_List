"""Compatibility alias for luigi_web.modules.tasks.repository."""

import sys

from .modules.tasks import repository as _relocated_module

sys.modules[__name__] = _relocated_module
