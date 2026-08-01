"""ClickHouse-backed datasets — governed, read-only, embedded.

A sibling of :mod:`laurelin.core.federation` rather than an extension of it.
Federation's ``scan_expression`` returns ``(expr, positional_params,
duckdb_extensions)`` and all three parts break here: ClickHouse has no
positional placeholder, no DuckDB extensions, and its scan expression carries
its path as an escaped literal.

**What this slice is.** ClickHouse is the second dialect Laurelin's governance
layer renders to, and the point of adding it is to prove the "one decision, N
renderers" claim on an engine whose rules genuinely differ from DuckDB's — see
:mod:`laurelin.core.dialects` for the three measured divergences that each
would have been a silent leak.

**What this slice is not**, stated here rather than discovered:

* **No writes.** No INSERT, no MergeTree ingest. ``catalog.write``/``append``/
  ``upload_file`` refuse a clickhouse dataset outright. A serving tier is a
  different project; half of one produces a dataset that is part local Parquet
  and part remote table, and reads only the remote half.
* **No server mode.** chdb (embedded, in-process) only. Talking to a real
  ClickHouse server needs a version gate, TLS, credential storage and a
  settings-profile threat analysis, none of which this slice buys. The
  ``clickhouse-connect`` client is deliberately not a dependency.
* **No versions or time travel.** ``latest_version`` stays ``None``, as for
  federated.
* **No ontology object types.** Refused, exactly as for federated: every object
  page would be a full source scan with no stable row identity.

**Sandbox — read this before enabling the workbench.** Federation's
``connect()`` hardens DuckDB with ``disabled_filesystems`` and
``lock_configuration``. **chdb has no equivalent.** Measured:
``chdb.query("SELECT count(*) FROM file('/etc/passwd', LineAsString)")``
succeeds, and ``readonly=1`` rejects the whole query rather than restricting
the filesystem. The primary control still holds — only server-generated SQL
ever reaches the engine, because callers receive an Arrow table and never a
connection — but the defense in depth that federated sources get is *reduced*
here. That is the reason ClickHouse datasets stay behind the same opt-in
workbench gate (``LAURELIN_FEDERATION_WORKBENCH``) and are admin-only to
register.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from laurelin.core import federation
from laurelin.core.dialects import CLICKHOUSE

SOURCE_TYPES = ("parquet",)

# Re-exported unchanged: the password|secret|token|key regex applies to a
# ClickHouse DSN as directly as to a Postgres one, and a second redactor is a
# second thing to forget to update.
redacted_source = federation.redacted_source


class ClickHouseError(federation.FederationError):
    """A ClickHouse source could not be reached or scanned.

    Subclasses ``FederationError`` so the API layer's existing 502 handling
    covers it without a second except clause to keep in sync.
    """


def available() -> bool:
    """Whether chdb is installed.

    Mirrors ``iceberg.available()`` so the feature and its tests read
    availability off one function and cannot disagree about it.
    """
    try:
        import chdb  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install is unavailable too
        return False
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_source(source: dict[str, Any]) -> dict[str, Any]:
    """Reject a malformed ClickHouse source at registration time."""
    type_ = source.get("type")
    if type_ not in SOURCE_TYPES:
        raise ValueError(
            f"Unknown ClickHouse source type {type_!r}: expected one of "
            + ", ".join(SOURCE_TYPES)
            + " (server-backed sources are not implemented; see "
            "laurelin/core/clickhouse.py)"
        )
    path = str(source.get("path", "")).strip()
    if not path:
        raise ValueError("parquet source needs a 'path'")
    if path.startswith("file://"):
        raise ValueError("file:// paths are not allowed; use a managed dataset")
    return {"type": type_, "path": path}


# ---------------------------------------------------------------------------
# Scan rendering
# ---------------------------------------------------------------------------

def scan_expression(source: dict[str, Any]) -> str:
    """A table reference usable as ``FROM <expr>``.

    The path is escaped by the dialect's literal escaper — the same one that
    handles policy values — because ClickHouse offers no binding channel that
    preserves bytes. Returns a plain string, not the
    ``(expr, params, extensions)`` triple federation uses; there is nothing to
    bind and no extension to load.
    """
    type_ = source["type"]
    if type_ != "parquet":
        raise ValueError(f"Unknown ClickHouse source type {type_!r}")
    return f"file({CLICKHOUSE.literal(str(source['path']))}, Parquet)"


def run(sql: str) -> pa.Table:
    """Execute one statement and return an Arrow table.

    Stateless ``chdb.query`` only — never ``chdb.session.Session``. A Session
    hijacks the module-level connection chdb.query uses, so two callers would
    share state and one governed read could observe a table another created.
    The measured 11x per-query speedup (1.4ms vs 15.2ms) is not worth a
    governance suite that can pass on someone else's leftovers.
    """
    import chdb

    try:
        result = chdb.query(sql, "Arrow")
        payload = result.bytes() if hasattr(result, "bytes") else bytes(result)
    except Exception as exc:  # noqa: BLE001
        # chdb raises a bare RuntimeError, not a chdb-specific class, so
        # `except chdb.ChdbError` would catch nothing at all.
        raise ClickHouseError(f"ClickHouse query failed: {exc}") from exc
    if not payload:
        return pa.table({})
    try:
        with pa.ipc.open_file(pa.BufferReader(payload)) as reader:
            return reader.read_all()
    except pa.ArrowInvalid:
        with pa.ipc.open_stream(pa.BufferReader(payload)) as reader:
            return reader.read_all()


def schema_of(source: dict[str, Any]) -> pa.Schema:
    """Arrow schema of the source, read without fetching rows.

    ``SELECT ... LIMIT 0`` rather than ``DESCRIBE TABLE`` because the renderer
    needs Arrow *types*, not ClickHouse type names: whether a row filter or a
    hash mask means the same thing here as on the Arrow path is a fact about
    the Arrow type (see ``SqlDialect.row_key_matches_arrow``), and translating
    ``Nullable(DateTime64(6, 'UTC'))`` back into one by hand is a second place
    to get it wrong. Parquet answers this from the footer, so it stays a
    metadata read.

    Raises rather than returning an empty schema: an empty column set would let
    the policy renderer fall back to an unmasked projection, so discovery
    failing has to be a refusal (see ``SqlPolicy.render``).
    """
    scan = scan_expression(source)
    schema = run(f"SELECT * FROM {scan} LIMIT 0").schema
    # An empty name is not addressable in any dialect, so it cannot be masked
    # and must not be projected. ClickHouse drops such columns itself; this
    # keeps the guarantee explicit rather than borrowed.
    schema = pa.schema([f for f in schema if f.name])
    if not schema.names:
        raise ClickHouseError(
            f"ClickHouse source {source.get('path')!r} reported no columns. "
            "Refusing to register or read it: an unknown column set cannot be "
            "masked."
        )
    return schema


def columns_of(source: dict[str, Any]) -> list[str]:
    """Column names of the source, read without fetching rows."""
    return list(schema_of(source).names)
