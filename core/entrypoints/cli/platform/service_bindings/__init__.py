"""Per-domain CLI bindings resolved explicitly by ``ServiceRegistry``.

This package intentionally exports no aggregate maps: importing one binding must
not initialize every public command domain in the standalone CLI process.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
