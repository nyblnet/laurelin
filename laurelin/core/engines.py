"""Delegated compute — running a query on someone else's cluster.

Laurelin's own compute is DuckDB in one process, which covers medium data
well and a selective scan over large data adequately. What it cannot do is a
non-selective aggregate over billions of rows.

The answer is not to build a distributed engine. Organizations that have data
at that scale already have an engine for it — Trino, Dremio, Databricks,
Snowflake — and what they lack is a governed semantic layer over it. So an
*engine* here is a remote SQL cluster that Laurelin submits work to and
receives Arrow back from. Laurelin contributes orchestration, lineage and
policy; it contributes no compute.

**Explicit non-goals.** No shuffle, no distributed joins, no cluster manager,
no cross-node query planner. Those are what make a distributed engine large,
and a half-built version of them would be worse than the engines that already
exist.

Transport is ADBC over Flight SQL — an open protocol rather than a vendor SDK,
so one driver reaches Trino, Dremio, Databricks and anything else that speaks
it. Results arrive as Arrow, which is what the catalog already wants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

import pyarrow as pa

from laurelin.core import redaction

ENGINE_TYPES = ("flightsql",)


class EngineError(RuntimeError):
    """A delegated engine could not be reached, or rejected the query."""


class EngineClient(Protocol):
    """Minimal contract a delegated engine must satisfy.

    Deliberately tiny: everything Laurelin needs from a cluster is "run this
    SQL and give me Arrow back". Keeping it this small is what stops engine
    support from turning into engine *implementation* — and it lets the
    orchestration above be tested without a cluster.
    """

    def query(self, sql: str, params: Optional[list] = None) -> pa.Table: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class EngineConfig:
    name: str
    type: str = "flightsql"
    uri: str = ""
    options: dict[str, Any] = None  # type: ignore[assignment]

    def redacted(self) -> dict:
        """This config as an API response may carry it.

        Every option *value* goes, not just the secret-named ones. These
        options are handed to the driver as ``db_kwargs`` (see
        ``FlightSQLClient``), so the key vocabulary is ADBC's, not ours, and
        matching names over somebody else's namespace is measurably a guess:
        ``adbc.flight.sql.rpc.call_header.authorization`` contains none of
        ``password|secret|token|key|credential`` and shipped ``Bearer SEKRET``
        verbatim from ``GET /api/v1/engines``. The option names survive so the
        operator can see which knobs are set.
        """
        return {"name": self.name, "type": self.type,
                "uri": _redact_uri(self.uri),
                "options": redaction.withhold_values(self.options or {})}


def _redact_uri(uri: str) -> str:
    """A URI as a response may carry it — masked, or withheld whole.

    The regex this replaced required a username before the ':' and stopped the
    password at the first '@', so ``grpc+tls://:hunter2@trino:443`` and
    ``?api_key=SEKRET`` both came back untouched. See ``core/redaction.py``.
    """
    return redaction.redact_dsn(uri or "")


def validate_engine(config: dict) -> None:
    type_ = config.get("type", "flightsql")
    if type_ not in ENGINE_TYPES:
        raise ValueError(
            f"Unknown engine type {type_!r}: expected one of {', '.join(ENGINE_TYPES)}"
        )
    uri = str(config.get("uri", ""))
    if not uri.startswith(("grpc://", "grpc+tls://", "grpc+tcp://")):
        raise ValueError(
            "A Flight SQL engine needs a grpc:// or grpc+tls:// uri "
            "(e.g. grpc+tls://trino.internal:443)"
        )
    options = config.get("options", {})
    if not isinstance(options, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in options.items()
    ):
        raise ValueError("engine options must be a string-to-string object")


# ---------------------------------------------------------------------------
# Flight SQL
# ---------------------------------------------------------------------------

class FlightSQLClient:
    """ADBC Flight SQL client. One driver reaches every engine speaking the
    protocol, so adding Trino or Dremio is configuration, not code."""

    def __init__(self, config: EngineConfig, timeout_s: float = 300.0):
        try:
            import adbc_driver_flightsql.dbapi as flight_sql
            from adbc_driver_flightsql import DatabaseOptions
        except ImportError as exc:  # pragma: no cover - optional extra
            raise EngineError(
                "Delegated engines need the Flight SQL driver: "
                "pip install 'laurelin[engines]'"
            ) from exc

        db_kwargs = dict(config.options or {})
        db_kwargs.setdefault(DatabaseOptions.TIMEOUT_QUERY.value, str(timeout_s))
        try:
            self._con = flight_sql.connect(config.uri, db_kwargs=db_kwargs)
        except Exception as exc:  # noqa: BLE001 - driver raises many types
            raise EngineError(
                f"Could not connect to engine {config.name!r} at "
                f"{_redact_uri(config.uri)}: {exc}"
            ) from exc

    def query(self, sql: str, params: Optional[list] = None) -> pa.Table:
        try:
            with self._con.cursor() as cur:
                cur.execute(sql, params or None)
                return cur.fetch_arrow_table()
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"Engine rejected the query: {exc}") from exc

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # noqa: BLE001
            pass


def connect(config: EngineConfig, timeout_s: float = 300.0) -> EngineClient:
    # An imported engine carries its option *keys* and no values, which is a
    # shape awaiting a credential rather than a registration that failed. Said
    # here so it is one message instead of whatever ADBC reports about an empty
    # DSN.
    from laurelin.export.manifest import NEEDS_CREDENTIALS_KEY, NeedsCredentials

    if (config.options or {}).get(NEEDS_CREDENTIALS_KEY):
        raise NeedsCredentials(
            f"Engine {config.name!r} was imported without its URI. Re-supply it "
            "(PUT /api/v1/engines/{name} or Admin -> Engines) before using it."
        )
    if config.type == "flightsql":
        return FlightSQLClient(config, timeout_s=timeout_s)
    raise ValueError(f"Unknown engine type {config.type!r}")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def check_result_size(table: pa.Table, max_rows: int, engine: str) -> pa.Table:
    """Refuse a delegated result that is too large to be worth landing.

    Delegation exists so the *cluster* does the reduction. A query returning
    millions of rows has usually not reduced anything, and pulling it defeats
    the point — so this fails loudly rather than quietly filling the disk.
    """
    if max_rows > 0 and table.num_rows > max_rows:
        raise EngineError(
            f"Engine {engine!r} returned {table.num_rows:,} rows, above the "
            f"{max_rows:,} limit. Delegated queries should aggregate on the "
            f"cluster; raise LAURELIN_ENGINE_MAX_ROWS if this is intended."
        )
    return table
