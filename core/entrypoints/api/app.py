"""ASGI composition root for the Flow Steward HTTP application."""

from __future__ import annotations

from fastapi import FastAPI

from core.entrypoints.api import dependencies
from core.entrypoints.api.app_factory import create_app as create_runtime_app


def create_app() -> FastAPI:
    """Build the API and explicitly wire entrypoint-owned dependencies once."""
    return create_runtime_app(initialize_api_dependencies=dependencies.init)


app = create_app()

__all__ = ["app", "create_app"]
