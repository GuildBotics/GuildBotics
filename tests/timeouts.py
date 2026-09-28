"""How long a test may run before it fails instead of stopping the suite.

Every test has the default ``timeout`` of the pytest settings in
``pyproject.toml``; the tests here name what they take beyond it.
"""

import pytest

#: A test on this device's real agent environment: it boots a microVM, and
#: waits on provider CLIs and AI turns.
REAL_DEVICE = pytest.mark.timeout(30 * 60)
