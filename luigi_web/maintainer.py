"""Compatibility alias for luigi_web.modules.feedback.maintainer."""

import sys

from .modules.feedback import maintainer as _relocated_module

sys.modules[__name__] = _relocated_module