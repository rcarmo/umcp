"""Shared pytest configuration for the umcp test suite.

Adds the repository root and the ``examples/`` directory to ``sys.path``
so individual test modules can import ``umcp``, ``aioumcp``, and the
example servers without further boilerplate.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

for path in (ROOT, EXAMPLES):
    spath = str(path)
    if spath not in sys.path:
        sys.path.insert(0, spath)
