"""Load ontology definitions from a workspace's ontology/*.yml files.

All YAML files in the ontology directory are merged (sorted by filename) into
a single :class:`OntologyDef`. Each file may contain any subset of the keys
``object_types``, ``link_types``, ``actions``, each a list of definitions.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from laurelin.core.models import ActionDef, LinkTypeDef, ObjectTypeDef, OntologyDef


def _check_duplicates(kind: str, api_names: list[str]) -> None:
    seen: set[str] = set()
    for name in api_names:
        if name in seen:
            raise ValueError(f"Duplicate {kind} api_name in ontology: {name!r}")
        seen.add(name)


def load_ontology(ontology_dir: Path) -> OntologyDef:
    """Merge every *.yml / *.yaml file in ``ontology_dir`` into one OntologyDef.

    Missing directory or missing sections are tolerated (empty ontology).
    Duplicate api_names within a category raise ``ValueError``.
    """
    ontology_dir = Path(ontology_dir)
    object_types: list[ObjectTypeDef] = []
    link_types: list[LinkTypeDef] = []
    actions: list[ActionDef] = []

    if ontology_dir.is_dir():
        files = sorted(
            p for p in ontology_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".yml", ".yaml")
        )
        for path in files:
            doc = yaml.safe_load(path.read_text()) or {}
            if not isinstance(doc, dict):
                raise ValueError(f"Ontology file {path} must contain a mapping")
            for raw in doc.get("object_types") or []:
                object_types.append(ObjectTypeDef.model_validate(raw))
            for raw in doc.get("link_types") or []:
                link_types.append(LinkTypeDef.model_validate(raw))
            for raw in doc.get("actions") or []:
                actions.append(ActionDef.model_validate(raw))

    _check_duplicates("object type", [o.api_name for o in object_types])
    _check_duplicates("link type", [lt.api_name for lt in link_types])
    _check_duplicates("action", [a.api_name for a in actions])

    return OntologyDef(
        object_types=object_types, link_types=link_types, actions=actions
    )
