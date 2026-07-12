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
from typing import Any, Callable, Iterable, Optional

import duckdb
import pyarrow as pa
import pyarrow.dataset as pads
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

    def _commit_version(
        self,
        name: str,
        tmp_dir: Path,
        *,
        row_count: int,
        schema: pa.Schema,
        source: str,
        build_id: Optional[str],
    ) -> DatasetVersionInfo:
        """Rename a fully-written temp dir to its final version directory and
        record the version. The rename is the allocation mutex: os.rename onto
        an existing non-empty directory fails, so concurrent writers that
        picked the same version collide here and the loser retries with the
        next one."""
        dataset_dir = self.workspace.data_dir / name
        dataset_dir.mkdir(parents=True, exist_ok=True)
        version = self.store.next_version(name)
        while True:
            final_dir = dataset_dir / f"v{version:04d}"
            if not final_dir.exists():
                try:
                    os.rename(tmp_dir, final_dir)
                    break
                except OSError:
                    pass  # lost the race for this version; try the next
            version += 1

        info = DatasetVersionInfo(
            dataset=name,
            version=version,
            row_count=row_count,
            schema=[ColumnSchema(name=f.name, type=str(f.type)) for f in schema],
            path=str(final_dir.relative_to(self.workspace.root)),
            build_id=build_id,
            source=source,
        )
        self.store.add_version(info)
        return info

    @staticmethod
    def _cleanup_tmp(tmp_dir: Path) -> None:
        for f in tmp_dir.glob("*") if tmp_dir.exists() else []:
            f.unlink()
        if tmp_dir.exists():
            tmp_dir.rmdir()

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

        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=self.workspace.data_dir)
        )
        try:
            pq.write_table(table, tmp_dir / "data.parquet")
        except Exception:
            self._cleanup_tmp(tmp_dir)
            raise
        return self._commit_version(
            name, tmp_dir,
            row_count=table.num_rows, schema=table.schema,
            source=source, build_id=build_id,
        )

    def write_batches(
        self,
        name: str,
        chunks: "Iterable[pa.Table]",
        source: str = "sync",
        build_id: Optional[str] = None,
        description: str = "",
    ) -> DatasetVersionInfo:
        """Stream an iterable of Arrow tables into one new dataset version
        without materializing them all in memory (used by connectors pulling
        large external tables). Every chunk is cast to the first chunk's
        schema; an incompatible chunk fails the whole write."""
        _validate_name(name)
        self.store.upsert_dataset(name, description)

        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=self.workspace.data_dir)
        )
        writer: Optional[pq.ParquetWriter] = None
        schema: Optional[pa.Schema] = None
        row_count = 0
        try:
            for chunk in chunks:
                if schema is None:
                    # A column that was all-NULL in the first chunk has arrow
                    # type null; later chunks would fail the cast, so type such
                    # columns as string up front.
                    schema = pa.schema([
                        pa.field(f.name, pa.string() if pa.types.is_null(f.type) else f.type)
                        for f in chunk.schema
                    ])
                    writer = pq.ParquetWriter(tmp_dir / "data.parquet", schema)
                writer.write_table(chunk.cast(schema))
                row_count += chunk.num_rows
            if writer is None:
                raise ValueError(f"Sync for dataset {name!r} produced no data")
            writer.close()
            writer = None
        except Exception:
            if writer is not None:
                writer.close()
            self._cleanup_tmp(tmp_dir)
            raise
        assert schema is not None
        return self._commit_version(
            name, tmp_dir,
            row_count=row_count, schema=schema,
            source=source, build_id=build_id,
        )

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

    def arrow_dataset(self, name: str, version: Optional[int] = None) -> "pads.Dataset":
        """The version's parquet files as a *lazy* pyarrow dataset. DuckDB can
        scan these with projection/filter pushdown, streaming batches instead
        of materializing the whole table in memory."""
        info = self._version_info(name, version)
        return pads.dataset(self.workspace.root / info.path, format="parquet")

    def query(
        self,
        sql: str,
        max_rows: int = 1000,
        allowed: Optional[set[str]] = None,
        policy_for: Optional[Callable[[str], Optional[Callable[[pa.Table], pa.Table]]]] = None,
    ) -> dict:
        """Run a read-only SQL query with each dataset's latest version exposed
        as a view named after the dataset. Returns
        ``{columns, rows, row_count, truncated}`` with JSON-safe values.

        Only datasets in ``allowed`` are registered (``None`` = all). A query
        referencing a dataset outside ``allowed`` fails as an unknown table, so
        this is how per-dataset ACLs are enforced on the ad-hoc query surface.

        ``policy_for`` maps a dataset name to an optional row-level-security /
        masking transform. Datasets with no transform are registered as lazy
        Arrow datasets — DuckDB streams them with projection and filter
        pushdown, so query memory scales with the result, not the dataset.
        Datasets that need a policy are read and filtered up front (the policy
        engine operates on tables), which keeps the security choke point
        identical to the row API.

        Either way the SQL itself never touches the filesystem: only
        registered Arrow objects are visible and external access is disabled
        (no read_csv('/etc/passwd'), no COPY ... TO, no path traversal).
        ``max_rows`` caps the result; ``truncated`` reports whether more rows
        were available.
        """
        con = duckdb.connect()
        try:
            for ds in self.store.list_datasets():
                if ds.latest_version is None:
                    continue
                if allowed is not None and ds.name not in allowed:
                    continue
                fn = policy_for(ds.name) if policy_for is not None else None
                if fn is None:
                    con.register(ds.name, self.arrow_dataset(ds.name))
                else:
                    con.register(ds.name, fn(self.read(ds.name)))
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
