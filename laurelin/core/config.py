"""Workspace: the single directory that holds everything Laurelin knows.

A workspace is identified by a `laurelin.yml` marker file at its root:

    <root>/
      laurelin.yml
      metadata.db
      data/<dataset>/v<NNNN>/data.parquet
      pipelines/*.py
      ontology/*.yml
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from laurelin.core.fileperms import PRIVATE_FILE, mkdir_private

MARKER = "laurelin.yml"


class WorkspaceNotFound(Exception):
    pass


class Workspace:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self._config: Optional[dict] = None

    # -- layout -------------------------------------------------------------

    @property
    def marker_path(self) -> Path:
        return self.root / MARKER

    @property
    def metadata_path(self) -> Path:
        return self.root / "metadata.db"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def pipelines_dir(self) -> Path:
        return self.root / "pipelines"

    @property
    def ontology_dir(self) -> Path:
        return self.root / "ontology"

    # -- config -------------------------------------------------------------

    @property
    def config(self) -> dict:
        if self._config is None:
            if self.marker_path.exists():
                self._config = yaml.safe_load(self.marker_path.read_text()) or {}
            else:
                self._config = {}
        return self._config

    @property
    def name(self) -> str:
        return self.config.get("name", self.root.name)

    @property
    def description(self) -> str:
        return self.config.get("description", "")

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def init(cls, root: Path | str, name: str = "", description: str = "") -> "Workspace":
        """Create a new workspace directory structure (idempotent)."""
        ws = cls(root)
        # 0700 on a workspace root we create. The mode of a directory the
        # operator already made is a decision the operator already took, so
        # mkdir_private leaves those alone — see its docstring.
        mkdir_private(ws.root)
        # The subdirectories, at 0700 as well, and for a reason the 0700 root
        # does not already cover: the root is only 0700 when *Laurelin* made
        # it. Under a root the operator provisioned — an upgraded workspace, or
        # the Dockerfile's `RUN mkdir -p /data` at 0755 — these three inherited
        # `0777 & ~umask` and became the real exposure. Measured at umask 022:
        # every dataset Parquet 0644 inside 0755 directories, so any local user
        # or any container sharing the volume reads every governed dataset with
        # no row policy and no column masks. At umask 000 `pipelines/` came out
        # 0777, and `transforms.api.collect_transforms` exec()s what it finds
        # there — an attacker-authored file in that directory ran.
        for d in (ws.data_dir, ws.pipelines_dir, ws.ontology_dir):
            mkdir_private(d)
        if not ws.marker_path.exists():
            ws._write_marker(name or ws.root.name, description)
        return ws

    def _write_marker(self, name: str, description: str) -> None:
        """Write laurelin.yml at 0600 from creation.

        Created private even though today it carries only a name and a
        description: it is the one file every workspace has, it is where any
        future workspace-level setting (a data URI, a catalog endpoint) would
        land, and a config file that is private from its first version never
        has to be retro-fixed once it stops being boring. Existing markers are
        *not* re-moded — Laurelin only reaches into files it did not create
        when it can point at a secret inside them, and here it cannot.
        """
        body = yaml.safe_dump({"name": name, "description": description}, sort_keys=False)
        fd = os.open(self.marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)

    @classmethod
    def find(cls, path: Optional[Path | str] = None) -> "Workspace":
        """Locate a workspace: explicit path > $LAURELIN_WORKSPACE > walk up from cwd."""
        if path is not None:
            ws = cls(path)
            if ws.marker_path.exists():
                return ws
            raise WorkspaceNotFound(f"No {MARKER} in {ws.root}")
        env = os.environ.get("LAURELIN_WORKSPACE")
        if env:
            return cls.find(env)
        cur = Path.cwd().resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / MARKER).exists():
                return cls(candidate)
        raise WorkspaceNotFound(
            f"No {MARKER} found in {cur} or its parents. "
            "Run `laurelin init <dir>` or pass --workspace."
        )
