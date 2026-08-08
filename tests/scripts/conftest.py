"""Make the repository scripts importable by the tests in this directory.

The digests live under scripts/ rather than in the package, so the directory has
to be on sys.path before the test modules import them.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
