"""Admin control-plane router entrypoint."""

from __future__ import annotations

from fastapi import APIRouter


def create_admin_router() -> APIRouter:
    """Create and return the combined operator control-plane router."""
    from core.entrypoints.control.api import create_control_router

    return create_control_router()
