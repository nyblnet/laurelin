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
from typing import Any, Callable, Iterable, Optional

import duckdb
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from laurelin.core import clickhouse, federation, limits, metrics, starrocks
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.dialects import CLICKHOUSE, DUCKDB, STARROCKS
from laurelin.core.failure import Failure, Phase
from laurelin.core.models import ColumnSchema, DatasetInfo, DatasetVersionInfo
from laurelin.core.permissions import PolicyRenderError
from laurelin.core.storage import storage_for

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid dataset name {name!r}: must match ^[a-z][a-z0-9_]*$"
        )


def _refuse_if_needs_credentials(info) -> None:
    """Refuse to read a dataset whose bytes did not survive an import.

    Loudly, and never as an empty result set: in a governance product zero rows
    is indistinguishable from a working row policy, so a silent empty read of a
    migrated federated table looks exactly like correct enforcement. The
    exception is mapped to HTTP 409 and its message names what to re-supply.
    """
    from laurelin.export.manifest import (
        DATA_STATE_KEY,
        NEEDS_CREDENTIALS_KEY,
        NeedsCredentials,
    )

    source = getattr(info, "source", None) or {}
    if not source.get(NEEDS_CREDENTIALS_KEY):
        return
    state = source.get(DATA_STATE_KEY, "elsewhere")
    if state == "metadata_only":
        detail = (
            "its metadata was imported without its data. Re-import from a full "
            "export, or re-upload it."
        )
    else:
        detail = (
            "it points at a system this workspace has no endpoint for. The "
            "export withheld the connection details; re-supply them and the "
            "sentinel clears."
        )
    raise NeedsCredentials(f"Dataset {info.name!r} cannot be read: {detail}")


def suggest_dataset_name(filename: str) -> str:
    """A valid dataset name derived from an uploaded file's name.

    Dataset names are ``^[a-z][a-z0-9_]*$``, which almost no real filename
    satisfies ("Q3 Orders (final).csv"). Rejecting those and making the user
    invent a name is a bad first five minutes; suggesting ``q3_orders_final``
    and letting them edit it is a good one.
    """
    stem = Path(filename).stem.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        return "dataset"
    if not slug[0].isalpha():
        slug = f"d_{slug}"
    return slug[:48].rstrip("_")


def _iceberg_path(location: str) -> str:
    return location[len("file://"):] if location.startswith("file://") else location


