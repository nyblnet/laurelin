"""Apache Iceberg as a dataset backend.

Laurelin's own format is already open — a version is a manifest of plain
Parquet parts, and walking away costs `cp -r`. Iceberg buys something that
format can't: **other engines read the table without asking Laurelin**. Spark,
Trino, Snowflake and DuckDB all speak Iceberg, so a dataset written here is a
table those tools open directly, with the snapshot history intact.

Two decisions worth stating.

*The catalog is the database Laurelin already runs.* pyiceberg's ``SqlCatalog``
points at the same SQLite file or Postgres URL as everything else, so adopting
Iceberg adds no service to operate — no REST catalog, no Hive metastore. The
roadmap said "a REST catalog we host"; hosting one turned out to be
unnecessary, which is a better answer than building it.

*Reads go through DuckDB's ``iceberg_scan``, not pyiceberg.* That is the same
path federated datasets already take, which means the SQL policy renderer
(``PolicyPlan.to_sql``) applies unchanged — row filters and column masks reach
Iceberg tables without a second implementation of what a policy means. Writes
are the only genuinely new code.

What this does *not* do yet, stated plainly because the Iceberg name implies
all of it: no branches or tags, no schema evolution, no hidden partitioning,
no row-level deletes, no compaction of small files. Those are the reasons to
reach for Iceberg beyond interoperability, and they are not here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa

# Table identifiers are `<namespace>.<name>`; a dataset name is already
# validated as ^[a-z][a-z0-9_]*$, but the namespace comes from config.
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

DEFAULT_NAMESPACE = "laurelin"


class IcebergUnavailable(RuntimeError):
    """pyiceberg isn't installed. Raised rather than degraded, because
    silently writing plain Parquet when someone asked for Iceberg would be a
    lie about where their data is."""


def available() -> bool:
    try:
        import pyiceberg.catalog.sql  # noqa: F401
    except Exception:
        return False
    return True


def warehouse_uri(workspace) -> str:
    """Where Iceberg table data lives.

    Defaults inside the workspace so embedded mode works with no
    configuration; point ``LAURELIN_ICEBERG_WAREHOUSE`` at ``s3://…`` for a
    warehouse other engines can reach.
    """
    configured = os.environ.get("LAURELIN_ICEBERG_WAREHOUSE")
    if configured:
        return configured
    return (Path(workspace.root) / "iceberg").absolute().as_uri()


def catalog_uri(workspace) -> str:
    """The catalog database — the one Laurelin already has.

    SQLAlchemy is what pyiceberg speaks, so a Postgres control plane becomes a
    shared Iceberg catalog across replicas for free, and embedded mode gets a
    SQLite catalog beside the metadata it already keeps.
    """
    configured = os.environ.get("LAURELIN_ICEBERG_CATALOG")
    if configured:
        return configured
    database = os.environ.get("LAURELIN_DATABASE_URL", "")
    if database.startswith(("postgres://", "postgresql://")):
        # SQLAlchemy wants an explicit driver; psycopg 3 is what we depend on.
        return database.replace("postgresql://", "postgresql+psycopg://", 1).replace(
            "postgres://", "postgresql+psycopg://", 1
        )
    return f"sqlite:///{Path(workspace.root) / 'iceberg-catalog.db'}"


class IcebergTables:
    """Create, append to and read Iceberg tables for one workspace."""

    def __init__(self, workspace, namespace: str = DEFAULT_NAMESPACE):
        if not available():  # pragma: no cover - exercised via skipif in tests
            raise IcebergUnavailable(
                "Iceberg support needs pyiceberg: pip install 'laurelin[iceberg]'"
            )
        if not _NAMESPACE_RE.match(namespace):
            raise ValueError(f"Invalid Iceberg namespace {namespace!r}")
        self.workspace = workspace
        self.namespace = namespace
        self._catalog = None

    @property
    def catalog(self):
        if self._catalog is None:
            from pyiceberg.catalog.sql import SqlCatalog

            self._catalog = SqlCatalog(
                "laurelin",
                uri=catalog_uri(self.workspace),
                warehouse=warehouse_uri(self.workspace),
            )
            # A local warehouse must exist before the first write; an object
            # store creates prefixes implicitly.
            uri = warehouse_uri(self.workspace)
            if uri.startswith("file://"):
                Path(uri[len("file://"):]).mkdir(parents=True, exist_ok=True)
            try:
                self._catalog.create_namespace(self.namespace)
            except Exception:
                pass  # already exists; pyiceberg has no create-if-not-exists
        return self._catalog

    def _identifier(self, name: str) -> str:
        return f"{self.namespace}.{name}"

    def exists(self, name: str) -> bool:
        from pyiceberg.exceptions import NoSuchTableError

        try:
            self.catalog.load_table(self._identifier(name))
        except NoSuchTableError:
            return False
        return True

    def write(self, name: str, table: pa.Table, mode: str = "replace") -> dict:
        """Write a table, returning ``{metadata_location, snapshot_id, rows}``.

        ``mode="append"`` adds a snapshot on top of the existing data; the
        default replaces the table's contents. Either way the previous
        snapshots remain, which is what makes the history readable.
        """
        if mode not in ("replace", "append"):
            raise ValueError(f"Unknown mode {mode!r}: expected 'replace' or 'append'")
        from pyiceberg.exceptions import NoSuchTableError

        identifier = self._identifier(name)
        try:
            tbl = self.catalog.load_table(identifier)
        except NoSuchTableError:
            tbl = self.catalog.create_table(identifier, schema=table.schema)

        if mode == "append":
            tbl.append(table)
        else:
            tbl.overwrite(table)
        tbl.refresh()
        return self.state(name)

    def state(self, name: str) -> dict:
        tbl = self.catalog.load_table(self._identifier(name))
        snapshot = tbl.current_snapshot()
        return {
            "metadata_location": tbl.metadata_location,
            "snapshot_id": snapshot.snapshot_id if snapshot else None,
            "rows": tbl.scan().to_arrow().num_rows,
        }

    def snapshots(self, name: str) -> list[dict]:
        """Every snapshot, oldest first — the table's own version history."""
        tbl = self.catalog.load_table(self._identifier(name))
        return [
            {
                "snapshot_id": s.snapshot_id,
                "timestamp_ms": s.timestamp_ms,
                "operation": (s.summary.operation.value if s.summary else None),
            }
            for s in tbl.metadata.snapshots
        ]

    def read(self, name: str, snapshot_id: Optional[int] = None) -> pa.Table:
        """Read the table, optionally as of a past snapshot."""
        tbl = self.catalog.load_table(self._identifier(name))
        scan = tbl.scan(snapshot_id=snapshot_id) if snapshot_id else tbl.scan()
        return scan.to_arrow()

    def metadata_location(self, name: str) -> str:
        return self.catalog.load_table(self._identifier(name)).metadata_location

    def drop(self, name: str) -> None:
        from pyiceberg.exceptions import NoSuchTableError

        try:
            self.catalog.drop_table(self._identifier(name))
        except NoSuchTableError:
            pass


def source_for(metadata_location: str) -> dict[str, Any]:
    """The federation source describing an Iceberg table.

    Reusing the federated scan rendering is the point: `iceberg_scan(?)` is
    already how DuckDB reads Iceberg here, so a Laurelin-owned Iceberg table
    and a foreign one are read by the same code, and the SQL policy renderer
    covers both without knowing the difference.
    """
    return {"type": "iceberg", "path": _as_path(metadata_location)}


def _as_path(location: str) -> str:
    """DuckDB wants a filesystem path or an object-store URL, not file://."""
    return location[len("file://"):] if location.startswith("file://") else location
