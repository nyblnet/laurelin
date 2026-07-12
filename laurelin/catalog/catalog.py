"""DatasetCatalog: versioned Parquet dataset storage.

Data layout inside a workspace:

    data/<dataset>/v0001/data.parquet
    data/<dataset>/v0002/data.parquet
    ...

Versions are immutable. Each write lands in a fresh version directory via an
atomic-ish temp-dir + rename, and is recorded in the MetadataStore only after
the files are in place.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import ColumnSchema, DatasetInfo, DatasetVersionInfo

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid dataset name {name!r}: must match ^[a-z][a-z0-9_]*$"
        )


def _json_safe(value: Any) -> Any:
    """Convert a value to something JSON-serializable."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


class DatasetCatalog:
    """Versioned Parquet storage for one workspace."""

    def __init__(self, workspace: Workspace, store: MetadataStore):
        self.workspace = workspace
        self.store = store

    # -- datasets -------------------------------------------------------------

    def create_dataset(self, name: str, description: str = "") -> DatasetInfo:
        _validate_name(name)
        return self.store.upsert_dataset(name, description)

    # -- writing --------------------------------------------------------------

    def write(
        self,
        name: str,
        table: pa.Table,
        source: str = "upload",
        build_id: Optional[str] = None,
        description: str = "",
    ) -> DatasetVersionInfo:
        """Write a new immutable version of a dataset.

        The parquet file is written to a temp dir inside data/ and renamed to
        its final version directory before the version is recorded, so a crash
        mid-write never leaves a registered-but-missing version.
        """
        _validate_name(name)
        self.store.upsert_dataset(name, description)

        dataset_dir = self.workspace.data_dir / name
        dataset_dir.mkdir(parents=True, exist_ok=True)

        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=self.workspace.data_dir)
        )
        version = self.store.next_version(name)
        try:
            pq.write_table(table, tmp_dir / "data.parquet")
            # The rename is the allocation mutex: os.rename onto an existing
            # non-empty directory fails, so concurrent writers that picked the
            # same version collide here and the loser retries with the next one.
            while True:
                final_dir = dataset_dir / f"v{version:04d}"
                if not final_dir.exists():
                    try:
                        os.rename(tmp_dir, final_dir)
                        break
                    except OSError:
                        pass  # lost the race for this version; try the next
                version += 1
        except Exception:
            for f in tmp_dir.glob("*") if tmp_dir.exists() else []:
                f.unlink()
            if tmp_dir.exists():
                tmp_dir.rmdir()
            raise

        info = DatasetVersionInfo(
            dataset=name,
            version=version,
            row_count=table.num_rows,
            schema=[
                ColumnSchema(name=f.name, type=str(f.type)) for f in table.schema
            ],
            path=str(final_dir.relative_to(self.workspace.root)),
            build_id=build_id,
            source=source,
        )
        self.store.add_version(info)
        return info

    # -- reading --------------------------------------------------------------

    def _version_info(self, name: str, version: Optional[int]) -> DatasetVersionInfo:
        if self.store.get_dataset(name) is None:
            raise KeyError(f"Dataset not found: {name!r}")
        info = self.store.get_version(name, version)
        if info is None:
            if version is None:
                raise KeyError(f"Dataset {name!r} has no versions")
            raise KeyError(f"Dataset {name!r} has no version {version}")
        return info

    def read(self, name: str, version: Optional[int] = None) -> pa.Table:
        info = self._version_info(name, version)
        return pq.read_table(self.workspace.root / info.path)

    def parquet_glob(self, name: str, version: Optional[int] = None) -> str:
        """Absolute glob over the version's parquet files, for duckdb read_parquet()."""
        info = self._version_info(name, version)
        return str((self.workspace.root / info.path / "*.parquet").resolve())

    @staticmethod
    def table_to_rows(table: pa.Table) -> list[dict]:
        """A pyarrow Table as a list of JSON-safe dicts (used for policy-filtered
        pages, where paging happens in-memory rather than in duckdb)."""
        if table.num_rows == 0:
            return []
        cols = table.column_names
        columns = [c.to_pylist() for c in table.columns]
        return [
            {c: _json_safe(columns[j][i]) for j, c in enumerate(cols)}
            for i in range(table.num_rows)
        ]

    def rows(
        self,
        name: str,
        limit: int = 100,
        offset: int = 0,
        version: Optional[int] = None,
    ) -> list[dict]:
        """Page of rows as JSON-safe dicts, via duckdb (no row/column policy —
        callers that must enforce policy read+filter the table and slice it with
        ``table_to_rows``)."""
        glob = self.parquet_glob(name, version)
        con = duckdb.connect()
        try:
            cur = con.execute(
                "SELECT * FROM read_parquet(?) LIMIT ? OFFSET ?",
                [glob, limit, offset],
            )
            columns = [d[0] for d in cur.description]
            data = cur.fetchall()
        finally:
            con.close()
        return [
            {col: _json_safe(val) for col, val in zip(columns, row)} for row in data
        ]

    # -- ad-hoc query ---------------------------------------------------------

    def query(
        self,
        sql: str,
        max_rows: int = 1000,
        allowed: Optional[set[str]] = None,
        policy: Optional[Callable[[str, pa.Table], pa.Table]] = None,
    ) -> dict:
        """Run a read-only SQL query with each dataset's latest version exposed
        as a view named after the dataset. Returns
        ``{columns, rows, row_count, truncated}`` with JSON-safe values.

        Only datasets in ``allowed`` are registered (``None`` = all). A query
        referencing a dataset outside ``allowed`` fails as an unknown table, so
        this is how per-dataset ACLs are enforced on the ad-hoc query surface.

        The connection is read-only over in-memory tables, so a query can read
        the registered datasets but cannot mutate stored data or touch the
        filesystem. ``max_rows`` caps the result; ``truncated`` reports whether
        more rows were available.
        """
        con = duckdb.connect()
        try:
            for ds in self.store.list_datasets():
                if ds.latest_version is None:
                    continue
                if allowed is not None and ds.name not in allowed:
                    continue
                # Register each dataset's latest version as an in-memory Arrow
                # table rather than a file-backed view. Combined with disabling
                # external access below, this means arbitrary user SQL can read
                # the workspace's datasets but cannot touch the filesystem
                # (no read_csv('/etc/passwd'), no COPY ... TO, no path traversal).
                table = self.read(ds.name)
                if policy is not None:
                    # Row-level security / column masking: the workbench sees the
                    # same filtered/masked view of each dataset as the row API.
                    table = policy(ds.name, table)
                con.register(ds.name, table)
            # Lock down all filesystem/network access for the untrusted query.
            con.execute("SET enable_external_access=false")
            cur = con.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            data = cur.fetchmany(max_rows + 1)
            truncated = len(data) > max_rows
            data = data[:max_rows]
        finally:
            con.close()
        rows = [
            {col: _json_safe(val) for col, val in zip(columns, row)} for row in data
        ]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }

    # -- file uploads ---------------------------------------------------------

    def upload_file(self, name: str, path: Path, description: str = "") -> DatasetVersionInfo:
        """Ingest a CSV or Parquet file as a new dataset version."""
        _validate_name(name)
        path = Path(path)
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            con = duckdb.connect()
            try:
                table = con.execute(
                    "SELECT * FROM read_csv_auto(?)", [str(path)]
                ).arrow()
            finally:
                con.close()
        elif suffix in (".parquet", ".pq"):
            table = pq.read_table(path)
        else:
            raise ValueError(
                f"Unsupported file type {suffix!r}: expected .csv or .parquet"
            )
        if isinstance(table, pa.RecordBatchReader):
            table = table.read_all()
        return self.write(name, table, source="upload", description=description)
