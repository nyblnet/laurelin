"""Structured logging.

Line-oriented logs are fine on a laptop and painful across replicas. Setting
``LAURELIN_LOG_FORMAT=json`` emits one JSON object per record, with the
request id, workspace and actor attached — so a request can be followed
across the replicas that handled it and the background work it started.

Kept dependency-free: this is a stdlib ``Formatter`` and a ``ContextVar``, not
a logging framework.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any, Optional

# Carried through async request handling and inherited by tasks the request
# spawns, so background work stays attributable to what asked for it.
request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
workspace_slug: ContextVar[Optional[str]] = ContextVar("workspace_slug", default=None)
actor: ContextVar[Optional[str]] = ContextVar("actor", default=None)

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"asctime", "message", "taskName"}


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with request context folded in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in (
            ("request_id", request_id.get()),
            ("workspace", workspace_slug.get()),
            ("actor", actor.get()),
        ):
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything passed via `extra=` rides along, so call sites can attach
        # structure without a bespoke logger.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def configure() -> None:
    """Install the configured format on the root logger.

    Idempotent, and a no-op unless JSON is requested — a laptop should keep
    readable logs by default.
    """
    if os.environ.get("LAURELIN_LOG_FORMAT", "").lower() != "json":
        return
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_laurelin_json", False):
            return  # already configured
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._laurelin_json = True  # type: ignore[attr-defined]
    root.handlers = [handler]
    root.setLevel(os.environ.get("LAURELIN_LOG_LEVEL", "INFO").upper())
