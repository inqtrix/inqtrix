"""Inqtrix's default same-origin web gateway."""

from .app import build_app, create_app_from_env
from .settings import CollaborationProxySettings

__all__ = [
    "CollaborationProxySettings",
    "build_app",
    "create_app_from_env",
]
