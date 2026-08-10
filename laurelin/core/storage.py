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
import re
import uuid
from pathlib import Path
from typing import Iterable, Optional

import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from laurelin.core.fileperms import mkdir_private

# Object-store URI schemes pyarrow can address natively.
_REMOTE_SCHEMES = ("s3", "gs", "gcs", "abfs", "abfss", "az")

# Schemes that name an object store this build cannot address, plus the
# near-misses of the ones it can. They exist as a *refusal* list rather than
# being silently treated as directory names — see :meth:`Storage.for_uri`.
_UNSUPPORTED_SCHEMES = (
    "s3a", "s3n", "wasb", "wasbs", "adl", "hdfs", "http", "https", "ftp", "oss",
    "cos", "obs", "swift", "b2", "r2", "minio",
)

# Anything of the form `scheme:` — including the one-slash `s3:/bucket` typo,
# which is not a URI at all but is unmistakably a botched attempt at one.
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):(//)?")


class UnsupportedDataURI(ValueError):
    """A ``LAURELIN_DATA_URI`` naming a scheme this build cannot address."""


def is_remote_uri(uri: str) -> bool:
    """Whether `uri` names an object store rather than a local directory.

    Case-insensitive, because ``S3://bucket/prefix`` is an object store URI
    that an operator typed and a filesystem path to nobody. This used to be
    ``str.startswith`` against a fixed tuple of lowercase ``scheme://``
    prefixes, so ``S3://``, ``s3:/`` and ``s3a://`` were all judged *local* and
    quietly became directories named ``S3:/bucket/prefix`` relative to the
    process CWD — 0755, with the governed Parquet 0644 inside, on the
    container's own disk, while the operator believed the data was in a bucket
    behind a bucket policy. Nothing logged, warned or failed, and the data
    vanished with the container.
    """
    match = _SCHEME_RE.match(str(uri))
    return (
        match is not None
        and bool(match.group(2))
        and match.group(1).lower() in _REMOTE_SCHEMES
    )


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
        picking up credentials from the usual environment. A path with no
        scheme is a local directory.

        **A scheme this build cannot address is an error, not a directory
        name.** It used to fall through to the local branch, which meant a
        typo in ``LAURELIN_DATA_URI`` — ``s3a://``, ``S3://``, ``s3:/`` —
        created a directory literally called ``s3a:/bucket/prefix`` under the
        process CWD and wrote every governed Parquet into it at 0644, while
        the operator believed the data was in S3. A misconfiguration that
        silently relocates the data plane out of its bucket and onto a
        container's ephemeral local disk has to fail at startup, loudly, with
        the URI in the message.

        The local directory is created with :func:`fileperms.mkdir_private`,
        not a bare ``mkdir``: a ``LAURELIN_DATA_URI`` pointing at a local path
        takes the data plane *outside* the 0700 workspace root, and it came
        out 0755 with 0644 Parquet inside — the same exposure ``core/config``
        creates ``data/`` privately to prevent, one directory over.
        """
        text = str(uri)
        match = _SCHEME_RE.match(text)
        if match is not None:
            scheme = match.group(1).lower()
            if scheme in _REMOTE_SCHEMES and match.group(2):
                # Lowercased for pyarrow, which does not accept `S3://`.
                normalized = scheme + text[len(match.group(1)):]
                filesystem, path = pafs.FileSystem.from_uri(normalized)
                return cls(filesystem, path, normalized)
            raise UnsupportedDataURI(
                f"Cannot use {text!r} as a data location: {match.group(1)!r} is "
                f"not an object-store scheme this build can address "
                f"({', '.join(s + '://' for s in _REMOTE_SCHEMES)}). Left as "
                f"it is, this would become a local directory of that name and "
                f"every dataset would be written to this host's disk instead "
                f"of the store."
            )
        root = Path(text).resolve()
        mkdir_private(root)
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

    def size(self, key: str) -> int:
        """Byte size of a part, or -1 when it is not there.

        A tar member header needs the size *before* the bytes, and a workspace
        export must not learn it by reading the part into memory first.
        """
        info = self.fs.get_file_info(self.resolve(key))
        if info.type == pafs.FileType.NotFound:
            return -1
        return int(info.size or 0)

    def open_input_stream(self, key: str):
        """A raw byte stream over one part.

        The export copies parts through this rather than ``read_table``: a
        terabyte dataset then costs one buffer instead of a terabyte of RAM,
        and the bytes that land in the archive are the bytes on disk rather
        than a re-encoding of them.
        """
        return self.fs.open_input_stream(self.resolve(key))

    def open_output_stream(self, key: str):
        """A raw byte sink for one part, creating its parent."""
        target = self.resolve(key)
        self._ensure_parent(target)
        return self.fs.open_output_stream(target)

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
