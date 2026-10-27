"""Security-test conftest.

Activates the same ``e2e_authenticated_requests`` autouse fixture that
``tests/e2e/conftest.py`` uses so that security tests marked with
``@pytest.mark.e2e`` have their ``requests.*`` calls automatically
authenticated.  Without this, the fixture (defined inside the ``e2e/``
sub-package conftest) is not in scope for tests living in ``security/``.
"""

from __future__ import annotations

# Re-export the fixture so pytest can discover it in this directory.
# The import makes ``e2e_authenticated_requests`` available as if it were
# defined here, giving all ``tests/security/`` tests access to authed HTTP.
from tests.e2e.conftest import (
    e2e_authenticated_requests as e2e_authenticated_requests,
)
