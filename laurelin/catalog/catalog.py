"""DatasetCatalog: versioned Parquet dataset storage.

A version is a **manifest of immutable Parquet parts**, recorded in the
MetadataStore:

    data/<dataset>/parts/<uuid>.parquet

Parts are written to unique keys and never rewritten, so an ``append`` writes
only its delta and references the previous version's parts. Registering the
manifest row is the commit point: a crash before it leaves an unreferenced
part (collectable garbage), never a registered-but-missing version.

Bytes are addressed through ``laurelin.core.storage``, so the data plane can
be a local directory or an object store. Older workspaces whose versions
predate manifests are still read by listing their version directory.
"""

from __future__ import annotations

import math
import os
import re
import time as _time
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

import duckdb
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from laurelin.core import federation, limits, metrics
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import ColumnSchema, DatasetInfo, DatasetVersionInfo
from laurelin.core.storage import storage_for

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid dataset name {name!r}: must match ^[a-z][a-z0-9_]*$"
        )


def _is_duplicate_version(exc: BaseException) -> bool:
    """True if this error is a (dataset, version) primary-key collision — i.e.
    another writer claimed the version number first. Matched on message text
    because sqlite3 and psycopg raise different exception types."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return "unique" in text or "duplicate key" in text


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

    def __init__(self, workspace: Workspace, store: MetadataStore, storage=None):
        self.workspace = workspace
        self.store = store
        # All dataset bytes go through here, so the data plane can be a local
        # directory or an object store without the catalog knowing which.
        self.storage = storage or storage_for(workspace)

    # -- datasets -------------------------------------------------------------

    def create_dataset(self, name: str, description: str = "") -> DatasetInfo:
        _validate_name(name)
        return self.store.upsert_dataset(name, description)

    # -- writing --------------------------------------------------------------

    def _commit_version(
        self,
        name: str,
        new_parts: list[str],
        *,
        row_count: int,
        schema: pa.Schema,
        source: str,
        build_id: Optional[str],
        inherited: Optional[list[str]] = None,
    ) -> DatasetVersionInfo:
        """Register already-written parts as a new version.

        The parts are at unique keys, so writers never collide in storage; the
        only thing needing arbitration is the version *number*, and the
        metadata database does that via the (dataset, version) primary key. A
        writer that loses the race simply retries with the next number — its
        bytes are already safely written and don't move.

        This replaces the old atomic directory rename, which had no equivalent
        on object storage. The invariant it protected — never register a
        version whose files aren't fully written — still holds, because the
        row is inserted last.
        """
        files = list(inherited or []) + list(new_parts)
        columns = [ColumnSchema(name=f.name, type=str(f.type)) for f in schema]
        version = self.store.next_version(name)
        while True:
            info = DatasetVersionInfo(
                dataset=name,
                version=version,
                row_count=row_count,
                schema=columns,
                path=f"data/{name}",
                files=files,
                build_id=build_id,
                source=source,
            )
            try:
                self.store.add_version(info)
                return info
            except Exception as exc:  # noqa: BLE001 - dialect-specific integrity errors
                if not _is_duplicate_version(exc):
                    raise
                version += 1  # another writer took this number; take the next

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

        key = self.storage.new_part_key(name)
        try:
            self.storage.write_table(table, key)
        except Exception:
            self.storage.delete(key)
            raise
        return self._commit_version(
            name, [key],
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

        key = self.storage.new_part_key(name)
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
                    writer = self.storage.writer(key, schema)
                writer.write_table(chunk.cast(schema))
                row_count += chunk.num_rows
            if writer is None:
                raise ValueError(f"Sync for dataset {name!r} produced no data")
            writer.close()
            writer = None
        except Exception:
            if writer is not None:
                writer.close()
            self.storage.delete(key)
            raise
        assert schema is not None
        return self._commit_version(
            name, [key],
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

        key = self.storage.new_part_key(name)
        try:
            self.storage.write_table(table, key)
        except Exception:
            self.storage.delete(key)
            raise
        return self._maybe_compact(self._commit_version(
            name, [key],
            row_count=previous.row_count + table.num_rows,
            schema=schema,
            source=source,
            build_id=build_id,
            inherited=self._inherited_files(previous),
        ))

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
        key = self.storage.new_part_key(name)
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
                    writer = self.storage.writer(key, schema)
                writer.write_table(chunk)
                added += chunk.num_rows
            if writer is not None:
                writer.close()
                writer = None
        except Exception:
            if writer is not None:
                writer.close()
            self.storage.delete(key)
            raise

        if added == 0:
            # Nothing new upstream: don't mint an identical version.
            self.storage.delete(key)
            return previous

        return self._maybe_compact(self._commit_version(
            name, [key],
            row_count=previous.row_count + added,
            schema=schema,
            source=source,
            build_id=build_id,
            inherited=self._inherited_files(previous),
        ))

    def _inherited_files(self, info: DatasetVersionInfo) -> list[str]:
        """The manifest to carry forward from ``info`` — materializing the
        pre-manifest layout (a bare version directory) into explicit keys."""
        if info.files:
            return list(info.files)
        return [k for k in self.storage.list_keys(info.path) if k.endswith(".parquet")]

    def _maybe_compact(self, info: DatasetVersionInfo) -> DatasetVersionInfo:
        """Compact once a version has accumulated too many parts.

        Appends are cheap precisely because they don't rewrite, but many small
        parts eventually slow every scan. Rewriting on a threshold amortizes
        that cost the way a hash table amortizes resizing: most appends stay
        O(delta), and occasionally one pays for a tidy layout.

        Off unless ``LAURELIN_AUTO_COMPACT_PARTS`` is set, because the right
        threshold depends on how often you append versus how often you read.
        """
        threshold = int(os.environ.get("LAURELIN_AUTO_COMPACT_PARTS", "0") or 0)
        if threshold <= 0 or len(info.files) < threshold:
            return info
        return self.compact(info.dataset, auto=True)

    def compact(
        self, name: str, description: str = "", auto: bool = False
    ) -> DatasetVersionInfo:
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
             "row_count": result.row_count, "automatic": auto},
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
        """Storage keys of the Parquet parts making up a version.

        A version written by ``append`` references parts written for *earlier*
        versions; parts are immutable and never rewritten, so those references
        stay valid. An empty manifest means the pre-manifest layout: everything
        under the version's directory.
        """
        if info.files:
            return list(info.files)
        return [k for k in self.storage.list_keys(info.path) if k.endswith(".parquet")]

    def version_files(self, name: str, version: Optional[int] = None) -> list[str]:
        """Storage keys for a version's parts."""
        return self._files_for(self._version_info(name, version))

    def iter_batches(
        self,
        name: str,
        version: Optional[int] = None,
        batch_rows: int = 200_000,
    ) -> "Iterable[pa.Table]":
        """Stream a dataset as Arrow tables without materializing it.

        The batches come off the Parquet scan lazily, so a consumer that also
        streams its output never holds more than one batch. A federated table
        has no local scan to iterate, so it is yielded as a single batch —
        honest rather than pretending to stream something already collected.
        """
        info = self.store.get_dataset(name)
        if info is not None and info.is_federated:
            yield self.federated_table(name)
            return
        scanner = self.arrow_dataset(name, version).scanner(batch_size=batch_rows)
        for record_batch in scanner.to_batches():
            if record_batch.num_rows:
                yield pa.Table.from_batches([record_batch])

    def read(self, name: str, version: Optional[int] = None) -> pa.Table:
        info = self.store.get_dataset(name)
        if info is not None and info.is_federated:
            # No versions to pin: a federated table is read as it is now.
            return self.federated_table(name)
        return self.storage.read_table(self.version_files(name, version))

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
        info = self.store.get_dataset(name)
        if info is not None and info.is_federated:
            # Page a federated table at the source rather than dragging it back.
            table = self.federated_table(name, limit=limit + offset)
            return self.table_to_rows(table.slice(offset, limit))
        con = duckdb.connect()
        try:
            # Register the Arrow dataset rather than passing file paths, so
            # DuckDB needs no filesystem access — the same code path works when
            # the parts live in object storage.
            con.execute("SET enable_external_access=false")
            con.register("__ds", self.arrow_dataset(name, version))
            cur = con.execute(
                "SELECT * FROM __ds LIMIT ? OFFSET ?",
                [limit, offset],
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
        return self.storage.dataset(self.version_files(name, version))

    # -- federated datasets -----------------------------------------------------

    def register_federated(
        self, name: str, source: dict, description: str = ""
    ) -> DatasetInfo:
        """Register a table Laurelin governs but does not hold."""
        _validate_name(name)
        federation.validate_source(source)
        federation.columns_of(source)  # fail fast if it isn't reachable
        self.store.upsert_dataset(name, description)
        self.store.set_dataset_source(name, "federated", source)
        info = self.store.get_dataset(name)
        assert info is not None
        return info

    def federated_table(
        self,
        name: str,
        sql_policy_for=None,
        limit: Optional[int] = None,
    ) -> pa.Table:
        """Scan a federated dataset, with row/column policy applied remotely.

        The policy is compiled to SQL and wrapped around the scan, so filtering
        happens at the source rather than after the data arrives. A policy that
        cannot be compiled is a refusal, never an unfiltered read.
        """
        info = self.store.get_dataset(name)
        if info is None or not info.is_federated:
            raise KeyError(f"Not a federated dataset: {name!r}")

        con = federation.connect(info.source)
        try:
            expr, params, _ = federation.scan_expression(info.source)
            columns = federation.columns_of(info.source, con)
            select_list, where, policy_params = "*", "TRUE", []
            if sql_policy_for is not None:
                policy = sql_policy_for(name, columns)
                select_list, where = policy.select_list, policy.where
                policy_params = policy.params
            sql = f"SELECT {select_list} FROM {expr} WHERE {where}"
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
            # A federated scan can be expensive on someone *else's*
            # infrastructure, so it gets the same budget as a local one.
            with limits.limited(con, limits.QueryLimits.interactive()):
                result = con.execute(sql, [*params, *policy_params]).arrow()
            if isinstance(result, pa.RecordBatchReader):
                result = result.read_all()
            return result
        except federation.FederationError:
            raise
        except duckdb.Error as exc:
            raise federation.FederationError(
                f"Federated scan of {name!r} failed: {exc}"
            ) from exc
        finally:
            con.close()

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
        sql_policy_for=None,
        federated_scan_limit: int = 1_000_000,
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
                if allowed is not None and ds.name not in allowed:
                    continue
                if ds.is_federated:
                    if not federation.workbench_enabled() or sql_policy_for is None:
                        # Off by default: enabling federation must not silently
                        # widen what ad-hoc SQL can reach. Unregistered means
                        # "unknown table", the same as any dataset you can't see.
                        continue
                    con.register(
                        ds.name,
                        self.federated_table(
                            ds.name, sql_policy_for=sql_policy_for,
                            limit=federated_scan_limit,
                        ),
                    )
                    continue
                if ds.latest_version is None:
                    continue
                con.register(ds.name, self.scan_for(ds.name, plan_for=plan_for))
            # Lock down all filesystem/network access for the untrusted query.
            con.execute("SET enable_external_access=false")
            # Resource budget + admission control: user SQL is arbitrary, so
            # this is where one expensive query is stopped from degrading the
            # replica for everyone else on it.
            metrics.queries.labels(surface="workbench").inc()
            _started = _time.perf_counter()
            with limits.limited(con, limits.QueryLimits.interactive()):
                cur = con.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                data = cur.fetchmany(max_rows + 1)
                truncated = len(data) > max_rows
                data = data[:max_rows]
        finally:
            con.close()
        metrics.query_duration.labels(surface="workbench").observe(
            _time.perf_counter() - _started
        )
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
