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

Branches and schema evolution are here; a branch is a named pointer into the
snapshot history, so cutting one copies no data, and Iceberg tracks columns by
id rather than position, so adding one leaves every existing snapshot
readable.

Compaction is a whole-table rewrite into one new snapshot
(``DatasetCatalog._compact_iceberg``), not Iceberg's incremental
``rewrite_data_files``: it reads every row, so it costs the table, and it
reclaims *scan* cost rather than disk, because every earlier snapshot keeps its
own data files. That is the honest shape of what is implemented.

What this still does *not* do, stated plainly because the Iceberg name implies
all of it: no tags, no hidden partitioning, no row-level deletes, no expiry of
old snapshots (so nothing here ever frees storage), and merges are
**fast-forward only** — a three-way merge of two diverged histories needs a
row-level conflict policy, and guessing one would silently pick a winner
between two people's writes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa

from laurelin.core.fileperms import ensure_private_file, mkdir_private

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

            uri = catalog_uri(self.workspace)
            # SQLAlchemy creates a missing SQLite file at 0666 & ~umask, same
            # as sqlite3 did before laurelin/core/fileperms.py. This catalog is
            # not the metadata database, but it is a database Laurelin causes
            # to exist in the workspace root, and a warehouse URI recorded in
            # it can carry a credential. Pre-created 0600 for the same reason
            # and by the same route; a no-op once it exists and is private.
            if uri.startswith("sqlite:///"):
                ensure_private_file(uri[len("sqlite:///"):], what="Iceberg catalog")
            self._catalog = SqlCatalog(
                "laurelin",
                uri=uri,
                warehouse=warehouse_uri(self.workspace),
            )
            # A local warehouse must exist before the first write; an object
            # store creates prefixes implicitly.
            uri = warehouse_uri(self.workspace)
            if uri.startswith("file://"):
                # 0700, like data/ — a bare mkdir here was the whole exposure.
                # `core/config.py` makes data/, pipelines/ and ontology/
                # private precisely because the workspace root is only 0700
                # when Laurelin created it, and the documented Docker shape
                # (`RUN mkdir -p /data`, or any bind-mounted volume) leaves it
                # 0755. iceberg/ arrived later and never joined that list, so
                # under a pre-existing root the whole chain came out
                # root(0755)/iceberg(0755)/…/data(0755)/*.parquet(0644):
                # governed dataset Parquet readable by every local user, with
                # no row policy and no column masks. Measured through
                # POST /api/v1/datasets/{name}/iceberg at umask 022, on both a
                # SQLite and a PostgreSQL metadata store. pyiceberg creates the
                # tree below this at `0777 & ~umask` and its files at 0644; a
                # private parent is what contains them, exactly as for data/.
                mkdir_private(Path(uri[len("file://"):]))
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

    def write(self, name: str, table: pa.Table, mode: str = "replace",
              branch: str = "main") -> dict:
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
            tbl.append(table, branch=branch)
        else:
            tbl.overwrite(table, branch=branch)
        tbl.refresh()
        return self.state(name, branch=branch)

    def state(self, name: str, branch: str = "main") -> dict:
        tbl = self.catalog.load_table(self._identifier(name))
        snapshot_id = (
            self._ref_snapshot(tbl, branch) if branch != "main"
            else (tbl.current_snapshot().snapshot_id if tbl.current_snapshot() else None)
        )
        scan = tbl.scan(snapshot_id=snapshot_id) if snapshot_id else tbl.scan()
        return {
            "metadata_location": tbl.metadata_location,
            "snapshot_id": snapshot_id,
            "rows": scan.to_arrow().num_rows,
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

    def data_files(self, name: str, snapshot_id: Optional[int] = None) -> int:
        """How many data files a scan of this table would open.

        The number compaction exists to reduce, and therefore the number it
        has to report before and after — Laurelin's own ``version.files``
        describes a directory of Parquet parts and says nothing about an
        Iceberg table's layout, which is how compaction here once audited
        ``parts_before: 0`` while doing nothing at all.
        """
        tbl = self.catalog.load_table(self._identifier(name))
        scan = tbl.scan(snapshot_id=snapshot_id) if snapshot_id else tbl.scan()
        return sum(1 for _ in scan.plan_files())

    def read(self, name: str, snapshot_id: Optional[int] = None,
             branch: Optional[str] = None) -> pa.Table:
        """Read the table, optionally as of a past snapshot or a branch."""
        tbl = self.catalog.load_table(self._identifier(name))
        if branch and snapshot_id is None:
            snapshot_id = self._ref_snapshot(tbl, branch)
        scan = tbl.scan(snapshot_id=snapshot_id) if snapshot_id else tbl.scan()
        return scan.to_arrow()

    # -- branches ---------------------------------------------------------------

    @staticmethod
    def _ref_snapshot(tbl, ref: str) -> int:
        if ref not in tbl.metadata.refs:
            raise KeyError(f"No branch {ref!r} on this table")
        return tbl.metadata.refs[ref].snapshot_id

    def create_branch(self, name: str, branch: str,
                      snapshot_id: Optional[int] = None) -> dict:
        """Branch a dataset so work can happen without touching what readers see.

        A branch is a named pointer into the snapshot history, so creating one
        copies no data — it is the same cheap operation as a git branch, for
        the same reason.
        """
        if branch == "main":
            raise ValueError("'main' is the trunk, not a branch you create")
        if not _NAMESPACE_RE.match(branch):
            raise ValueError(
                f"Invalid branch name {branch!r}: lowercase letters, digits "
                "and underscores, starting with a letter"
            )
        tbl = self.catalog.load_table(self._identifier(name))
        if branch in tbl.metadata.refs:
            raise ValueError(f"Branch {branch!r} already exists")
        base = snapshot_id or self._ref_snapshot(tbl, "main")
        tbl.manage_snapshots().create_branch(
            snapshot_id=base, branch_name=branch
        ).commit()
        tbl.refresh()
        return {"branch": branch, "snapshot_id": base}

    def branches(self, name: str) -> list[dict]:
        tbl = self.catalog.load_table(self._identifier(name))
        return [
            {"branch": ref, "snapshot_id": meta.snapshot_id}
            for ref, meta in sorted(tbl.metadata.refs.items())
        ]

    def delete_branch(self, name: str, branch: str) -> None:
        if branch == "main":
            raise ValueError("Refusing to delete 'main'")
        tbl = self.catalog.load_table(self._identifier(name))
        tbl.manage_snapshots().remove_branch(branch_name=branch).commit()

    def merge_branch(self, name: str, branch: str) -> dict:
        """Fast-forward main to the branch's tip.

        A metadata swap, not a data rewrite — main's history keeps every
        snapshot it had, so a merge is as reversible as anything else here.

        Deliberately *only* fast-forward: a three-way merge of two diverged
        snapshot histories needs a row-level conflict policy, and guessing one
        would silently pick a winner between two people's writes.
        """
        tbl = self.catalog.load_table(self._identifier(name))
        tip = self._ref_snapshot(tbl, branch)
        main = self._ref_snapshot(tbl, "main")
        if tip == main:
            return {"branch": branch, "snapshot_id": main, "changed": False}
        if not self._descends_from(tbl, tip, main):
            raise ValueError(
                f"Branch {branch!r} has diverged from main — main has moved on "
                "since the branch was cut. Only fast-forward merges are "
                "supported; re-cut the branch from the current main."
            )
        tbl.manage_snapshots().set_current_snapshot(snapshot_id=tip).commit()
        tbl.refresh()
        return {"branch": branch, "snapshot_id": tip, "changed": True}

    @staticmethod
    def _descends_from(tbl, snapshot_id: int, ancestor_id: int) -> bool:
        """Whether ``ancestor_id`` is on ``snapshot_id``'s parent chain."""
        by_id = {s.snapshot_id: s for s in tbl.metadata.snapshots}
        current = by_id.get(snapshot_id)
        while current is not None:
            if current.snapshot_id == ancestor_id:
                return True
            current = by_id.get(current.parent_snapshot_id)
        return False

    # -- schema evolution --------------------------------------------------------

    def schema_names(self, name: str) -> list[str]:
        tbl = self.catalog.load_table(self._identifier(name))
        return [f.name for f in tbl.schema().fields]

    def evolve_schema(self, name: str, add: Optional[dict] = None,
                      drop: Optional[list[str]] = None,
                      rename: Optional[dict] = None) -> list[str]:
        """Apply a schema change. Returns the resulting column names.

        Iceberg tracks columns by id rather than position, so adding one
        leaves every existing snapshot readable — which is why additive
        changes are safe to make casually and destructive ones are not.
        """
        from pyiceberg.types import (
            BooleanType,
            DoubleType,
            LongType,
            StringType,
            TimestampType,
        )

        types = {
            "string": StringType(), "integer": LongType(), "long": LongType(),
            "float": DoubleType(), "double": DoubleType(),
            "boolean": BooleanType(), "timestamp": TimestampType(),
        }
        tbl = self.catalog.load_table(self._identifier(name))
        with tbl.update_schema() as update:
            for column, type_name in (add or {}).items():
                if type_name not in types:
                    raise ValueError(
                        f"Unknown column type {type_name!r}: expected one of "
                        f"{', '.join(sorted(types))}"
                    )
                # Added columns are optional: existing rows have no value for
                # them, and pretending otherwise would make old snapshots
                # unreadable against the new schema.
                update.add_column(column, types[type_name], required=False)
            for column in drop or []:
                update.delete_column(column)
            for old_name, new_name in (rename or {}).items():
                update.rename_column(old_name, new_name)
        tbl.refresh()
        return [f.name for f in tbl.schema().fields]

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
