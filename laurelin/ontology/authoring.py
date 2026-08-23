"""Governed write path for ontology definition files.

The ontology is YAML on disk: ``load_ontology(workspace.ontology_dir)`` merges
every ``ontology/*.yml`` per request (loader.py), so a written definition is
live on the next request with no restart. This module is the mechanism behind
the ADMIN-gated routes in ``laurelin/api/ontology_def_routes.py`` — the role
gate, audit and R2 all run in the route; the file is mechanism, exactly as
Parquet files are mechanism behind the dataset routes. Nothing outside those
routes should call this.

Managed layout: one file per definition, ``ontology/<api_name>.yml``, holding
a single-element list under its kind key. Hand-written files (multi-definition
files, demo output) are never touched. The load-bearing rule: a duplicate
``api_name`` makes ``load_ontology`` raise on EVERY subsequent request —
bricking the whole ontology surface for the workspace — so the prospective
merged ontology is checked for duplicates BEFORE anything touches disk, and a
name owned by a file other than its own managed path is a conflict, not an
overwrite.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Union

import yaml

from laurelin.core.models import ActionDef, LinkTypeDef, ObjectTypeDef
from laurelin.ontology.loader import load_ontology

DefinitionModel = Union[ObjectTypeDef, LinkTypeDef, ActionDef]

# kind -> the YAML key its definitions live under (loader.py's vocabulary).
KIND_KEYS = {
    "object_type": "object_types",
    "link_type": "link_types",
    "action": "actions",
}


class DefinitionConflict(Exception):
    """The api_name is owned by a file this API does not manage (or the
    managed file holds something other than the single definition we wrote).
    Routes map this to 409. The message names only the *filename* inside the
    ontology directory, never a server path."""


def managed_path(ontology_dir: Path, api_name: str) -> Path:
    return Path(ontology_dir) / f"{api_name}.yml"


def _yaml_files(ontology_dir: Path) -> list[Path]:
    ontology_dir = Path(ontology_dir)
    if not ontology_dir.is_dir():
        return []
    return sorted(
        p for p in ontology_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".yml", ".yaml")
    )


def _names_in(path: Path, key: str) -> list[str]:
    """The api_names ``path`` defines under ``key``. A file that fails to parse
    is reported as owning nothing — the loader will refuse it with its own
    message on the next request; this module only decides file ownership."""
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    return [
        str(raw.get("api_name", ""))
        for raw in doc.get(key) or []
        if isinstance(raw, dict)
    ]


def _owner_of(ontology_dir: Path, key: str, api_name: str,
              exclude: Optional[Path] = None) -> Optional[Path]:
    for path in _yaml_files(ontology_dir):
        if exclude is not None and path == exclude:
            continue
        if api_name in _names_in(path, key):
            return path
    return None


def _is_managed(path: Path, key: str, api_name: str) -> bool:
    """True only when ``path`` holds exactly what upsert writes: one document
    with one kind key and one definition of ``api_name``. A hand-written file
    that happens to share the name stays hand-written."""
    if not path.is_file():
        return False
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict) or set(doc) != {key}:
        return False
    defs = doc.get(key) or []
    return (
        len(defs) == 1
        and isinstance(defs[0], dict)
        and defs[0].get("api_name") == api_name
    )


def upsert_definition(ontology_dir: Path, kind: str, model: DefinitionModel) -> Path:
    """Write ``model`` as the managed file for its api_name, atomically, with
    the whole-ontology postcondition checked (and rolled back) on this side of
    the response."""
    key = KIND_KEYS[kind]
    ontology_dir = Path(ontology_dir)
    ontology_dir.mkdir(parents=True, exist_ok=True)
    api_name = model.api_name
    path = managed_path(ontology_dir, api_name)

    # Refuse before touching disk: a name defined in any other file would merge
    # into a duplicate that breaks load_ontology for every request after this
    # one. The fix belongs in that file, and the message says which one.
    owner = _owner_of(ontology_dir, key, api_name, exclude=path)
    if owner is not None:
        raise DefinitionConflict(
            f"{kind} {api_name!r} is already defined in ontology file "
            f"{owner.name!r}; edit that file instead"
        )
    # The managed filename may be claimed by a hand-written file or by a
    # different kind reusing the name; overwriting either would silently
    # destroy someone's definition.
    if path.is_file() and not _is_managed(path, key, api_name):
        raise DefinitionConflict(
            f"Ontology file {path.name!r} exists but is not managed by this "
            f"API; edit that file instead"
        )

    payload = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    text = yaml.safe_dump({key: [payload]}, sort_keys=False)

    previous = path.read_text() if path.is_file() else None
    fd, tmp = tempfile.mkstemp(dir=ontology_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    # Postcondition: the merged ontology still loads. The pre-write duplicate
    # check makes this unreachable in the ordinary case, but "the workspace's
    # every ontology request now 500s" is the failure this path must never
    # leave behind, so verify and roll back rather than trust.
    try:
        load_ontology(ontology_dir)
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous)
        raise RuntimeError(
            f"Writing {kind} {api_name!r} left the ontology unloadable; "
            "the write was rolled back"
        ) from None
    return path


def delete_definition(ontology_dir: Path, kind: str, api_name: str) -> None:
    """Remove the managed file for ``api_name``. Refuses (DefinitionConflict)
    to touch a definition living in a hand-written file; KeyError when the
    definition does not exist anywhere."""
    key = KIND_KEYS[kind]
    ontology_dir = Path(ontology_dir)
    path = managed_path(ontology_dir, api_name)
    if _is_managed(path, key, api_name):
        path.unlink()
        return
    owner = _owner_of(ontology_dir, key, api_name)
    if owner is not None:
        raise DefinitionConflict(
            f"{kind} {api_name!r} is defined in hand-written ontology file "
            f"{owner.name!r}; edit that file instead"
        )
    raise KeyError(f"Unknown {kind}: {api_name!r}")
