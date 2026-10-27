"""Pure boundary validation for operator effect resolution commands."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from core.application.extensions.external_effect_resolution import (
    ExternalEffectResolutionError,
    resolve_external_effect,
)


@pytest.mark.parametrize("version", [True, False, 1.5, "1", 0, -1, None])
def test_resolution_version_must_be_a_positive_integer_before_accessing_storage(version):
    db = MagicMock()
    with pytest.raises(ExternalEffectResolutionError, match="version_invalid"):
        resolve_external_effect(
            db,
            account_id=str(uuid4()),
            project_id=str(uuid4()),
            effect_id=str(uuid4()),
            observed_version=version,
            action="reject",
            comment="The operator verified that the operation did not happen.",
            actor_id="operator",
        )
    db.cursor.assert_not_called()
