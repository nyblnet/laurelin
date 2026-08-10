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

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from laurelin.core import fileperms
from laurelin.core.fileperms import PRIVATE_FILE, mkdir_private

log = logging.getLogger("laurelin.config")

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
        ws._write_marker(name or ws.root.name, description)
        ws.harden_if_strict()
        return ws

    def harden_if_strict(self) -> None:
        """Under ``LAURELIN_STRICT_FILE_MODE``, take the marker and the tree too.

        Strict mode is the remedy the admin screen and ``docs/DEPLOYMENT.md``
        tell the operator to reach for, and on an inherited workspace it did
        nothing to either of the two rows that screen renders beside
        ``metadata.db``: ``laurelin.yml`` stayed 0644 and the workspace
        directory stayed 0755, on every restart, under a card offering exactly
        those two remedies (``chmod 600 metadata.db`` — already 0600 — and
        "set ``LAURELIN_STRICT_FILE_MODE=1`` and restart", already set). Two
        permanently red rows and advice that provably does nothing is how a
        security screen teaches people to ignore it.

        The default stays as it was, and the reasons in ``fileperms`` still
        hold: a directory the operator made is a mode the operator chose. But
        an operator who sets this variable is saying the opposite about *this*
        directory, and the docs promise it happens "on every open" — so this
        runs from :meth:`find` as well as :meth:`init`, because a restart is
        the action the remediation card asks for and a restart does not call
        ``init``.
        """
        if not fileperms.strict_mode():
            return
        if self.marker_path.exists():
            fileperms.harden_existing(self.marker_path, what="workspace marker")
        for d in (self.root, self.data_dir, self.pipelines_dir, self.ontology_dir):
            if not d.is_dir():
                continue
            try:
                os.chmod(d, fileperms.PRIVATE_DIR)
            except OSError as exc:  # pragma: no cover - reported, never fatal
                log.warning("could not tighten %s: %s", d, exc)

    def _write_marker(self, name: str, description: str) -> None:
        """Write laurelin.yml at 0600 from creation.

        Created private even though today it carries only a name and a
        description: it is the one file every workspace has, it is where any
        future workspace-level setting (a data URI, a catalog endpoint) would
        land, and a config file that is private from its first version never
        has to be retro-fixed once it stops being boring. Existing markers are
        *not* re-moded — Laurelin only reaches into files it did not create
        when it can point at a secret inside them, and here it cannot. (Except
        under ``LAURELIN_STRICT_FILE_MODE``; see :meth:`init`.)

        **An existing marker is a no-op, and the check is the ``O_EXCL`` open
        itself**, not a preceding ``exists()``. It used to be the latter, in
        ``init``, and two processes calling ``Workspace.init`` on the same
        fresh root both passed it and the loser died on an unhandled
        ``FileExistsError``: 17 of 40 trials, six processes each. That is
        reachable from the front door — ``api/context._bundle`` calls
        ``Workspace.init`` lazily for a registered-but-unmaterialised
        workspace and FastAPI runs sync endpoints on a threadpool, so two
        simultaneous requests for the same slug both enter and one gets a 500
        instead of a workspace. ``create_private`` in ``fileperms`` handles the
        identical race correctly; this is the copy that never got it.
        """
        body = yaml.safe_dump({"name": name, "description": description}, sort_keys=False)
        try:
            fd = os.open(
                self.marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE
            )
        except FileExistsError:
            return  # somebody else wrote it, in this process or another
        with os.fdopen(fd, "w") as fh:
            fh.write(body)

    @classmethod
    def find(cls, path: Optional[Path | str] = None) -> "Workspace":
        """Locate a workspace: explicit path > $LAURELIN_WORKSPACE > walk up from cwd."""
        if path is not None:
            ws = cls(path)
            if ws.marker_path.exists():
                ws.harden_if_strict()
                return ws
            raise WorkspaceNotFound(f"No {MARKER} in {ws.root}")
        env = os.environ.get("LAURELIN_WORKSPACE")
        if env:
            return cls.find(env)
        cur = Path.cwd().resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / MARKER).exists():
                ws = cls(candidate)
                ws.harden_if_strict()
                return ws
        raise WorkspaceNotFound(
            f"No {MARKER} found in {cur} or its parents. "
            "Run `laurelin init <dir>` or pass --workspace."
        )
