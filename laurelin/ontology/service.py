"""OntologyService: materialize objects (base data + edit overlay), traverse
links, and apply write-back actions.

Objects are computed in memory per request: the backing dataset's latest
version is read via duckdb, projected to declared properties, then the
recorded ObjectEdits are replayed in order (create / update / delete).
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional

import duckdb

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import (
    ActionDef,
    EditKind,
    LinkTypeDef,
    ObjectEdit,
    ObjectTypeDef,
    OntologyDef,
)

_TRUE_STRINGS = {"true", "1"}
_FALSE_STRINGS = {"false", "0"}


def _json_safe(value: Any) -> Any:
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


def _coerce_parameter(name: str, value: Any, type_name: str) -> Any:
    """Coerce a raw parameter value to its declared type; ValueError if impossible."""
    try:
        if type_name == "integer":
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, float) and not value.is_integer():
                raise ValueError  # don't silently truncate 1.5 -> 1
            if isinstance(value, str) and "." in value:
                value = float(value)
                if not value.is_integer():
                    raise ValueError
            return int(value)
        if type_name == "float":
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if type_name == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in _TRUE_STRINGS:
                    return True
                if lowered in _FALSE_STRINGS:
                    return False
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(
            f"Parameter {name!r} has invalid value {value!r} for type {type_name!r}"
        ) from None
    return value


class OntologyService:
    """Queries and actions over ontology objects backed by catalog datasets."""

    def __init__(
        self,
        workspace: Workspace,
        catalog: DatasetCatalog,
        store: MetadataStore,
        ontology: OntologyDef,
    ):
        self.workspace = workspace
        self.catalog = catalog
        self.store = store
        self.ontology = ontology

    # -- definitions ----------------------------------------------------------

    def list_object_types(self) -> list[ObjectTypeDef]:
        return list(self.ontology.object_types)

    def _require_object_type(self, type_name: str) -> ObjectTypeDef:
        ot = self.ontology.object_type(type_name)
        if ot is None:
            raise KeyError(f"Unknown object type: {type_name!r}")
        return ot

    # -- materialization --------------------------------------------------------

    def _base_rows(self, ot: ObjectTypeDef) -> list[dict]:
        """Rows from the backing dataset's latest version, projected to the
        declared properties (plus the primary key). Empty if the dataset does
        not exist or has no versions yet."""
        try:
            glob = self.catalog.parquet_glob(ot.backing_dataset)
        except KeyError:
            return []
        con = duckdb.connect()
        try:
            cur = con.execute("SELECT * FROM read_parquet(?)", [glob])
            columns = [d[0] for d in cur.description]
            data = cur.fetchall()
        finally:
            con.close()
        keep = set(ot.properties) | {ot.primary_key}
        return [
            {col: _json_safe(val) for col, val in zip(columns, row) if col in keep}
            for row in data
        ]

    def _materialize(self, ot: ObjectTypeDef) -> list[dict]:
        """Base rows with the edit overlay applied, in stable order."""
        keep = set(ot.properties) | {ot.primary_key}
        objects: dict[str, dict] = {}
        for row in self._base_rows(ot):
            objects[str(row.get(ot.primary_key))] = row
        for edit in self.store.list_object_edits(ot.api_name):
            if edit.kind == EditKind.create:
                payload = {k: v for k, v in edit.payload.items() if k in keep}
                pk = str(payload.get(ot.primary_key, edit.pk_value))
                objects[pk] = payload
            elif edit.kind == EditKind.update:
                target = objects.get(edit.pk_value)
                if target is not None:
                    target.update(
                        {k: v for k, v in edit.payload.items() if k in keep}
                    )
            elif edit.kind == EditKind.delete:
                objects.pop(edit.pk_value, None)
        result = []
        for pk, row in objects.items():
            obj = dict(row)
            obj["__pk"] = pk
            obj["__title"] = ot.title_for(row)
            result.append(obj)
        return result

    # -- queries ----------------------------------------------------------------

    def query(
        self,
        type_name: str,
        search: Optional[str] = None,
        filters: Optional[dict[str, str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        ot = self._require_object_type(type_name)
        objects = self._materialize(ot)

        if search:
            needle = search.lower()
            string_props = [
                name for name, prop in ot.properties.items() if prop.type == "string"
            ]
            objects = [
                o
                for o in objects
                if any(
                    isinstance(o.get(p), str) and needle in o[p].lower()
                    for p in string_props
                )
            ]
        if filters:
            declared = set(ot.properties) | {ot.primary_key}
            for prop, value in filters.items():
                if prop not in declared:
                    raise ValueError(
                        f"Unknown filter property {prop!r} for object type {type_name!r}"
                    )
                objects = [
                    o
                    for o in objects
                    if o.get(prop) is not None and str(o.get(prop)) == str(value)
                ]

        total = len(objects)
        offset = max(0, offset)
        limit = max(0, limit)
        return {"objects": objects[offset : offset + limit], "total": total}

    def get(self, type_name: str, pk: str) -> Optional[dict]:
        ot = self._require_object_type(type_name)
        pk = str(pk)
        for obj in self._materialize(ot):
            if obj["__pk"] == pk:
                return obj
        return None

    def linked(self, type_name: str, pk: str, link_name: str) -> list[dict]:
        ot = self._require_object_type(type_name)
        link = self.ontology.link_type(link_name)
        if link is None:
            raise KeyError(f"Unknown link type: {link_name!r}")
        if link.from_type == ot.api_name:
            my_prop, other_type_name, other_prop = (
                link.from_property,
                link.to_type,
                link.to_property,
            )
        elif link.to_type == ot.api_name:
            my_prop, other_type_name, other_prop = (
                link.to_property,
                link.from_type,
                link.from_property,
            )
        else:
            raise KeyError(
                f"Link {link_name!r} does not involve object type {type_name!r}"
            )
        obj = self.get(type_name, pk)
        if obj is None:
            return []
        other = self._require_object_type(other_type_name)
        my_value = obj.get(my_prop)
        if my_value is None:
            return []  # a null join key links to nothing, not to other nulls
        key = str(my_value)
        return [
            o
            for o in self._materialize(other)
            if o.get(other_prop) is not None and str(o.get(other_prop)) == key
        ]

    def edits(self, type_name: str) -> list[ObjectEdit]:
        return self.store.list_object_edits(type_name)

    # -- actions ------------------------------------------------------------------

    def apply_action(
        self,
        action_name: str,
        pk: Optional[str],
        parameters: dict,
        actor: str = "anonymous",
    ) -> ObjectEdit:
        action = self.ontology.action(action_name)
        if action is None:
            raise ValueError(f"Unknown action: {action_name!r}")
        ot = self.ontology.object_type(action.object_type)
        if ot is None:
            raise ValueError(
                f"Action {action_name!r} targets unknown object type "
                f"{action.object_type!r}"
            )
        parameters = dict(parameters or {})

        for name in parameters:
            if name not in action.parameters:
                raise ValueError(
                    f"Unknown parameter {name!r} for action {action_name!r}"
                )
        for name, pdef in action.parameters.items():
            if pdef.required and (name not in parameters or parameters[name] is None):
                raise ValueError(
                    f"Missing required parameter {name!r} for action {action_name!r}"
                )

        payload = {
            name: _coerce_parameter(name, value, action.parameters[name].type)
            for name, value in parameters.items()
        }

        declared = set(ot.properties) | {ot.primary_key}
        for name in payload:
            if name not in declared:
                raise ValueError(
                    f"Parameter {name!r} is not a declared property of object type "
                    f"{ot.api_name!r}"
                )

        kind = EditKind(action.kind.value)
        if kind == EditKind.create:
            if ot.primary_key not in payload or payload[ot.primary_key] is None:
                raise ValueError(
                    f"Create action {action_name!r} requires a value for primary key "
                    f"{ot.primary_key!r} in parameters"
                )
            pk_value = str(payload[ot.primary_key])
        else:
            if pk is None:
                raise ValueError(
                    f"Action {action_name!r} ({kind.value}) requires a pk"
                )
            pk_value = str(pk)
            if self.get(ot.api_name, pk_value) is None:
                raise ValueError(
                    f"No {ot.api_name!r} object with primary key {pk_value!r}"
                )

        edit = ObjectEdit(
            id=uuid.uuid4().hex,
            object_type=ot.api_name,
            pk_value=pk_value,
            kind=kind,
            payload=payload,
            actor=actor,
        )
        self.store.add_object_edit(edit)
        self.store.log_audit(
            "action_applied",
            {
                "action": action_name,
                "object_type": ot.api_name,
                "pk_value": pk_value,
                "kind": kind.value,
                "edit_id": edit.id,
                "parameters": payload,
            },
            actor=actor,
        )
        return edit
