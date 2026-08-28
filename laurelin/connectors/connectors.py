"""Data connectors: pull external data into datasets.

A *source* is a stored definition (``SourceInfo`` in the metadata store) that
names a connector type plus its config, and targets one dataset. Syncing a
source produces a normal immutable dataset version (``source="sync:<type>"``),
so everything downstream — transforms, lineage, markings, ACLs — treats
connector data exactly like uploaded data.

Connector types:

- ``postgres`` — pull a table or query from a PostgreSQL database via a
  server-side cursor, streamed to Parquet in batches (never fully in memory).
  Config: ``{"url": "postgresql://...", "table": "schema.name"}`` or
  ``{"url": ..., "query": "SELECT ..."}``, optional ``batch_size``.
- ``http`` — fetch a CSV or Parquet file over http(s).
  Config: ``{"url": ...}``, optional ``format`` ("csv"/"parquet", inferred
  from the URL path when omitted) and ``headers`` (e.g. an Authorization
  header for an API export).
- ``file`` — read CSV/Parquet/JSON/JSONL/Avro from a server-side path or glob
  (for data landed on a mounted volume). Config:
  ``{"path": "/mnt/land/*.parquet"}``, optional ``format``.
- ``object_store`` — copy objects out of an S3/GCS bucket into a managed
  dataset (ingestion, not federation: the bytes are pulled in and versioned).
  Config: ``{"uri": "s3://bucket/prefix/*.parquet"}`` (a key, prefix or glob),
  optional ``provider`` ("s3" default, or "gcs" via HMAC interop),
  ``format`` (csv/parquet/json/jsonl/avro, inferred from the uri suffix when
  omitted), ``endpoint_url`` (S3-compatible stores such as MinIO/R2; implies
  path-style addressing, SSL from the endpoint scheme), ``region``, and the
  credential pair ``access_key_id``/``secret_access_key`` (both or neither;
  absent means an anonymous/public bucket).

Source management is admin-only at the API layer: postgres/http/object_store
configs can embed credentials, and file reads the server's filesystem.
Secret-bearing config values are redacted in every API response (see
``redacted_config``).

**Network scope (the SSRF surface, stated).** ``endpoint_url`` and ``uri`` are
admin-authored (``PUT /sources`` is admin-only). The *trigger* is
lower-privileged — an editor, a viewer holding a ``can_edit`` dataset grant
(the sync route's only gate is ``_require_dataset_edit``), and the scheduler
unattended can all cause a fetch to the admin-configured endpoint — but none
of them can *supply* the endpoint. The surface is therefore "an admin can
point the server's sync path at an arbitrary URL", the same trust already
extended for postgres/http sources. httpfs is enabled on exactly one
connection: the fresh, per-sync DuckDB connection inside
``_pull_object_store``, hardened in the ``federation.connect`` order
(extensions → secret → ``disabled_filesystems`` → ``lock_configuration``) and
closed in a ``finally``. That lockdown disables *both* ``LocalFileSystem`` and
``HTTPFileSystem``: s3:// reads use DuckDB's S3FileSystem (unaffected), so the
network-enabled connection cannot reach an arbitrary http(s) host (incl. cloud
metadata at 169.254.169.254) even if a non-s3 target somehow reached the
reader. The ingest size is capped at ``LAURELIN_MAX_UPLOAD_MB`` (default
1024 MB, same ceiling as the http puller) so an editor-triggered sync of a huge
object cannot mint an unbounded managed dataset. Build/query/ontology
connections all set
``enable_external_access=false``, which blocks ``s3://`` even with a valid
secret on the same connection (measured), and DuckDB secrets are
per-database-instance and temporary, so no other connection in the process
can use the sync's credential. Residual, measured: a scoped secret is
credential *selection*, not egress control — an out-of-scope ``s3://`` URL
falls through to unauthenticated defaults and the request leaves for
``s3.amazonaws.com``. Bounded because only server-generated SQL over the same
admin's ``uri`` ever runs on that connection.

**Append-mode caveats for object_store (documented, not fixed).** The cursor
is the max object ``last_modified`` seen, compared strictly-``>`` — an object
landing in the same instant as a sync's max is skipped, the same semantics as
the postgres ``cursor_column > %s`` high-water mark (AWS lists
whole-second mtimes; MinIO milliseconds). And an *overwritten* object (same
key, newer mtime) re-enters an append sync and duplicates its rows:
replace-in-place buckets must use ``mode="replace"``.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

import duckdb
import pyarrow as pa

from laurelin.catalog import DatasetCatalog
from laurelin.core import metrics, redaction
from laurelin.core.db import MetadataStore
from laurelin.core.failure import (
    Failure,
    FailureCode,
    Phase,
    connect_failure,
    driver_of,
)
from laurelin.core.models import DatasetVersionInfo, Role, SourceInfo

log = logging.getLogger("laurelin.connectors")

CONNECTOR_TYPES = ("postgres", "http", "file", "object_store")

_DEFAULT_BATCH_SIZE = 50_000
_MAX_BATCH_SIZE = 1_000_000
# PostgreSQL identifier or schema-qualified identifier, e.g. public.orders.
_PG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


# ---------------------------------------------------------------------------
# Validation & redaction
# ---------------------------------------------------------------------------

def validate_source(type_: str, config: dict[str, Any]) -> None:
    """Reject a malformed source definition up front (create time), so a bad
    config fails loudly at PUT rather than at the first sync."""
    if type_ not in CONNECTOR_TYPES:
        raise ValueError(
            f"Unknown source type {type_!r}: expected one of {', '.join(CONNECTOR_TYPES)}"
        )
    mode = config.get("mode", "replace")
    if mode not in ("replace", "append"):
        raise ValueError("mode must be 'replace' or 'append'")
    cursor = config.get("cursor_column")
    if cursor is not None:
        if mode != "append":
            raise ValueError("cursor_column only applies to mode='append'")
        if type_ != "postgres":
            raise ValueError("cursor_column is only supported by postgres sources")
        if not _PG_IDENT_RE.match(str(cursor)) or "." in str(cursor):
            raise ValueError(f"Invalid cursor_column {cursor!r}: expected an identifier")

    if type_ == "postgres":
        url = config.get("url", "")
        if not str(url).startswith(("postgresql://", "postgres://")):
            raise ValueError("postgres source needs a postgresql:// url")
        table, query = config.get("table"), config.get("query")
        if bool(table) == bool(query):
            raise ValueError("postgres source needs exactly one of 'table' or 'query'")
        if table and not _PG_IDENT_RE.match(str(table)):
            raise ValueError(
                f"Invalid table {table!r}: expected identifier or schema.identifier"
            )
        if cursor and query:
            raise ValueError(
                "cursor_column requires 'table' (the cursor predicate is added "
                "to the generated query); embed your own WHERE clause instead"
            )
        batch = config.get("batch_size", _DEFAULT_BATCH_SIZE)
        if not isinstance(batch, int) or not (1 <= batch <= _MAX_BATCH_SIZE):
            raise ValueError(f"batch_size must be an int in [1, {_MAX_BATCH_SIZE}]")
    elif type_ == "http":
        scheme = urllib.parse.urlparse(str(config.get("url", ""))).scheme
        if scheme not in ("http", "https"):
            raise ValueError("http source needs an http:// or https:// url")
        _http_format(config)  # raises if format is absent and not inferrable
        headers = config.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("headers must be a string-to-string object")
    elif type_ == "file":
        if not str(config.get("path", "")).strip():
            raise ValueError("file source needs a 'path' (server-side file or glob)")
        _file_format(config)
    elif type_ == "object_store":
        # Config values (uri, endpoint_url, keys) never appear in these
        # messages — the 400 path is safe_detail, but the R1 rule stands:
        # never put config values in exception text.
        provider = config.get("provider", "s3")
        if provider == "azure":
            raise ValueError(
                "provider 'azure' is not supported yet; use federation for "
                "in-place reads"
            )
        if provider not in ("s3", "gcs"):
            raise ValueError("provider must be 's3' or 'gcs'")
        uri = str(config.get("uri", ""))
        scheme = "gs://" if provider == "gcs" else "s3://"
        if not uri.startswith(scheme):
            raise ValueError(
                f"object_store source needs a {scheme} uri for provider "
                f"{provider!r} (a key, prefix or glob)"
            )
        if not urllib.parse.urlparse(uri).netloc:
            raise ValueError("object_store uri names no bucket")
        if bool(config.get("access_key_id")) != bool(config.get("secret_access_key")):
            raise ValueError(
                "access_key_id and secret_access_key must be set together "
                "(both for a credentialed bucket, neither for a public one)"
            )
        endpoint = config.get("endpoint_url")
        if endpoint is not None:
            if provider != "s3":
                raise ValueError("endpoint_url only applies to provider 's3'")
            if urllib.parse.urlparse(str(endpoint)).scheme not in ("http", "https"):
                raise ValueError("endpoint_url must be an http:// or https:// URL")
        _object_store_format(config)


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    """Config safe to return from the API.

    Two things changed here, both because the previous version walked the top
    level plus ``headers`` and trusted key names for the rest:

    * ``{"auth": {"password": "SEKRET"}}`` came back verbatim — nothing below
      the top level was redacted at all. Nested values are now withheld, names
      kept.
    * ``{"headers": {"X-Api-Key": "SEKRET"}}`` came back verbatim, because
      ``api_?key`` does not match ``Api-Key``. Header *values* are now withheld
      wholesale rather than by name, which also withholds an innocent
      ``Accept: text/csv``. That is the trade: a header name list is a list of
      the names we happened to think of, and this one was already short by at
      least ``X-Api-Key``, ``Cookie`` and ``Proxy-Authorization``.

    ``urlsplit`` is gone too — it ends the authority at the first '/', so a
    password containing '/' survived it. See ``core/redaction.py``.
    """
    return redaction.redact_mapping(config)


# ---------------------------------------------------------------------------
# Connector implementations — each yields Arrow tables
# ---------------------------------------------------------------------------

def _pull_postgres(config: dict[str, Any], since: Optional[str] = None) -> Iterator[pa.Table]:
    import psycopg

    params: list[Any] = []
    query = config.get("query")
    if not query:
        # validate_source vetted the identifiers; quote each part.
        parts = str(config["table"]).split(".")
        query = "SELECT * FROM " + ".".join(f'"{p}"' for p in parts)
        cursor_column = config.get("cursor_column")
        if cursor_column and since is not None:
            # Only rows newer than the last high-water mark. The column name is
            # a validated identifier; the value is bound as a parameter.
            query += f' WHERE "{cursor_column}" > %s'
            params.append(since)
            query += f' ORDER BY "{cursor_column}"'
    batch_size = int(config.get("batch_size", _DEFAULT_BATCH_SIZE))

    with psycopg.connect(str(config["url"])) as conn:
        # A named (server-side) cursor streams rows instead of buffering the
        # full result client-side.
        with conn.cursor(name="laurelin_sync") as cur:
            cur.itersize = batch_size
            cur.execute(query, params or None)  # type: ignore[arg-type]
            columns = [d.name for d in cur.description or []]
            got_rows = False
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                got_rows = True
                yield pa.Table.from_pylist(
                    [dict(zip(columns, row)) for row in rows]
                )
            if not got_rows:
                # Empty result: still produce the column layout.
                yield pa.table({c: pa.array([], type=pa.string()) for c in columns})


# All the formats the DuckDB-backed readers speak. json and jsonl are two
# names for the same reader (read_json auto-detects array vs newline-delimited,
# measured); both survive because the *name* is the admin's declaration when
# the suffix lies.
_FORMATS = ("csv", "parquet", "json", "jsonl", "avro")
_SUFFIX_FORMATS = {
    ".csv": "csv", ".parquet": "parquet", ".pq": "parquet",
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".avro": "avro",
}
_READERS = {
    "csv": "read_csv_auto", "parquet": "read_parquet",
    "json": "read_json", "jsonl": "read_json", "avro": "read_avro",
}


def _http_format(config: dict[str, Any]) -> str:
    fmt = config.get("format")
    if not fmt:
        path = urllib.parse.urlparse(str(config.get("url", ""))).path
        fmt = _SUFFIX_FORMATS.get(Path(path).suffix.lower())
    if fmt not in _FORMATS:
        raise ValueError(
            "Cannot determine file format from the URL; set format to one of "
            + ", ".join(_FORMATS)
        )
    return fmt


def _pull_http(config: dict[str, Any]) -> Iterator[pa.Table]:
    fmt = _http_format(config)
    url = str(config["url"])
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("http source needs an http:// or https:// url")
    max_bytes = int(os.environ.get("LAURELIN_MAX_UPLOAD_MB", "1024")) * 1024 * 1024

    req = urllib.request.Request(url, headers=dict(config.get("headers") or {}))
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 — scheme vetted above
                written = 0
                while chunk := resp.read(1 << 20):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(
                            f"Download exceeds {max_bytes // (1024 * 1024)} MB limit "
                            "(set LAURELIN_MAX_UPLOAD_MB to raise it)"
                        )
                    tmp.write(chunk)
        except Exception:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise
    try:
        yield _read_local(str(tmp_path), fmt)
    finally:
        tmp_path.unlink(missing_ok=True)


def _file_format(config: dict[str, Any]) -> str:
    fmt = config.get("format")
    if not fmt:
        suffix = Path(str(config.get("path", ""))).suffix.lower()
        fmt = _SUFFIX_FORMATS.get(suffix)
    if fmt not in _FORMATS:
        raise ValueError(
            "Cannot determine file format from the path; set format to one of "
            + ", ".join(_FORMATS)
        )
    return fmt


def _read_local(path_or_glob: str, fmt: str) -> pa.Table:
    con = duckdb.connect()
    try:
        # json and avro autoload on an unrestricted local connection (they are
        # installed; read_avro's autoload needs local-FS access, which this
        # connection has — the object_store puller is the one that must load
        # them before locking down).
        table = con.execute(
            f"SELECT * FROM {_READERS[fmt]}(?)", [path_or_glob]
        ).arrow()
    finally:
        con.close()
    if isinstance(table, pa.RecordBatchReader):
        table = table.read_all()
    return table


def _pull_file(config: dict[str, Any]) -> Iterator[pa.Table]:
    yield _read_local(str(config["path"]), _file_format(config))


def _object_store_format(config: dict[str, Any]) -> str:
    fmt = config.get("format")
    if not fmt:
        path = urllib.parse.urlparse(str(config.get("uri", ""))).path
        fmt = _SUFFIX_FORMATS.get(Path(path).suffix.lower())
    if fmt not in _FORMATS:
        raise ValueError(
            "Cannot determine file format from the uri; set format to one of "
            + ", ".join(_FORMATS)
        )
    return fmt


def _sql_str(value: Any) -> str:
    """A SQL single-quoted string literal. CREATE SECRET takes no bound
    parameters, so credential values are embedded as literals with quotes
    doubled. The statement never appears in any error Laurelin authors, and
    ``redaction._SQL_SECRET_STMT_RE`` treats any CREATE SECRET text as a
    credential if it ever surfaces anyway."""
    return "'" + str(value).replace("'", "''") + "'"


def _create_sync_secret(con: "duckdb.DuckDBPyConnection", config: dict[str, Any]) -> None:
    """One temporary, connection-scoped DuckDB secret for this sync.

    Temporary (the DuckDB default — never PERSISTENT) so it dies with the
    connection, and SCOPEd to the uri's bucket so the credential cannot be
    presented to any other bucket on this connection. Scope is credential
    *selection*, not egress control — see the module docstring. No ``AWS_*``
    env vars are ever exported.
    """
    provider = config.get("provider", "s3")
    bucket = urllib.parse.urlparse(str(config["uri"])).netloc
    parts = ["TYPE gcs" if provider == "gcs" else "TYPE s3"]
    key_id = config.get("access_key_id")
    secret = config.get("secret_access_key")
    if key_id and secret:
        parts += [f"KEY_ID {_sql_str(key_id)}", f"SECRET {_sql_str(secret)}"]
    endpoint = config.get("endpoint_url")
    if provider == "s3" and endpoint:
        # An S3-compatible store (MinIO, R2): path-style addressing, SSL from
        # the endpoint scheme — the convention, measured against MinIO. No
        # endpoint means DuckDB's defaults (vhost style, SSL) for real AWS.
        parsed = urllib.parse.urlparse(str(endpoint))
        parts += [
            f"ENDPOINT {_sql_str(parsed.netloc)}",
            "URL_STYLE 'path'",
            f"USE_SSL {'true' if parsed.scheme == 'https' else 'false'}",
        ]
    if config.get("region"):
        parts.append(f"REGION {_sql_str(config['region'])}")
    scheme = "gs" if provider == "gcs" else "s3"
    parts.append(f"SCOPE {_sql_str(f'{scheme}://{bucket}')}")
    con.execute(f"CREATE SECRET laurelin_sync ({', '.join(parts)})")


def _hardened_sync_connection(config: dict[str, Any]) -> "duckdb.DuckDBPyConnection":
    """A fresh DuckDB connection prepared for object-store ingestion, hardened
    in the ``federation.connect`` order.

    The order is load-bearing twice: extensions load first because loading needs
    an unlocked config *and* because ``read_avro``'s autoinstall needs the local
    filesystem (measured: loading it after ``disabled_filesystems`` fails with
    "LocalFileSystem has been disabled"); then the secret, then the lockdown,
    then ``lock_configuration`` so nothing later on the connection can undo it.

    The lockdown disables BOTH ``LocalFileSystem`` and ``HTTPFileSystem``.
    s3:// reads go through DuckDB's S3FileSystem (the secret's ENDPOINT), which
    is unaffected — verified against MinIO that read_csv/read_blob/glob over
    s3:// still return rows with HTTPFileSystem off. Leaving HTTPFileSystem on
    would let this network-enabled connection reach an arbitrary http(s) host
    (incl. 169.254.169.254 cloud metadata) if a non-s3 target ever reached the
    reader. ``validate_source`` already forces the uri to s3://gs://, so this is
    defense-in-depth closing that gap at the connection, not the only guard.
    """
    con = duckdb.connect()
    for ext in ("httpfs", "json", "avro"):
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")
    _create_sync_secret(con, config)
    con.execute("SET disabled_filesystems='LocalFileSystem,HTTPFileSystem'")
    con.execute("SET lock_configuration=true")
    return con


def _pull_object_store(
    config: dict[str, Any],
    since: Optional[str] = None,
    cursor_box: Optional[dict[str, Optional[str]]] = None,
) -> Iterator[pa.Table]:
    """Pull bucket objects through a fresh, hardened DuckDB connection.

    ``cursor_box`` (mode="append") switches to the implicit object cursor:
    list object metadata via ``read_blob`` (a ListObjectsV2, no content
    fetch), keep only objects with ``last_modified > since``, and report the
    new high-water mark through the box — filled as the generator runs, read
    by the caller after the write consumed it.
    """
    fmt = _object_store_format(config)
    uri = str(config["uri"])
    reader = _READERS[fmt]
    con = _hardened_sync_connection(config)
    try:
        target: Any = uri
        if cursor_box is not None:
            # CAST to VARCHAR avoids DuckDB's pytz requirement on TIMESTAMPTZ
            # results, and the rendered offset keeps the string comparison
            # timezone-safe when parsed back.
            sql = (
                "SELECT filename, CAST(last_modified AS VARCHAR) AS lm "
                "FROM read_blob(?)"
            )
            params: list[Any] = [uri]
            if since:
                sql += " WHERE last_modified > CAST(? AS TIMESTAMPTZ)"
                params.append(since)
            sql += " ORDER BY last_modified"
            listing = con.execute(sql, params).fetchall()
            if not listing:
                return  # nothing new: an empty stream, which append no-ops on
            cursor_box["value"] = listing[-1][1]
            target = [row[0] for row in listing]
        else:
            # mode="replace": zero matching objects must be an error, not a
            # silent empty version — and a first-party one, not DuckDB's
            # opaque "no files found" surfacing as a 502. Metadata-only.
            hit = con.execute(
                "SELECT filename FROM read_blob(?) LIMIT 1", [uri]
            ).fetchall()
            if not hit:
                raise ValueError("Source matched no objects at its configured location")

        # Cap total ingested size the same way _pull_http caps its download:
        # an editor-triggered sync of a huge bucket object must not mint an
        # unbounded managed dataset (a resource-exhaustion asymmetry — the http
        # puller enforces this ceiling, the object puller did not). Measured on
        # decoded Arrow bytes as the stream runs, so columnar formats still
        # stream batch-by-batch and abort before the write balloons.
        max_bytes = int(os.environ.get("LAURELIN_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
        seen = 0

        def _guard(t: pa.Table) -> pa.Table:
            nonlocal seen
            seen += t.nbytes
            if seen > max_bytes:
                raise ValueError(
                    f"Ingest exceeds {max_bytes // (1024 * 1024)} MB limit "
                    "(set LAURELIN_MAX_UPLOAD_MB to raise it)"
                )
            return t

        table = con.execute(f"SELECT * FROM {reader}(?)", [target]).arrow()
        if isinstance(table, pa.RecordBatchReader):
            for batch in table:
                yield _guard(pa.Table.from_batches([batch]))
        else:
            yield _guard(table)
    finally:
        con.close()


_PULLERS = {
    "postgres": _pull_postgres,
    "http": _pull_http,
    "file": _pull_file,
    "object_store": _pull_object_store,
}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _max_cursor(table: pa.Table, column: str) -> Optional[str]:
    if column not in table.column_names or table.num_rows == 0:
        return None
    import pyarrow.compute as pc

    value = pc.max(table.column(column)).as_py()
    return None if value is None else str(value)


def sync_source(
    catalog: DatasetCatalog,
    store: MetadataStore,
    source: SourceInfo,
    actor: str = "",
) -> DatasetVersionInfo:
    """Run one sync: pull from the external system, write a new dataset
    version, and record the outcome on the source (also on failure).

    With ``mode="append"`` the pulled rows are appended (O(delta) I/O) instead
    of replacing the dataset. With a ``cursor_column`` as well, only rows above
    the stored high-water mark are pulled — so a recurring sync moves just the
    new data, in both directions. An ``object_store`` append sync has an
    *implicit* cursor (max object ``last_modified``; no ``cursor_column``):
    only objects newer than the stored mark are pulled, and a sync that finds
    nothing new mints no version.
    """
    _refuse_without_credentials(source)
    mode = source.config.get("mode", "replace")
    cursor_column = source.config.get("cursor_column")
    # The object cursor is implicit (max last_modified), not a column: the
    # puller fills this box as the write consumes its stream, mirroring how
    # `tracked` below fills `new_cursor` for a column cursor.
    object_cursor_box: dict[str, Optional[str]] = {"value": None}
    try:
        if source.type == "postgres":
            chunks = _pull_postgres(source.config, since=source.cursor_value)
        elif source.type == "object_store" and mode == "append":
            chunks = _pull_object_store(
                source.config, since=source.cursor_value,
                cursor_box=object_cursor_box,
            )
        else:
            chunks = _PULLERS[source.type](source.config)

        new_cursor: Optional[str] = None
        if cursor_column:
            # Track the high-water mark as chunks stream past, so an
            # incremental sync never re-reads what it has already ingested.
            def tracked(stream):
                nonlocal new_cursor
                for chunk in stream:
                    seen = _max_cursor(chunk, cursor_column)
                    if seen is not None and (new_cursor is None or seen > new_cursor):
                        new_cursor = seen
                    yield chunk

            chunks = tracked(chunks)

        writer = catalog.append_batches if mode == "append" else catalog.write_batches
        info = writer(source.dataset, chunks, source=f"sync:{source.type}")
        if new_cursor is None:
            new_cursor = object_cursor_box["value"]
    except Exception as exc:
        metrics.syncs.labels(type=source.type, status="failed").inc()
        # R1. This used to store `redact_driver_text(f"{type(exc).__name__}:
        # {exc}", secrets_in_config(config))` — a *redacted driver sentence*,
        # which is still a driver sentence, and round 3 walked around the
        # redactor twice (a libpq conninfo has no `://`; a password containing a
        # space is quoted back by psycopg in a message the config's secret list
        # does not match because the driver re-escaped it).
        #
        # What is stored now is a value Laurelin constructed. The driver's words
        # go to the log inside `Failure.from_exception` and nowhere else, and
        # the exception is re-raised for the caller.
        failure = _sync_failure(exc, source)
        store.record_source_sync(source.name, "failed", failure=failure)
        store.log_audit(
            "source_sync_failed",
            {"source": source.name, "dataset": source.dataset,
             "failure": failure.audit_projection()},
            actor=actor,
            # An editor owns sources and needs to see why a sync failed. Safe to
            # lower *because* the bag holds the failure's PROJECTION and not the
            # record: `as_dict()` stood here, and it carried `endpoint` —
            # rebuilt from the connector's ADMIN-authored `url`, which is the
            # one field `source_routes._public` exists to withhold from an
            # editor. `details` is an open dict, so `serialize.dump` cannot
            # reach inside it to fix that; the writer has to.
            min_read_role=Role.editor,
        )
        raise
    metrics.syncs.labels(type=source.type, status="succeeded").inc()
    metrics.sync_rows.inc(info.row_count)
    store.record_source_sync(
        source.name, "succeeded", version=info.version, rows=info.row_count,
        cursor_value=new_cursor,
    )
    store.log_audit(
        "source_synced",
        {
            "source": source.name,
            "dataset": source.dataset,
            "mode": mode,
            "version": info.version,
            "row_count": info.row_count,
            **({"cursor": new_cursor} if new_cursor is not None else {}),
        },
        actor=actor,
    )
    return info


def _refuse_without_credentials(source: SourceInfo) -> None:
    """A source whose endpoint the export withheld is not a broken connector.

    Without this the first sync after an import fails inside a driver, with
    whatever that driver says about ``url=None`` — which reads as a bug in
    Laurelin rather than as the re-supply step the manifest already listed.
    """
    from laurelin.export.manifest import NEEDS_CREDENTIALS_KEY, NeedsCredentials

    if source.config.get(NEEDS_CREDENTIALS_KEY):
        raise NeedsCredentials(
            f"Source {source.name!r} was imported without its endpoint. "
            "Re-supply it (PUT /api/v1/sources/{name} or Admin -> Sources) "
            "before syncing; the manifest's withheld list says what is missing."
        )


# Which library actually raised, per connector type. A closed map, because
# `Failure.driver` is a closed set — it names the library whose log line an
# operator should go read, and a value nobody put here is a value nobody
# constructed.
# `http` said "requests" and `_pull_http` uses `urllib.request`; `requests` is
# not imported on that path at all, so a stored failure sent an operator to
# a log that does not exist. Overridden per-exception by `driver_of` when
# the raising library is known.
_DRIVER_BY_TYPE = {
    "postgres": "psycopg", "http": "urllib", "file": "duckdb",
    "object_store": "duckdb",
}


def _sync_failure(exc: BaseException, source: SourceInfo) -> Failure:
    """One source sync's failure, as Laurelin records it.

    On the postgres path this goes through the connect-phase pre-flight, because
    psycopg gives ``sqlstate=None`` on **every** connect failure (measured on
    this tree against live Postgres: wrong password, space-in-password, unknown
    database, bad host and refused port are all `sqlstate=None`, and all are
    `OperationalError` except space-in-password, which is `ProgrammingError`).
    So the driver discriminates nothing at connect time and Laurelin does its
    own DNS lookup and TCP connect to find out which of "unresolvable",
    "unreachable", "timed out" and "rejected us" is true.
    """
    subject = f"source:{source.name}"
    # The connector type says which library we *meant* to use; the raising
    # class's module says which one actually did. Prefer the fact.
    driver = driver_of(exc, _DRIVER_BY_TYPE.get(source.type, ""))
    # `uri` is the object_store locator; the Failure's secret-awareness needs
    # to see it (driver is duckdb there, so no psycopg connect pre-flight).
    dsn = str(source.config.get("url") or source.config.get("uri") or "")
    if driver == "psycopg" and dsn:
        return connect_failure(
            exc, subject=subject, driver=driver, dsn=dsn, config=source.config
        )
    return Failure.from_exception(
        exc, phase=Phase.execute, subject=subject, driver=driver, dsn=dsn,
        config=source.config,
        code=None if driver in ("psycopg", "duckdb") else FailureCode.REMOTE_FAILED,
    )
