"""Metrics — making the platform's own behaviour visible.

Laurelin now does a lot on its own: a scheduler fires pipelines, builds change
hands between replicas, and resource limits reject queries. All of that is
invisible without instrumentation, and "the platform silently did something"
is a bad property for an operator.

``prometheus_client`` is an **optional** extra. When it isn't installed every
metric here is a no-op with the same interface, so instrumentation can be
written unconditionally at the call sites and costs nothing to those who don't
want it.

**Label discipline.** Labels are bounded sets — route templates, statuses,
reasons. Never a dataset name, workspace slug, user, or query text: those are
unbounded, and unbounded labels are how monitoring turns into an outage.
"""

from __future__ import annotations

import os
from typing import Any, Optional

try:  # pragma: no cover - exercised both ways in tests via _AVAILABLE
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


class _NoOpMetric:
    """Stands in for a metric when prometheus_client isn't installed.

    Supports the same calls so instrumentation never needs a conditional.
    """

    def labels(self, *args: Any, **kwargs: Any) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1) -> None:
        return None

    def dec(self, amount: float = 1) -> None:
        return None

    def set(self, value: float) -> None:
        return None

    def observe(self, value: float) -> None:
        return None


def available() -> bool:
    return _AVAILABLE


def enabled() -> bool:
    """Whether /metrics should serve anything."""
    return _AVAILABLE and os.environ.get("LAURELIN_METRICS", "1") != "0"


def public() -> bool:
    """Whether /metrics may be scraped without credentials.

    Off by default: metric *names* are harmless but counts leak activity
    patterns, so this should only be turned on when the port isn't exposed to
    users.
    """
    return os.environ.get("LAURELIN_METRICS_PUBLIC") == "1"


registry: Optional[Any] = CollectorRegistry() if _AVAILABLE else None


def _counter(name: str, doc: str, labels: tuple[str, ...] = ()) -> Any:
    if not _AVAILABLE:
        return _NoOpMetric()
    return Counter(name, doc, list(labels), registry=registry)


def _histogram(name: str, doc: str, labels: tuple[str, ...] = (), buckets=None) -> Any:
    if not _AVAILABLE:
        return _NoOpMetric()
    kwargs = {"registry": registry}
    if buckets is not None:
        kwargs["buckets"] = buckets
    return Histogram(name, doc, list(labels), **kwargs)


def _gauge(name: str, doc: str, labels: tuple[str, ...] = ()) -> Any:
    if not _AVAILABLE:
        return _NoOpMetric()
    return Gauge(name, doc, list(labels), registry=registry)


# -- HTTP ---------------------------------------------------------------------

# Route *templates* ("/api/v1/datasets/{name}"), never concrete paths, so
# cardinality stays bounded by the route table.
http_requests = _counter(
    "laurelin_http_requests_total",
    "HTTP requests handled",
    ("method", "route", "status"),
)
http_duration = _histogram(
    "laurelin_http_request_seconds",
    "HTTP request duration",
    ("method", "route"),
)

# -- queries ------------------------------------------------------------------

queries = _counter("laurelin_queries_total", "SQL queries executed", ("surface",))
query_duration = _histogram(
    "laurelin_query_seconds",
    "SQL query duration",
    ("surface",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 15, 60, 300),
)
# The reason matters operationally: a timeout means "too slow", memory means
# "too big", admission means "too busy" — three different remedies.
query_rejections = _counter(
    "laurelin_query_rejections_total",
    "Queries refused by a resource limit",
    ("reason",),
)
queries_in_flight = _gauge(
    "laurelin_queries_in_flight", "Queries currently executing"
)

# -- builds -------------------------------------------------------------------

builds = _counter("laurelin_builds_total", "Builds finished", ("status",))
build_duration = _histogram(
    "laurelin_build_seconds",
    "Build duration",
    buckets=(0.1, 1, 5, 30, 60, 300, 1800, 7200),
)
build_claims = _counter(
    "laurelin_build_claims_total",
    "Attempts to claim a build lease",
    ("outcome",),  # won | lost
)
builds_reaped = _counter(
    "laurelin_builds_reaped_total", "Builds failed after their lease expired"
)

# -- scheduler ----------------------------------------------------------------

schedule_fires = _counter(
    "laurelin_schedule_fires_total", "Schedules fired", ("trigger", "status")
)
scheduler_ticks = _counter("laurelin_scheduler_ticks_total", "Scheduler poll passes")

# -- connectors & engines ------------------------------------------------------

syncs = _counter("laurelin_source_syncs_total", "Connector syncs", ("type", "status"))
sync_rows = _counter("laurelin_source_sync_rows_total", "Rows ingested by syncs")
engine_queries = _counter(
    "laurelin_engine_queries_total", "Delegated engine queries", ("status",)
)


def render() -> bytes:
    """The Prometheus exposition payload."""
    if not _AVAILABLE or registry is None:
        return b""
    return generate_latest(registry)


def content_type() -> str:
    return CONTENT_TYPE_LATEST
