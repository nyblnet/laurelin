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
from typing import Any, Optional

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

        version = self.store.next_version(name)
        dataset_dir = self.workspace.data_dir / name
        dataset_dir.mkdir(parents=True, exist_ok=True)
        final_dir = dataset_dir / f"v{version:04d}"
        if final_dir.exists():
            raise ValueError(
                f"Version directory already exists: {final_dir} (versions are immutable)"
            )

        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f".tmp-{name}-v{version:04d}-", dir=self.workspace.data_dir)
        )
        try:
            pq.write_table(table, tmp_dir / "data.parquet")
            os.rename(tmp_dir, final_dir)
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

    def rows(
        self,
        name: str,
        limit: int = 100,
        offset: int = 0,
        version: Optional[int] = None,
    ) -> list[dict]:
        """Page of rows as JSON-safe dicts, via duckdb."""
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
