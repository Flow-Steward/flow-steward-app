"""Single ownership policy for durable operation state during Clone."""

CLONE_TARGET_RETAINED_STATE_NAMES = frozenset(
    {
        "active-revision.json",
        "compose.env",
        "operations",
        "runtime",
        "secret-input",
        "secret-output",
        "logs",
        "backups",
        "locks",
        "clone",
        "install-state.json",
        "installed-release.json",
        "pending-release.json",
        "release-handoff.json",
        "release-transition-binding.json",
        "release-handoff-secret.txt",
        "release-handoff-bootstrap.py",
        "stack.toml",
        "install.lock",
    }
)

# Target-local operation artifacts are never imported from the source archive.
CLONE_SOURCE_EXCLUDED_STATE_NAMES = CLONE_TARGET_RETAINED_STATE_NAMES

__all__ = [
    "CLONE_SOURCE_EXCLUDED_STATE_NAMES",
    "CLONE_TARGET_RETAINED_STATE_NAMES",
]