class StaleBaseVersion(ValueError):
    """A compare-and-set write lost: the dataset moved under the writer.

    A ``ValueError`` so the API keeps mapping it to a client error, and its own
    type so a caller can retry the *build* rather than guess from a string.
    """


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

    def _refuse_write_at_source(self, name: str) -> None:
        """Writing to a dataset that is scanned at source is not a partial
        feature, it is a corrupt one.

        The write would land in local Parquet parts and mint a version row,
        while ``read()`` keeps returning the remote table — leaving a dataset
        that is half managed and half remote, and reporting only the half you
        did not write. Iceberg is exempt because Laurelin owns and writes that
        table for real (``write_iceberg``).
        """
        info = self.store.get_dataset(name)
        if info is None or not info.scans_at_source or info.is_iceberg:
            return
        raise ValueError(
            f"Dataset {name!r} is a {info.kind} dataset: it is scanned at the "
            "source and Laurelin does not write to it. Land the rows in a "
            "managed dataset instead (a transform can read this one and write "
            "that one)."
        )

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
        validate: Optional[Callable[[list[str]], None]] = None,
        expect_version: Optional[int] = None,
    ) -> DatasetVersionInfo:
        """Register already-written parts as a new version.

        The parts are at unique keys, so writers never collide in storage; the
        only thing needing arbitration is the version *number*, and the
        metadata database does that via the (dataset, version) primary key. A
        writer that loses the race simply retries with the next number — its
        bytes are already safely written and don't move.

        ``expect_version`` turns that into a compare-and-set: the new version
        must be exactly ``expect_version + 1`` or nothing is registered. Taking
        the next free number is right for a writer publishing *new* rows and
        wrong for one publishing a *rewrite* of a base it already read —
        writeback rebuilds the whole dataset from a version it read earlier, so
        letting it take V+2 after someone else published V+1 silently discards
        V+1's rows. Its own check-then-write cannot close that: the gap between
        reading the version and inserting the row is where the other writer
        lands. This is the same check with no gap.

        This replaces the old atomic directory rename, which had no equivalent
        on object storage. The invariant it protected — never register a
        version whose files aren't fully written — still holds, because the
        row is inserted last.

        ``validate`` runs after the parts exist but before the row does, which
        is exactly where a data-quality check belongs: the row insert *is* the
        publication, so a check that raises here means the bad version was
        never visible to anyone. The caller deletes the orphaned parts.
        """
        files = list(inherited or []) + list(new_parts)
        if validate is not None:
            validate(files)
        columns = [ColumnSchema(name=f.name, type=str(f.type)) for f in schema]
        version = self.store.next_version(name)
        if expect_version is not None and version != expect_version + 1:
            raise StaleBaseVersion(
                f"Dataset {name!r} moved from version {expect_version} to "
                f"{version - 1} while this write was being built. Nothing was "
                f"registered; retry."
            )
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
                if expect_version is not None:
                    raise StaleBaseVersion(
                        f"Dataset {name!r} version {version} was published by "
                        f"another writer while this write was being built. "
                        f"Nothing was registered; retry."
                    ) from exc
                version += 1  # another writer took this number; take the next

    def write(
        self,
        name: str,
        table: pa.Table,
        source: str = "upload",
        build_id: Optional[str] = None,
        description: str = "",
        validate: Optional[Callable[[list[str]], None]] = None,
        expect_version: Optional[int] = None,
    ) -> DatasetVersionInfo:
        """Write a new immutable version of a dataset.

        The parquet file is written to a temp dir inside data/ and renamed to
        its final version directory before the version is recorded, so a crash
        mid-write never leaves a registered-but-missing version.

        ``expect_version`` makes it a compare-and-set; see ``_commit_version``.
        """
        _validate_name(name)
        self._refuse_write_at_source(name)
        self.store.upsert_dataset(name, description)

        key = self.storage.new_part_key(name)
        try:
            self.storage.write_table(table, key)
            return self._commit_version(
                name, [key],
                row_count=table.num_rows, schema=table.schema,
                source=source, build_id=build_id, validate=validate,
                expect_version=expect_version,
            )
        except Exception:
            # Covers a failed write and a failed validation alike: in both
            # cases the part is unreferenced, so removing it leaves nothing
            # behind rather than a version nobody registered.
            self.storage.delete(key)
            raise

    def write_batches(
        self,
        name: str,
        chunks: "Iterable[pa.Table]",
        source: str = "sync",
        build_id: Optional[str] = None,
        description: str = "",
        validate: Optional[Callable[[list[str]], None]] = None,
    ) -> DatasetVersionInfo:
        """Stream an iterable of Arrow tables into one new dataset version
        without materializing them all in memory (used by connectors pulling
        large external tables). Every chunk is cast to the first chunk's
        schema; an incompatible chunk fails the whole write."""
        _validate_name(name)
        self._refuse_write_at_source(name)
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
            assert schema is not None
            return self._commit_version(
                name, [key],
                row_count=row_count, schema=schema,
                source=source, build_id=build_id, validate=validate,
            )
        except Exception:
            if writer is not None:
                writer.close()
            self.storage.delete(key)
            raise

    def append(
        self,
        name: str,
        table: pa.Table,
        source: str = "append",
        build_id: Optional[str] = None,
        description: str = "",
        validate: Optional[Callable[[list[str]], None]] = None,
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
        self._refuse_write_at_source(name)
        self.store.upsert_dataset(name, description)

        previous = self.store.get_version(name, None)
        if previous is None:
            return self.write(name, table, source=source, build_id=build_id,
                              validate=validate)

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
            return self._maybe_compact(self._commit_version(
                name, [key],
                row_count=previous.row_count + table.num_rows,
                schema=schema,
                source=source,
                build_id=build_id,
                inherited=self._inherited_files(previous),
                validate=validate,
            ))
        except Exception:
            self.storage.delete(key)
            raise

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
        self._refuse_write_at_source(name)
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
        dataset = self.store.get_dataset(name)
        if dataset is None:
            raise KeyError(f"Dataset not found: {name!r}")
        _refuse_if_needs_credentials(dataset)
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
        if info is not None and info.scans_at_source:
            yield self.source_table(name)
            return
        scanner = self.arrow_dataset(name, version).scanner(batch_size=batch_rows)
        for record_batch in scanner.to_batches():
            if record_batch.num_rows:
                yield pa.Table.from_batches([record_batch])

    # -- Iceberg ---------------------------------------------------------------

    def _iceberg(self):
        from laurelin.core.iceberg import IcebergTables

        return IcebergTables(self.workspace)

    def create_iceberg_dataset(self, name: str, description: str = "") -> DatasetInfo:
        """Declare a dataset whose storage is an Iceberg table.

        Laurelin still owns and versions it — the difference from `managed` is
        that Spark, Trino, Snowflake and DuckDB can open the table without
        Laurelin running, and each write leaves a snapshot in the table's own
        history rather than only in ours.
        """
        _validate_name(name)
        self.store.upsert_dataset(name, description)
        self.store.set_dataset_source(name, "iceberg", {"type": "iceberg", "path": ""})
        return self.store.get_dataset(name)

    def write_iceberg(
        self,
        name: str,
        table: pa.Table,
        mode: str = "replace",
        source: str = "upload",
        build_id: Optional[str] = None,
        description: str = "",
    ) -> DatasetVersionInfo:
        """Write an Iceberg-backed dataset and record the snapshot as a version.

        The Iceberg snapshot is the commit; the Laurelin version row names it.
        Keeping both means a version number and a snapshot id refer to the same
        point in history, so lineage, builds and time travel keep working
        without a second notion of "when".
        """
        _validate_name(name)
        info = self.store.get_dataset(name)
        if info is None or not info.is_iceberg:
            self.create_iceberg_dataset(name, description)

        state = self._iceberg().write(name, table, mode=mode)
        # The metadata location changes with every snapshot, so the source is
        # rewritten to point at the current one — that is what readers scan.
        self.store.set_dataset_source(
            name, "iceberg", {"type": "iceberg",
                              "path": _iceberg_path(state["metadata_location"])}
        )
        version = self.store.next_version(name)
        info = DatasetVersionInfo(
            dataset=name,
            version=version,
            snapshot_id=state["snapshot_id"],
            row_count=state["rows"],
            schema=[ColumnSchema(name=f.name, type=str(f.type)) for f in table.schema],
            path=f"iceberg/{name}",
            files=[],
            build_id=build_id,
            source=source,
        )
        self.store.add_version(info)
        return info

    def iceberg_snapshots(self, name: str) -> list[dict]:
        return self._iceberg().snapshots(name)

    def read_iceberg(self, name: str, version: Optional[int] = None) -> pa.Table:
        """Read an Iceberg dataset, optionally as of one of its versions."""
        snapshot_id = None
        if version is not None:
            recorded = self.store.get_version(name, version)
            if recorded is None:
                raise KeyError(f"No version {version} of dataset {name!r}")
            snapshot_id = recorded.snapshot_id
        return self._iceberg().read(name, snapshot_id=snapshot_id)

    # -- Iceberg branches & schema ----------------------------------------------

    def iceberg_branch(self, name: str, branch: str,
                       from_version: Optional[int] = None) -> dict:
        """Cut a branch so work can happen without touching what readers see."""
        snapshot_id = None
        if from_version is not None:
            recorded = self.store.get_version(name, from_version)
            if recorded is None:
                raise KeyError(f"No version {from_version} of dataset {name!r}")
            snapshot_id = recorded.snapshot_id
        return self._iceberg().create_branch(name, branch, snapshot_id=snapshot_id)

    def iceberg_branches(self, name: str) -> list[dict]:
        return self._iceberg().branches(name)

    def delete_iceberg_branch(self, name: str, branch: str) -> None:
        self._iceberg().delete_branch(name, branch)

    def merge_iceberg_branch(self, name: str, branch: str) -> DatasetVersionInfo:
        """Fast-forward main to a branch, recording the result as a version.

        Without the version row the merge would be invisible to everything
        outside Iceberg — lineage, builds and time travel all speak in
        Laurelin versions.
        """
        result = self._iceberg().merge_branch(name, branch)
        state = self._iceberg().state(name)
        self.store.set_dataset_source(
            name, "iceberg",
            {"type": "iceberg", "path": _iceberg_path(state["metadata_location"])},
        )
        table = self._iceberg().read(name)
        info = DatasetVersionInfo(
            dataset=name,
            version=self.store.next_version(name),
            snapshot_id=result["snapshot_id"],
            row_count=table.num_rows,
            schema=[ColumnSchema(name=f.name, type=str(f.type)) for f in table.schema],
            path=f"iceberg/{name}",
            files=[],
            source=f"merge:{branch}",
        )
        self.store.add_version(info)
        return info

    def downstream_of(self, dataset: str) -> list[str]:
        """Datasets derived from this one, transitively.

        A breaking schema change is only safe to reason about with this in
        hand: the question is never "is dropping this column fine?" but "what
        breaks when I do?"
        """
        edges = self.store.list_lineage()
        children: dict[str, set[str]] = {}
        for edge in edges:
            children.setdefault(edge.upstream_dataset, set()).add(edge.downstream_dataset)
        seen: set[str] = set()
        queue = list(children.get(dataset, ()))
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(children.get(current, ()))
        return sorted(seen)

    def evolve_iceberg_schema(
        self,
        name: str,
        add: Optional[dict] = None,
        drop: Optional[list[str]] = None,
        rename: Optional[dict] = None,
        allow_breaking: bool = False,
    ) -> list[str]:
        """Change an Iceberg dataset's schema.

        Adding a column is additive and always allowed: Iceberg tracks columns
        by id, so old snapshots stay readable and nothing downstream can break
        by gaining a field it doesn't reference.

        Dropping or renaming one is different — it breaks every transform,
        object type and dashboard that names it — so it requires
        ``allow_breaking=True`` and the error names what is downstream. The
        point is not to forbid the change but to stop it being made without
        seeing the blast radius.
        """
        if (drop or rename) and not allow_breaking:
            affected = self.downstream_of(name)
            impact = (", ".join(affected) if affected
                      else "no derived datasets, but object types and dashboards "
                           "may still reference it")
            raise ValueError(
                f"Dropping or renaming a column on {name!r} is a breaking change. "
                f"Downstream: {impact}. Pass allow_breaking=True once you've "
                "checked what references it."
            )
        columns = self._iceberg().evolve_schema(name, add=add, drop=drop, rename=rename)
        state = self._iceberg().state(name)
        self.store.set_dataset_source(
            name, "iceberg",
            {"type": "iceberg", "path": _iceberg_path(state["metadata_location"])},
        )
        return columns

    def read(self, name: str, version: Optional[int] = None) -> pa.Table:
        info = self.store.get_dataset(name)
        if info is not None and info.is_iceberg:
            # Unlike a federated table, this one *does* have versions — each
            # pinned to an Iceberg snapshot, so history is readable.
            return self.read_iceberg(name, version)
        if info is not None and info.scans_at_source:
            # No versions to pin: a federated or ClickHouse table is read as it
            # is now. (Iceberg is handled above — it really does have history.)
            return self.source_table(name)
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
        if info is not None and info.scans_at_source:
            # Page a federated table at the source rather than dragging it back.
            table = self.source_table(name, limit=limit + offset)
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

    # -- source-scanned datasets -------------------------------------------------
    #
    # Federated, Iceberg and ClickHouse datasets are all read through
    # `source_table` below, which is the ONLY place a SqlPolicy is applied to a
    # scan. Keeping it that way is the point: a second apply site is a second
    # interpretation of what a policy means, and the two will drift.
    #
    # The engines differ in three things — how a scan expression is written,
    # how policy values travel, and how the statement is assembled — so each
    # gets a small reader below. Everything else, including the fail-closed
    # rules, is shared.

    class _DuckDbSource:
        """Federated and Iceberg: DuckDB, bound parameters, hardened connection."""

        dialect = DUCKDB

        def __init__(self, source: dict):
            self.source = source
            self.con = federation.connect(source)

        def schema(self) -> pa.Schema:
            return federation.schema_of(self.source, self.con)

        def run(self, select_list: str, where: str, params: list, limit) -> pa.Table:
            expr, scan_params, _ = federation.scan_expression(self.source)
            sql = DUCKDB.assemble(select_list, expr, where, limit)
            # A scan can be expensive on someone *else's* infrastructure, so it
            # gets the same budget as a local one.
            with limits.limited(self.con, limits.QueryLimits.interactive()):
                result = self.con.execute(sql, [*scan_params, *params]).arrow()
            if isinstance(result, pa.RecordBatchReader):
                result = result.read_all()
            return result

        def close(self) -> None:
            self.con.close()

    class _ClickHouseSource:
        """ClickHouse via chdb: no connection to hold, no parameters to bind.

        The budgets ride in the statement's SETTINGS clause because ClickHouse
        enforces them itself; there is no watchdog thread and no `interrupt()`.
        """

        dialect = CLICKHOUSE

        def __init__(self, source: dict):
            self.source = source

        def schema(self) -> pa.Schema:
            return clickhouse.schema_of(self.source)

        def run(self, select_list: str, where: str, params: list, limit) -> pa.Table:
            assert not params, "the ClickHouse dialect binds nothing"
            budget = limits.QueryLimits.interactive()
            sql = CLICKHOUSE.assemble(
                select_list,
                clickhouse.scan_expression(self.source),
                where,
                limit,
                limits.clickhouse_settings(budget),
            )
            with limits.clickhouse_limited(budget):
                return clickhouse.run(sql)

        def close(self) -> None:
            pass

    class _StarRocksSource:
        """StarRocks over the MySQL wire protocol: a connection, and real binds.

        Closest to ``_DuckDbSource`` of the three — it holds a connection and
        binds every policy value — and unlike it, the budgets ride inside the
        statement as a ``SET_VAR`` hint, because the work happens on a server
        this process cannot interrupt.
        """

        dialect = STARROCKS

        def __init__(self, source: dict):
            self.source = source
            self.con = starrocks.connect(source)
            self._schema = None

        def schema(self) -> pa.Schema:
            if self._schema is None:
                self._schema = starrocks.schema_of(self.source, self.con)
            return self._schema

        def run(self, select_list: str, where: str, params: list, limit) -> pa.Table:
            budget = limits.QueryLimits.interactive()
            sql = STARROCKS.assemble(
                select_list,
                starrocks.scan_expression(self.source),
                where,
                limit,
                limits.starrocks_hint(budget),
            )
            with limits.starrocks_limited(budget):
                # The declared schema types the result: the protocol cannot
                # tell a BOOLEAN from a TINYINT (see laurelin/core/starrocks.py).
                return starrocks.run(sql, params, self.con, schema=self.schema())

        def close(self) -> None:
            try:
                self.con.close()
            except Exception:  # noqa: BLE001 - closing a dead connection
                pass

    # Total, and with no fallback: a kind that is not here has no reader.
    # `return self._DuckDbSource(...)` as an else-branch was the shape this
    # replaced, and it fails in the one direction that matters — a new
    # source-scanned kind would be read with DuckDB's quoter and DuckDB's flat
    # statement, and the dialect-mismatch guard in `source_table` could not
    # catch it because both sides would say "duckdb".
    _SOURCE_READERS = {
        "federated": _DuckDbSource,
        "iceberg": _DuckDbSource,
        "clickhouse": _ClickHouseSource,
        "starrocks": _StarRocksSource,
    }

    def _source_reader(self, info: DatasetInfo):
        _refuse_if_needs_credentials(info)
        try:
            reader = self._SOURCE_READERS[info.kind]
        except KeyError:
            raise ValueError(
                f"No source reader is registered for dataset kind "
                f"{info.kind!r} ({info.name!r}). Add one to "
                "DatasetCatalog._SOURCE_READERS — reading it with another "
                "engine's reader would render its policy in the wrong dialect."
            ) from None
        # The reader's dialect and the dataset's declared one are derived
        # independently; if they ever disagree, the policy would be compiled
        # for one engine and executed by another.
        assert reader.dialect.name == info.sql_dialect, (
            f"{info.kind!r} reads with {reader.dialect.name} but declares "
            f"{info.sql_dialect}"
        )
        return reader(info.source)

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

    def register_clickhouse(
        self, name: str, source: dict, description: str = ""
    ) -> DatasetInfo:
        """Register a read-only table scanned by embedded ClickHouse.

        Probed before it is stored, so an unreachable table fails here rather
        than at first query — and so does one whose column list comes back
        empty, since an unknown column set cannot be masked.
        """
        _validate_name(name)
        source = clickhouse.validate_source(source)
        clickhouse.columns_of(source)
        self.store.upsert_dataset(name, description)
        self.store.set_dataset_source(name, "clickhouse", source)
        info = self.store.get_dataset(name)
        assert info is not None
        return info

    def register_starrocks(
        self, name: str, source: dict, description: str = ""
    ) -> DatasetInfo:
        """Register a read-only table served by a StarRocks cluster.

        Probed before it is stored, so an unreachable table, an unreadable one
        and one holding a column type Laurelin cannot render all fail here
        rather than at first query.
        """
        _validate_name(name)
        source = starrocks.validate_source(source)
        starrocks.columns_of(source)
        self.store.upsert_dataset(name, description)
        self.store.set_dataset_source(name, "starrocks", source)
        info = self.store.get_dataset(name)
        assert info is not None
        return info

    def source_table(
        self,
        name: str,
        sql_policy_for=None,
        limit: Optional[int] = None,
    ) -> pa.Table:
        """Scan a source-backed dataset, with row/column policy applied remotely.

        The policy is compiled to the *reader's* dialect and wrapped around the
        scan, so filtering happens at the source rather than after the data
        arrives. A policy that cannot be compiled exactly is a refusal, never an
        unfiltered read.

        ``sql_policy_for=None`` means "no policy", which is deliberately
        fail-*open* and safe only because of a three-way coupling that every
        caller is part of: ``transforms/builder.py`` runs server-authored SQL as
        the system, ``rows()`` reaches here only once ``row_policy_fn`` has
        already returned None, and ``read()``'s callers apply the Arrow policy
        themselves. A new call site that omits a policy where one applies leaks
        silently — so don't add one.
        """
        info = self.store.get_dataset(name)
        if info is None or not info.scans_at_source:
            raise KeyError(f"Not a source-scanned dataset: {name!r}")

        reader = self._source_reader(info)
        try:
            schema = reader.schema()
            columns = list(schema.names)
            if not columns:
                # Not "a table with no columns" — that isn't a thing. This is
                # discovery having failed, and it must not become an unmasked
                # read: with an empty column list `decide()` skips every mask
                # (none of their columns are "present") and the renderer would
                # then have nothing left to refuse.
                raise PolicyRenderError(
                    f"Could not determine the columns of {name!r}, so its "
                    "policy cannot be placed. Refusing the read."
                )
            select_list, where, params = "*", "TRUE", []
            if sql_policy_for is not None:
                # The types travel with the names: a row filter or a hash mask
                # is a text comparison, and the renderer refuses one it cannot
                # prove this engine spells the way the Arrow path does.
                policy = sql_policy_for(
                    name,
                    columns,
                    reader.dialect,
                    {f.name: f.type for f in schema},
                )
                if policy.dialect is not reader.dialect:
                    # A policy rendered for another engine would still *look*
                    # like valid SQL here and quietly mean something else.
                    raise PolicyRenderError(
                        f"Policy for {name!r} was rendered for "
                        f"{policy.dialect.name} but the dataset is read by "
                        f"{reader.dialect.name}. Refusing the read."
                    )
                select_list, where = policy.select_list, policy.where
                params = policy.params
            return reader.run(select_list, where, params, limit)
        except federation.FederationError:
            raise
        except duckdb.Error as exc:
            # `f"Scan of {name!r} failed: {exc}"` stood here. DuckDB's
            # postgres extension echoes the offending statement in a `LINE 1:`
            # block, so a DSN inside `postgres_scan(...)` appeared *twice* in
            # that string — and nothing caught `FederationError` on
            # `GET /datasets/{name}/rows`, so it 500'd for every role the moment
            # an upstream table was renamed away. Structured, and handled.
            raise federation.FederationError(failure=Failure.from_exception(
                exc, phase=Phase.execute, driver="duckdb", subject=f"dataset:{name}",
            )) from exc
        finally:
            reader.close()

    # Kept as a name: builder.py and tests/test_federation.py call it, and
    # "federated" is still what it does for every dataset that isn't ClickHouse.
    federated_table = source_table

    def scan_for(self, name: str, plan_for=None, version: Optional[int] = None):
        """A scannable object for ``name`` with any row/column policy applied.

        Returns a lazy pyarrow Dataset (or Scanner) whenever the policy can be
        expressed as a filter/projection, so DuckDB streams it with pushdown;
        falls back to a materialized, policy-filtered Table only when a rule
        has no Arrow equivalent. Either way the caller sees exactly the rows
        and values this user may see.
        """
        info = self.store.get_dataset(name)
        if info is not None and info.scans_at_source:
            # No local Parquet parts to build a lazy Dataset over. Read the
            # table and apply the plan exactly — correct, not lazy. The
            # pushdown that matters for Iceberg happens in `iceberg_scan`,
            # which the SQL path (query/source_table) uses instead.
            table = self.read(name, version)
            plan = plan_for(name, table.schema) if plan_for is not None else None
            if plan is None:
                return table
            if not plan.lazy:
                return plan.apply(table)
            # A *lazy* plan carries a filter/projection rather than an apply()
            # callable, and this branch used to call plan.apply unconditionally
            # — a TypeError for every source-scanned dataset with a row policy,
            # federated and Iceberg included. Nothing is streamed here (the
            # table is already materialized), but running the same Arrow
            # expressions keeps this path's answer identical to the managed
            # one's rather than approximately so.
            scan = pads.dataset(table)
            if plan.filter is not None:
                scan = scan.filter(plan.filter)
            if plan.projection is not None:
                return scan.scanner(columns=plan.projection).to_table()
            return scan.to_table()
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
                if ds.is_iceberg:
                    # Laurelin owns this table, so it is registered like any
                    # other dataset — the workbench gate below exists to stop
                    # ad-hoc SQL reaching *foreign* systems, and this isn't one.
                    con.register(
                        ds.name,
                        self.source_table(ds.name, sql_policy_for=sql_policy_for),
                    )
                    continue
                if ds.scans_at_source:
                    # Everything else read at the source joins the *federated*
                    # arm, never the Iceberg one (which `continue`d above):
                    # the reader is external, and unlike Iceberg, Laurelin
                    # neither owns the data nor has a filesystem sandbox to put
                    # it behind (see laurelin/core/clickhouse.py). Written as a
                    # property rather than `is_federated or is_clickhouse` so a
                    # new kind cannot fall through to the managed branch below
                    # and be read as local Parquet parts it does not have. Both
                    # conditions below must hold, so an unpolicied registration
                    # is impossible.
                    if not federation.workbench_enabled() or sql_policy_for is None:
                        # Off by default: enabling federation must not silently
                        # widen what ad-hoc SQL can reach. Unregistered means
                        # "unknown table", the same as any dataset you can't see.
                        continue
                    con.register(
                        ds.name,
                        self.source_table(
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

    @staticmethod
    def parse_upload(path: Path, limit: Optional[int] = None) -> pa.Table:
        """Read a CSV or Parquet file into Arrow, inferring types.

        Separate from :meth:`upload_file` so the same inference can answer
        "what would importing this give me?" without creating anything —
        showing someone the schema *before* they commit to it is the
        difference between an import and a guess.
        """
        path = Path(path)
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            con = duckdb.connect()
            try:
                sql = "SELECT * FROM read_csv_auto(?)"
                if limit is not None:
                    sql += f" LIMIT {int(limit)}"
                table = con.execute(sql, [str(path)]).arrow()
            finally:
                con.close()
        elif suffix in (".parquet", ".pq"):
            table = pq.read_table(path)
            if limit is not None:
                table = table.slice(0, int(limit))
        else:
            raise ValueError(
                f"Unsupported file type {suffix!r}: expected .csv or .parquet"
            )
        if isinstance(table, pa.RecordBatchReader):
            table = table.read_all()
        return table

    def upload_file(
        self, name: str, path: Path, description: str = "", mode: str = "replace"
    ) -> DatasetVersionInfo:
        """Ingest a CSV or Parquet file as a new dataset version.

        ``mode="append"`` adds the file's rows to the existing dataset without
        rewriting it (see :meth:`append`); the default replaces it.
        """
        _validate_name(name)
        self._refuse_write_at_source(name)
        table = self.parse_upload(path)
        if mode not in ("replace", "append"):
            raise ValueError(f"Unknown upload mode {mode!r}: expected 'replace' or 'append'")
        if mode == "append":
            return self.append(name, table, source="upload", description=description)
        return self.write(name, table, source="upload", description=description)
