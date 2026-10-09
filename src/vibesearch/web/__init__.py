"""Single-worker local ASGI application. See web/README.md for request schemas."""
from .app import create_app

__all__ = ['create_app']
