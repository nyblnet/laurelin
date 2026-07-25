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
        inherited: Optional[list[str]] = None,
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

        rel_dir = final_dir.relative_to(self.workspace.root)
        new_parts = sorted(
            str(rel_dir / f.name) for f in final_dir.iterdir() if f.suffix == ".parquet"
        )
        info = DatasetVersionInfo(
            dataset=name,
            version=version,
            row_count=row_count,
            schema=[ColumnSchema(name=f.name, type=str(f.type)) for f in schema],
            path=str(rel_dir),
            files=list(inherited or []) + new_parts,
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

    def append(
        self,
        name: str,
        table: pa.Table,
        source: str = "append",
        build_id: Optional[str] = None,
        description: str = "",
    ) -> DatasetVersionInfo:
        """Add rows to a dataset as a new version, writing **only the delta**.

        A full ``write`` re-serializes the whole dataset, so a daily 1% delta
        on a 50 GB dataset costs 50 GB of I/O. An append writes one new part
        file and records a manifest that references the previous version's
        parts, so the cost is proportional to the new rows. The result is still
        an immutable version: earlier versions keep their own manifests and
        remain readable.

        The appended table must be castable to the current version's schema.
        For an empty dataset this is exactly ``write``.
        """
        _validate_name(name)
        self.store.upsert_dataset(name, description)

        previous = self.store.get_version(name, None)
        if previous is None:
            return self.write(name, table, source=source, build_id=build_id)

        schema = pa.schema([pa.field(c.name, pa.type_for_alias(c.type)) for c in previous.schema_])
        try:
            table = table.cast(schema)
        except (ValueError, TypeError, KeyError, pa.ArrowNotImplementedError) as exc:
            # pyarrow signals a name mismatch as ValueError and a type mismatch
            # as ArrowInvalid (a ValueError subclass).
            raise ValueError(
                f"Cannot append to {name!r}: incompatible schema "
                f"({exc}). Column names and types must match version "
                f"{previous.version}, or use a full write."
            ) from exc

        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=self.workspace.data_dir)
        )
        try:
            pq.write_table(table, tmp_dir / "part.parquet")
        except Exception:
            self._cleanup_tmp(tmp_dir)
            raise
        return self._commit_version(
            name, tmp_dir,
            row_count=previous.row_count + table.num_rows,
            schema=schema,
            source=source,
            build_id=build_id,
            inherited=self._inherited_files(previous),
        )

    def append_batches(
        self,
        name: str,
        chunks: "Iterable[pa.Table]",
        source: str = "append",
        build_id: Optional[str] = None,
        description: str = "",
    ) -> DatasetVersionInfo:
        """Streaming :meth:`append` — the incremental-sync path.

        Neither the existing dataset nor the incoming delta is held whole in
        memory: the delta streams to one new part file and the previous
        version's parts are referenced. An incremental sync that pulls no new
        rows is a no-op that returns the current version rather than an error.
        """
        _validate_name(name)
        self.store.upsert_dataset(name, description)
        previous = self.store.get_version(name, None)
        if previous is None:
            return self.write_batches(
                name, chunks, source=source, build_id=build_id
            )

        schema = pa.schema(
            [pa.field(c.name, pa.type_for_alias(c.type)) for c in previous.schema_]
        )
        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=self.workspace.data_dir)
        )
        writer: Optional[pq.ParquetWriter] = None
        added = 0
        try:
            for chunk in chunks:
                if chunk.num_rows == 0:
                    continue
                try:
                    chunk = chunk.cast(schema)
                except (ValueError, TypeError, KeyError, pa.ArrowNotImplementedError) as exc:
                    raise ValueError(
                        f"Cannot append to {name!r}: incompatible schema ({exc})."
                    ) from exc
                if writer is None:
                    writer = pq.ParquetWriter(tmp_dir / "part.parquet", schema)
                writer.write_table(chunk)
                added += chunk.num_rows
            if writer is not None:
                writer.close()
                writer = None
        except Exception:
            if writer is not None:
                writer.close()
            self._cleanup_tmp(tmp_dir)
            raise

        if added == 0:
            # Nothing new upstream: don't mint an identical version.
            self._cleanup_tmp(tmp_dir)
            return previous

        return self._commit_version(
            name, tmp_dir,
            row_count=previous.row_count + added,
            schema=schema,
            source=source,
            build_id=build_id,
            inherited=self._inherited_files(previous),
        )

    def _inherited_files(self, info: DatasetVersionInfo) -> list[str]:
        """The manifest to carry forward from ``info`` — materializing the
        pre-manifest layout (a bare version directory) into explicit paths."""
        if info.files:
            return list(info.files)
        root = self.workspace.root
        return sorted(
            str(p.relative_to(root)) for p in (root / info.path).glob("*.parquet")
        )

    def compact(self, name: str, description: str = "") -> DatasetVersionInfo:
        """Rewrite the latest version's parts into a single file.

        Appends are cheap but accumulate parts, and many small files make scans
        slower. Compaction trades one full rewrite for a tidy layout; earlier
        versions are untouched and still readable.
        """
        _validate_name(name)
        info = self._version_info(name, None)
        parts = len(self._files_for(info))
        table = self.read(name)
        result = self.write(name, table, source="compact", description=description)
        self.store.log_audit(
            "dataset_compacted",
            {"dataset": name, "parts_before": parts, "version": result.version,
             "row_count": result.row_count},
        )
        return result

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

    def _files_for(self, info: DatasetVersionInfo) -> list[str]:
        """Absolute paths of the Parquet parts making up a version.

        A version written by ``append`` lists parts that live in *earlier*
        version directories — version dirs are immutable and never deleted, so
        those references stay valid. An empty manifest means the pre-manifest
        layout: everything under the version directory.
        """
        root = self.workspace.root
        if info.files:
            return [str((root / f).resolve()) for f in info.files]
        return sorted(str(p.resolve()) for p in (root / info.path).glob("*.parquet"))

    def version_files(self, name: str, version: Optional[int] = None) -> list[str]:
        return self._files_for(self._version_info(name, version))

    def read(self, name: str, version: Optional[int] = None) -> pa.Table:
        return pq.read_table(self.version_files(name, version))

    def parquet_glob(self, name: str, version: Optional[int] = None) -> str:
        """A DuckDB ``read_parquet`` argument covering the version's parts.

        Returns a SQL list literal, so it handles a multi-part (appended)
        version as well as a single file. Embed it directly:
        ``SELECT * FROM read_parquet(<this>)``.
        """
        files = self.version_files(name, version)
        return "[" + ", ".join("'" + f.replace("'", "''") + "'" for f in files) + "]"

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
        files = self.version_files(name, version)
        con = duckdb.connect()
        try:
            cur = con.execute(
                "SELECT * FROM read_parquet(?) LIMIT ? OFFSET ?",
                [files, limit, offset],
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
        return pads.dataset(self.version_files(name, version), format="parquet")

    def scan_for(self, name: str, plan_for=None, version: Optional[int] = None):
        """A scannable object for ``name`` with any row/column policy applied.

        Returns a lazy pyarrow Dataset (or Scanner) whenever the policy can be
        expressed as a filter/projection, so DuckDB streams it with pushdown;
        falls back to a materialized, policy-filtered Table only when a rule
        has no Arrow equivalent. Either way the caller sees exactly the rows
        and values this user may see.
        """
        dataset = self.arrow_dataset(name, version)
        plan = plan_for(name, dataset.schema) if plan_for is not None else None
        if plan is None:
            return dataset
        if not plan.lazy:
            return plan.apply(self.read(name, version))
        scan = dataset
        if plan.filter is not None:
            # Dataset.filter keeps this a *Dataset*, so DuckDB can still push
            # its own column pruning and predicates through — a filtered scan
            # costs about the same as an unfiltered one. Building a Scanner
            # here instead would freeze the column set and cost ~3x.
            scan = scan.filter(plan.filter)
        if plan.projection is not None:
            # Masking needs computed columns, which only a Scanner can express;
            # that fixes the projection, so masked datasets lose column pruning.
            scan = scan.scanner(columns=plan.projection)
        return scan

    def query(
        self,
        sql: str,
        max_rows: int = 1000,
        allowed: Optional[set[str]] = None,
        plan_for=None,
    ) -> dict:
        """Run a read-only SQL query with each dataset's latest version exposed
        as a view named after the dataset. Returns
        ``{columns, rows, row_count, truncated}`` with JSON-safe values.

        Only datasets in ``allowed`` are registered (``None`` = all). A query
        referencing a dataset outside ``allowed`` fails as an unknown table, so
        this is how per-dataset ACLs are enforced on the ad-hoc query surface.

        ``plan_for`` maps ``(dataset, schema)`` to a row-level-security /
        masking plan. Every dataset is registered through ``scan_for``, which
        keeps the scan lazy — pushing the policy's filter and projection into
        the Parquet read — unless a rule has no Arrow equivalent, in which case
        it materializes and applies the exact policy engine. Query memory
        tracks the result rather than the dataset, and the security choke point
        is the same one the row API uses.

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
                con.register(ds.name, self.scan_for(ds.name, plan_for=plan_for))
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

    def upload_file(
        self, name: str, path: Path, description: str = "", mode: str = "replace"
    ) -> DatasetVersionInfo:
        """Ingest a CSV or Parquet file as a new dataset version.

        ``mode="append"`` adds the file's rows to the existing dataset without
        rewriting it (see :meth:`append`); the default replaces it.
        """
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
        if mode not in ("replace", "append"):
            raise ValueError(f"Unknown upload mode {mode!r}: expected 'replace' or 'append'")
        if mode == "append":
            return self.append(name, table, source="upload", description=description)
        return self.write(name, table, source="upload", description=description)
