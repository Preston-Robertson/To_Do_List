"""Compatibility alias for luigi_web.modules.feedback.maintainer_agent."""

import sys

from .modules.feedback import maintainer_agent as _relocated_module

sys.modules[__name__] = _relocated_module