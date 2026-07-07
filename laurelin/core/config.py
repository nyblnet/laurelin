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
        ws.root.mkdir(parents=True, exist_ok=True)
        for d in (ws.data_dir, ws.pipelines_dir, ws.ontology_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not ws.marker_path.exists():
            ws.marker_path.write_text(
                yaml.safe_dump(
                    {"name": name or ws.root.name, "description": description},
                    sort_keys=False,
                )
            )
        return ws

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
