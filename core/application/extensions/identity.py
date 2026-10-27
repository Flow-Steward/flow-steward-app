"""Application-facing API for the shared extension package identity contract."""

from core.infrastructure.extension_runtime.package_identity import (
    CANONICAL_EXTENSION_ID_RE as CANONICAL_EXTENSION_ID_RE,
)
from core.infrastructure.extension_runtime.package_identity import (
    extension_folder_name as extension_folder_name,
)
from core.infrastructure.extension_runtime.package_identity import (
    is_canonical_extension_id as is_canonical_extension_id,
)
from core.infrastructure.extension_runtime.package_identity import (
    normalize_extension_id as normalize_extension_id,
)
