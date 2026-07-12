from laurelin.mcp.client import LaurelinClient

__all__ = ["LaurelinClient", "build_server"]


def build_server(*args, **kwargs):
    # Deferred: importing the server pulls in the optional `mcp` SDK.
    from laurelin.mcp.server import build_server as _build

    return _build(*args, **kwargs)
