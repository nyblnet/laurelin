"""Workspace data storage — a local directory or an object store.

All dataset bytes go through this layer, so the catalog never calls
``open()``, ``os.rename`` or ``Path.glob`` directly. That indirection is what
lets a workspace's Parquet live in S3/GCS/Azure instead of on a mounted
volume, which in turn is what makes the data plane stateless.

**Keys, not paths.** Callers address parts by workspace-relative key
(``data/orders/parts/ab12….parquet``). The storage resolves those against its
base — a local directory or a bucket prefix.

**Committing a version.** Object stores have no atomic directory rename, so
the old "write a temp dir, rename it into place" trick doesn't port. It
doesn't need to: a version is already a *manifest* row in the metadata
database, so that row's insert is the commit point. Parts are written to
unique keys first and referenced afterwards. A crash before the insert leaves
an unreferenced part (garbage, collectable) — never a registered-but-missing
version, which is the failure the rename was protecting against.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterable, Optional

import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.fs as pafs
import pyarrow.parquet as pq

# Object-store URI schemes pyarrow can address natively.
_REMOTE_SCHEMES = ("s3://", "gs://", "gcs://", "abfs://", "abfss://", "az://")


def is_remote_uri(uri: str) -> bool:
    return str(uri).startswith(_REMOTE_SCHEMES)


class Storage:
    """Bytes for one workspace, addressed by workspace-relative key."""

    def __init__(self, filesystem: pafs.FileSystem, base: str, uri: str):
        self.fs = filesystem
        self.base = base.rstrip("/")
        self.uri = uri

    # -- construction ---------------------------------------------------------

    @classmethod
    def for_uri(cls, uri: str | Path) -> "Storage":
        """Build storage for a base URI.

        ``s3://bucket/prefix`` (and gs/abfs) use pyarrow's native filesystems,
        picking up credentials from the usual environment. Anything else is
        treated as a local directory.
        """
        text = str(uri)
        if is_remote_uri(text):
            filesystem, path = pafs.FileSystem.from_uri(text)
            return cls(filesystem, path, text)
        root = Path(text).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return cls(pafs.LocalFileSystem(), str(root), str(root))

    @classmethod
    def for_workspace(cls, workspace) -> "Storage":
        """Storage for a workspace: its local directory unless a data URI is
        configured (``LAURELIN_DATA_URI``), in which case the workspace's data
        lives under ``<uri>/<workspace name>``."""
        configured = os.environ.get("LAURELIN_DATA_URI")
        if configured:
            return cls.for_uri(f"{configured.rstrip('/')}/{workspace.root.name}")
        return cls.for_uri(workspace.root)

    @property
    def is_remote(self) -> bool:
        return is_remote_uri(self.uri)

    # -- keys -----------------------------------------------------------------

    def resolve(self, key: str) -> str:
        return f"{self.base}/{str(key).lstrip('/')}"

    @staticmethod
    def new_part_key(dataset: str, suffix: str = "parquet") -> str:
        """A unique key for a new part. Uniqueness is what removes the need for
        any cross-writer coordination in storage: two writers racing produce
        two distinct parts, and the metadata database decides which version
        references which."""
        return f"data/{dataset}/parts/{uuid.uuid4().hex}.{suffix}"

    # -- reading --------------------------------------------------------------

    def dataset(self, keys: Iterable[str]) -> pads.Dataset:
        """A lazy pyarrow Dataset over the given parts."""
        return pads.dataset(
            [self.resolve(k) for k in keys], format="parquet", filesystem=self.fs
        )

    def read_table(self, keys: Iterable[str]) -> pa.Table:
        return self.dataset(keys).to_table()

    def exists(self, key: str) -> bool:
        info = self.fs.get_file_info(self.resolve(key))
        return info.type != pafs.FileType.NotFound

    def list_keys(self, prefix: str) -> list[str]:
        """Workspace-relative keys directly under a prefix.

        Deliberately **not** recursive: its only caller is the fallback for
        pre-manifest versions, whose files sat directly in a version
        directory. Recursing would let that fallback sweep up every part of
        every version once parts moved into a shared ``parts/`` directory.
        """
        selector = pafs.FileSelector(self.resolve(prefix), allow_not_found=True, recursive=False)
        base = self.base + "/"
        out = []
        for info in self.fs.get_file_info(selector):
            if info.type == pafs.FileType.File and info.path.startswith(base):
                out.append(info.path[len(base):])
        return sorted(out)

    # -- writing --------------------------------------------------------------

    def write_table(self, table: pa.Table, key: str) -> None:
        target = self.resolve(key)
        self._ensure_parent(target)
        pq.write_table(table, target, filesystem=self.fs)

    def writer(self, key: str, schema: pa.Schema) -> pq.ParquetWriter:
        """An incremental writer, for streaming a delta out in batches."""
        target = self.resolve(key)
        self._ensure_parent(target)
        return pq.ParquetWriter(target, schema, filesystem=self.fs)

    def delete(self, key: str) -> None:
        try:
            self.fs.delete_file(self.resolve(key))
        except (FileNotFoundError, OSError):
            pass  # already gone, or never created — cleanup is best-effort

    def _ensure_parent(self, target: str) -> None:
        parent = target.rsplit("/", 1)[0]
        if parent:
            self.fs.create_dir(parent, recursive=True)


def storage_for(workspace, override: Optional[str] = None) -> Storage:
    return Storage.for_uri(override) if override else Storage.for_workspace(workspace)
