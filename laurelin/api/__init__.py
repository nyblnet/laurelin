"""FastAPI server: REST API + static UI mount."""

from laurelin.api.app import create_app, create_server_app

__all__ = ["create_app", "create_server_app"]
