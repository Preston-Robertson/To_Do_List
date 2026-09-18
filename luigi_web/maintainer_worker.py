"""Compatibility alias and CLI for luigi_web.modules.feedback.maintainer_worker."""

import sys

from .modules.feedback import maintainer_worker as _relocated_module

if __name__ == "__main__":
    raise SystemExit(_relocated_module.main())

sys.modules[__name__] = _relocated_module