"""Platform-module loader (ADR 0082).

Kept outside ``core.infrastructure.runtime`` so that every image, including the
management image, can carry it: the loader imports only the standard library and a
few small infrastructure helpers. This package deliberately imports nothing; use
``core.infrastructure.platform_modules.loader``.
"""
