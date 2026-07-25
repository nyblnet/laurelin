"""Federated datasets — governed tables whose bytes Laurelin does not hold.

Most business data is medium-sized and belongs in a managed dataset (Parquet
Laurelin owns). Some isn't: a 5-billion-row event table already lives in
Iceberg or a warehouse, and importing it would be both wasteful and pointless.

A *federated* dataset registers such a table so that Laurelin governs it —
catalog entry, lineage, ACLs, classification markings, and use as a transform
input — while the data stays where it is and the scan happens in place, with
predicate pushdown handled by DuckDB's reader.

**Enforcement.** Row policies and column masks are compiled to SQL by
``PermissionService.sql_policy_fn`` and wrapped around the scan, so the same
policy means the same thing whether it filters a local Parquet file or a
remote Iceberg table. There is one policy decision; this module only renders
where it runs.

**Sandbox.** Reading remote data needs external access, which the workbench's
connection deliberately does not have. Federated scans therefore run on their
own connection, hardened as far as DuckDB allows: the local filesystem is
disabled outright and the configuration is locked so nothing can re-enable it.
The residual capability — reading object-store URLs the workspace's own
credentials can reach — is why exposing federated datasets to ad-hoc SQL is
opt-in (see ``workbench_enabled``).
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import duckdb

SOURCE_TYPES = ("parquet", "iceberg", "delta", "postgres")

# Remote URI schemes that need the httpfs extension.
_REMOTE_PREFIXES = ("s3://", "gs://", "gcs://", "r2://", "az://", "abfs://", "abfss://",
                    "http://", "https://")
_PG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_SECRET_KEY_RE = re.compile(r"password|secret|token|key", re.I)


class FederationError(RuntimeError):
    """A federated source could not be reached or scanned."""


def workbench_enabled() -> bool:
    """Whether federated datasets are exposed to ad-hoc SQL.

    Off by default: turning federation on should not silently widen what every
    viewer can read. Transforms can always use federated datasets, because
    their SQL is server-authored and runs on the hardened connection.
    """
    return os.environ.get("LAURELIN_FEDERATION_WORKBENCH") == "1"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_source(source: dict[str, Any]) -> None:
    """Reject a malformed federated source at registration time."""
    type_ = source.get("type")
    if type_ not in SOURCE_TYPES:
        raise ValueError(
            f"Unknown federated source type {type_!r}: expected one of "
            + ", ".join(SOURCE_TYPES)
        )
    if type_ == "postgres":
        if not str(source.get("url", "")).startswith(("postgresql://", "postgres://")):
            raise ValueError("postgres source needs a postgresql:// url")
        table = str(source.get("table", ""))
        if not _PG_IDENT_RE.match(table):
            raise ValueError(
                f"Invalid table {table!r}: expected identifier or schema.identifier"
            )
    else:
        path = str(source.get("path", "")).strip()
        if not path:
            raise ValueError(f"{type_} source needs a 'path'")
        if path.startswith("file://"):
            raise ValueError("file:// paths are not allowed; use a managed dataset")


def redacted_source(source: dict[str, Any]) -> dict[str, Any]:
    """Source config safe to return from the API."""
    out: dict[str, Any] = {}
    for key, value in source.items():
        if _SECRET_KEY_RE.search(key):
            out[key] = "*****"
        elif key == "url" and isinstance(value, str):
            out[key] = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:*****@", value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Scan rendering
# ---------------------------------------------------------------------------

def _needs_httpfs(path: str) -> bool:
    return str(path).startswith(_REMOTE_PREFIXES)


def scan_expression(source: dict[str, Any]) -> tuple[str, list, list[str]]:
    """``(sql_expression, params, required_extensions)`` for a source.

    The expression is a table reference usable as ``FROM <expr>``; every
    user-supplied value is a bound parameter, never interpolated.
    """
    type_ = source["type"]
    if type_ == "parquet":
        path = str(source["path"])
        return "read_parquet(?)", [path], ["httpfs"] if _needs_httpfs(path) else []
    if type_ == "iceberg":
        path = str(source["path"])
        exts = ["iceberg"] + (["httpfs"] if _needs_httpfs(path) else [])
        return "iceberg_scan(?)", [path], exts
    if type_ == "delta":
        path = str(source["path"])
        exts = ["delta"] + (["httpfs"] if _needs_httpfs(path) else [])
        return "delta_scan(?)", [path], exts
    if type_ == "postgres":
        parts = str(source["table"]).split(".")
        schema, table = (parts[0], parts[1]) if len(parts) == 2 else ("public", parts[0])
        return "postgres_scan(?, ?, ?)", [str(source["url"]), schema, table], [
            "postgres_scanner"
        ]
    raise ValueError(f"Unknown federated source type {type_!r}")


def is_local_source(source: dict[str, Any]) -> bool:
    """Whether reading this source requires the local filesystem."""
    path = str(source.get("path", ""))
    return source.get("type") != "postgres" and not path.startswith(_REMOTE_PREFIXES)


def connect(source: dict[str, Any]) -> duckdb.DuckDBPyConnection:
    """A DuckDB connection scoped to what this source actually needs.

    Only server-generated SQL ever runs here — the scan plus its compiled
    policy — because callers receive an Arrow table, not the connection. The
    hardening below is therefore defense in depth rather than the primary
    control, and it follows least privilege per source: a source in object
    storage gets **no local filesystem access at all**, while one that *is* a
    local path keeps only what it needs to read itself.

    Extensions load first (loading needs an unlocked config), then the
    restrictions go on and the configuration is locked so nothing can lift
    them mid-query.
    """
    _, _, extensions = scan_expression(source)
    con = duckdb.connect()
    try:
        for ext in extensions:
            try:
                con.execute(f"INSTALL {ext}")
                con.execute(f"LOAD {ext}")
            except Exception as exc:  # noqa: BLE001
                con.close()
                raise FederationError(
                    f"The {ext!r} DuckDB extension could not be loaded, which "
                    f"{source['type']} sources require: {exc}"
                ) from exc
        if not is_local_source(source):
            con.execute("SET disabled_filesystems='LocalFileSystem'")
        con.execute("SET lock_configuration=true")
    except FederationError:
        raise
    except Exception:
        con.close()
        raise
    return con


def columns_of(source: dict[str, Any], con: Optional[duckdb.DuckDBPyConnection] = None) -> list[str]:
    """Column names of the remote table, read without fetching rows."""
    owned = con is None
    con = con or connect(source)
    try:
        expr, params, _ = scan_expression(source)
        cur = con.execute(f"SELECT * FROM {expr} LIMIT 0", params)
        return [d[0] for d in (cur.description or [])]
    except duckdb.Error as exc:
        raise FederationError(f"Could not read the federated source: {exc}") from exc
    finally:
        if owned:
            con.close()
