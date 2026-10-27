"""Application contracts and source policy for installation Clone operations."""

from core.application.clone.contracts import (
    CloneDataClass,
    CloneEntry,
    CloneEnvelopeHeader,
    CloneKdfHeader,
    CloneManifest,
)
from core.application.clone.manifest import (
    CloneSourceItem,
    CloneSourcePlan,
    build_source_allowlist,
)
from core.application.clone.operation import (
    CloneRestoreConfirmation,
    CloneRestoreResult,
    clone_restore_definition,
)
from core.application.clone.planning import (
    CloneReplacementPlan,
    CloneTargetShell,
    PreparedCloneRestore,
)
from core.application.clone.service import (
    CloneCreatePlan,
    CloneCreateRequest,
    CloneCreateResult,
    CloneRestoreRequest,
    CloneService,
    clone_create_definition,
)

__all__ = [
    "CloneCreatePlan",
    "CloneCreateRequest",
    "CloneCreateResult",
    "CloneDataClass",
    "CloneEntry",
    "CloneEnvelopeHeader",
    "CloneKdfHeader",
    "CloneManifest",
    "CloneReplacementPlan",
    "CloneRestoreConfirmation",
    "CloneRestoreRequest",
    "CloneRestoreResult",
    "CloneService",
    "CloneSourceItem",
    "CloneSourcePlan",
    "CloneTargetShell",
    "PreparedCloneRestore",
    "build_source_allowlist",
    "clone_create_definition",
    "clone_restore_definition",
]
