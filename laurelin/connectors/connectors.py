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
- ``file`` — read CSV/Parquet from a server-side path or glob (for data
  landed on a mounted volume). Config: ``{"path": "/mnt/land/*.parquet"}``,
  optional ``format``.

Source management is admin-only at the API layer: postgres/http configs can
embed credentials, and file reads the server's filesystem. Secret-bearing
config values are redacted in every API response (see ``redacted_config``).
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from laurelin.catalog import DatasetCatalog
from laurelin.core.db import MetadataStore
from laurelin.core.models import DatasetVersionInfo, SourceInfo

CONNECTOR_TYPES = ("postgres", "http", "file")

_DEFAULT_BATCH_SIZE = 50_000
_MAX_BATCH_SIZE = 1_000_000
# PostgreSQL identifier or schema-qualified identifier, e.g. public.orders.
_PG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_SECRET_KEY_RE = re.compile(r"password|secret|token|authorization|api_?key", re.I)


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


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    """Config safe to return from the API: secret-named keys and URL passwords
    replaced with ``*****``."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        if _SECRET_KEY_RE.search(key):
            out[key] = "*****"
        elif key == "headers" and isinstance(value, dict):
            out[key] = {
                k: ("*****" if _SECRET_KEY_RE.search(k) else v)
                for k, v in value.items()
            }
        elif key == "url" and isinstance(value, str):
            out[key] = _redact_url_password(value)
        else:
            out[key] = value
    return out


def _redact_url_password(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host += f":{parts.port}"
    netloc = f"{parts.username}:*****@{host}" if parts.username else f":*****@{host}"
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


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


def _http_format(config: dict[str, Any]) -> str:
    fmt = config.get("format")
    if not fmt:
        path = urllib.parse.urlparse(str(config.get("url", ""))).path
        fmt = {".csv": "csv", ".parquet": "parquet", ".pq": "parquet"}.get(
            Path(path).suffix.lower()
        )
    if fmt not in ("csv", "parquet"):
        raise ValueError(
            "Cannot determine file format from the URL; set format to 'csv' or 'parquet'"
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
        fmt = {".csv": "csv", ".parquet": "parquet", ".pq": "parquet"}.get(suffix)
    if fmt not in ("csv", "parquet"):
        raise ValueError(
            "Cannot determine file format from the path; set format to 'csv' or 'parquet'"
        )
    return fmt


def _read_local(path_or_glob: str, fmt: str) -> pa.Table:
    con = duckdb.connect()
    try:
        fn = "read_csv_auto" if fmt == "csv" else "read_parquet"
        table = con.execute(f"SELECT * FROM {fn}(?)", [path_or_glob]).arrow()
    finally:
        con.close()
    if isinstance(table, pa.RecordBatchReader):
        table = table.read_all()
    return table


def _pull_file(config: dict[str, Any]) -> Iterator[pa.Table]:
    yield _read_local(str(config["path"]), _file_format(config))


_PULLERS = {"postgres": _pull_postgres, "http": _pull_http, "file": _pull_file}


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
    new data, in both directions.
    """
    mode = source.config.get("mode", "replace")
    cursor_column = source.config.get("cursor_column")
    try:
        if source.type == "postgres":
            chunks = _pull_postgres(source.config, since=source.cursor_value)
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
    except Exception as exc:
        store.record_source_sync(
            source.name, "failed", error=f"{type(exc).__name__}: {exc}"
        )
        store.log_audit(
            "source_sync_failed",
            {"source": source.name, "dataset": source.dataset, "error": str(exc)[:500]},
            actor=actor,
        )
        raise
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
