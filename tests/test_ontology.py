"""Tests for laurelin.ontology: loader, materialization, queries, links, actions."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Cardinality, EditKind
from laurelin.ontology import OntologyService, load_ontology

AIRCRAFT_YAML = """\
object_types:
  - api_name: aircraft
    display_name: Aircraft
    backing_dataset: aircraft
    primary_key: tail_number
    title_property: model
    properties:
      tail_number: {type: string}
      model: {type: string}
      operator: {type: string}
      status: {type: string}
      year_built: {type: integer}
actions:
  - api_name: update_aircraft_status
    object_type: aircraft
    kind: update
    parameters:
      status: {type: string, required: true}
  - api_name: add_aircraft
    object_type: aircraft
    kind: create
    parameters:
      tail_number: {type: string, required: true}
      model: {type: string, required: true}
      status: {type: string}
  - api_name: retire_aircraft
    object_type: aircraft
    kind: delete
"""

FLIGHTS_YAML = """\
object_types:
  - api_name: flight
    backing_dataset: flights
    primary_key: flight_id
    properties:
      flight_id: {type: string}
      tail_number: {type: string}
      origin: {type: string}
      delay_minutes: {type: integer}
link_types:
  - api_name: aircraft_flights
    from: aircraft
    to: flight
    cardinality: one_to_many
    from_property: tail_number
    to_property: tail_number
"""


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    return Workspace.init(tmp_path / "ws", name="test")


@pytest.fixture()
def store(ws: Workspace) -> MetadataStore:
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def catalog(ws: Workspace, store: MetadataStore) -> DatasetCatalog:
    return DatasetCatalog(ws, store)


def write_ontology(ws: Workspace) -> None:
    (ws.ontology_dir / "aircraft.yml").write_text(AIRCRAFT_YAML)
    (ws.ontology_dir / "flights.yaml").write_text(FLIGHTS_YAML)


def seed_data(catalog: DatasetCatalog) -> None:
    catalog.write(
        "aircraft",
        pa.table(
            {
                "tail_number": ["N100", "N200", "N300"],
                "model": ["A320", "B737", "A320"],
                "operator": ["Acme Air", "Blue Sky", "Acme Air"],
                "status": ["active", "active", "maintenance"],
                "year_built": [2010, 2015, 2008],
                "secret_column": ["x", "y", "z"],
            }
        ),
    )
    catalog.write(
        "flights",
        pa.table(
            {
                "flight_id": ["F1", "F2", "F3", "F4"],
                "tail_number": ["N100", "N100", "N200", "N999"],
                "origin": ["SFO", "LAX", "JFK", "SFO"],
                "delay_minutes": [5, 0, 42, 12],
            }
        ),
    )


@pytest.fixture()
def service(ws: Workspace, catalog: DatasetCatalog, store: MetadataStore) -> OntologyService:
    write_ontology(ws)
    seed_data(catalog)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


# -- loader --------------------------------------------------------------------


def test_load_ontology_merges_files(ws: Workspace):
    write_ontology(ws)
    onto = load_ontology(ws.ontology_dir)
    assert {o.api_name for o in onto.object_types} == {"aircraft", "flight"}
    assert [a.api_name for a in onto.actions] == [
        "update_aircraft_status",
        "add_aircraft",
        "retire_aircraft",
    ]
    link = onto.link_type("aircraft_flights")
    assert link is not None
    assert link.from_type == "aircraft"
    assert link.to_type == "flight"
    assert link.cardinality == Cardinality.one_to_many


def test_load_ontology_missing_dir_and_sections(tmp_path: Path):
    onto = load_ontology(tmp_path / "nowhere")
    assert onto.object_types == [] and onto.link_types == [] and onto.actions == []
    d = tmp_path / "onto"
    d.mkdir()
    (d / "empty.yml").write_text("")
    (d / "links_only.yml").write_text(
        "link_types:\n"
        "  - {api_name: l, from: a, to: b, from_property: x, to_property: y}\n"
    )
    onto = load_ontology(d)
    assert len(onto.link_types) == 1 and onto.object_types == []


def test_load_ontology_duplicate_api_names(ws: Workspace):
    write_ontology(ws)
    (ws.ontology_dir / "zz_dup.yml").write_text(
        "object_types:\n"
        "  - {api_name: aircraft, backing_dataset: aircraft, primary_key: tail_number}\n"
    )
    with pytest.raises(ValueError, match="aircraft"):
        load_ontology(ws.ontology_dir)


# -- materialization & overlay ---------------------------------------------------


def test_query_base_rows(service: OntologyService):
    result = service.query("aircraft")
    assert result["total"] == 3
    obj = next(o for o in result["objects"] if o["__pk"] == "N100")
    assert obj["__title"] == "A320"
    assert obj["model"] == "A320"
    assert "secret_column" not in obj  # undeclared columns are dropped


def test_query_missing_dataset_is_empty(ws, catalog, store):
    write_ontology(ws)  # no data seeded
    svc = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))
    assert svc.query("aircraft") == {"objects": [], "total": 0, "total_capped": False}


def test_query_unknown_type(service: OntologyService):
    with pytest.raises(KeyError):
        service.query("nope")


def test_overlay_create_update_delete(service: OntologyService, store: MetadataStore):
    service.apply_action(
        "add_aircraft", None,
        {"tail_number": "N400", "model": "A350", "status": "active"},
    )
    service.apply_action("update_aircraft_status", "N100", {"status": "grounded"})
    service.apply_action("retire_aircraft", "N300", {})

    result = service.query("aircraft")
    pks = {o["__pk"] for o in result["objects"]}
    assert pks == {"N100", "N200", "N400"}
    assert service.get("aircraft", "N100")["status"] == "grounded"
    assert service.get("aircraft", "N100")["model"] == "A320"  # shallow merge kept
    created = service.get("aircraft", "N400")
    assert created["model"] == "A350" and created["__title"] == "A350"
    assert service.get("aircraft", "N300") is None

    # later edit wins over earlier one
    service.apply_action("update_aircraft_status", "N100", {"status": "active"})
    assert service.get("aircraft", "N100")["status"] == "active"

    edits = service.edits("aircraft")
    assert [e.kind for e in edits] == [
        EditKind.create, EditKind.update, EditKind.delete, EditKind.update,
    ]


def test_overlay_update_delete_missing_pk_are_noops(service: OntologyService, store):
    from laurelin.core.models import ObjectEdit

    store.add_object_edit(ObjectEdit(
        id="e1", object_type="aircraft", pk_value="GHOST",
        kind=EditKind.update, payload={"status": "haunted"},
    ))
    store.add_object_edit(ObjectEdit(
        id="e2", object_type="aircraft", pk_value="GHOST2",
        kind=EditKind.delete, payload={},
    ))
    assert service.query("aircraft")["total"] == 3


# -- search / filter / paging ------------------------------------------------------


def test_search_case_insensitive_string_props(service: OntologyService):
    result = service.query("aircraft", search="acme")
    assert result["total"] == 2
    assert all("Acme" in o["operator"] for o in result["objects"])
    # search does not match non-string properties
    assert service.query("aircraft", search="2010")["total"] == 0


def test_filters_string_equality(service: OntologyService):
    result = service.query("aircraft", filters={"status": "active"})
    assert result["total"] == 2
    result = service.query("aircraft", filters={"year_built": "2010"})
    assert result["total"] == 1 and result["objects"][0]["__pk"] == "N100"
    result = service.query("aircraft", filters={"status": "active", "model": "A320"})
    assert result["total"] == 1


def test_paging_totals(service: OntologyService):
    result = service.query("aircraft", limit=2, offset=0)
    assert result["total"] == 3 and len(result["objects"]) == 2
    rest = service.query("aircraft", limit=2, offset=2)
    assert rest["total"] == 3 and len(rest["objects"]) == 1
    all_pks = {o["__pk"] for o in result["objects"]} | {o["__pk"] for o in rest["objects"]}
    assert all_pks == {"N100", "N200", "N300"}


# -- links -----------------------------------------------------------------------


def test_linked_forward(service: OntologyService):
    flights = service.linked("aircraft", "N100", "aircraft_flights")
    assert {f["__pk"] for f in flights} == {"F1", "F2"}


def test_linked_reverse(service: OntologyService):
    aircraft = service.linked("flight", "F3", "aircraft_flights")
    assert [a["__pk"] for a in aircraft] == ["N200"]


def test_linked_no_match_and_missing(service: OntologyService):
    assert service.linked("flight", "F4", "aircraft_flights") == []  # N999 not real
    assert service.linked("aircraft", "NOPE", "aircraft_flights") == []
    with pytest.raises(KeyError):
        service.linked("aircraft", "N100", "no_such_link")
    with pytest.raises(KeyError):
        service.linked("flight", "F1", "aircraft_flights_wrong")


def test_linked_sees_overlay(service: OntologyService):
    service.apply_action("retire_aircraft", "N200", {})
    assert service.linked("flight", "F3", "aircraft_flights") == []


# -- actions ------------------------------------------------------------------------


def test_action_unknown(service: OntologyService):
    with pytest.raises(ValueError, match="Unknown action"):
        service.apply_action("no_such_action", None, {})


def test_action_missing_required_param(service: OntologyService):
    with pytest.raises(ValueError, match="required parameter 'status'"):
        service.apply_action("update_aircraft_status", "N100", {})


def test_action_undeclared_param(service: OntologyService):
    with pytest.raises(ValueError, match="Unknown parameter 'bogus'"):
        service.apply_action("update_aircraft_status", "N100", {"status": "ok", "bogus": 1})


def test_action_update_missing_pk(service: OntologyService):
    with pytest.raises(ValueError, match="NXXX"):
        service.apply_action("update_aircraft_status", "NXXX", {"status": "ok"})
    with pytest.raises(ValueError, match="requires a pk"):
        service.apply_action("update_aircraft_status", None, {"status": "ok"})


def test_action_create_requires_pk_in_params(ws, catalog, store):
    write_ontology(ws)
    seed_data(catalog)
    onto = load_ontology(ws.ontology_dir)
    # make tail_number optional so we can omit it and hit the pk check
    onto.action("add_aircraft").parameters["tail_number"].required = False
    svc = OntologyService(ws, catalog, store, onto)
    with pytest.raises(ValueError, match="primary key"):
        svc.apply_action("add_aircraft", None, {"model": "A350"})


def test_action_param_coercion(service: OntologyService):
    edit = service.apply_action(
        "add_aircraft", None, {"tail_number": "N500", "model": "B787"},
    )
    assert edit.pk_value == "N500"
    obj = service.get("aircraft", "N500")
    assert obj is not None and obj["model"] == "B787"

    # integer coercion via a synthetic action on year_built
    from laurelin.core.models import ActionDef, ActionKind, ActionParameterDef

    service.ontology.actions.append(ActionDef(
        api_name="set_year", object_type="aircraft", kind=ActionKind.update,
        parameters={"year_built": ActionParameterDef(type="integer", required=True)},
    ))
    edit = service.apply_action("set_year", "N100", {"year_built": "1999"})
    assert edit.payload["year_built"] == 1999
    assert service.get("aircraft", "N100")["year_built"] == 1999
    with pytest.raises(ValueError, match="year_built"):
        service.apply_action("set_year", "N100", {"year_built": "not-a-number"})


def test_action_boolean_coercion():
    from laurelin.ontology.service import _coerce_parameter

    assert _coerce_parameter("b", "true", "boolean") is True
    assert _coerce_parameter("b", "FALSE", "boolean") is False
    assert _coerce_parameter("b", "1", "boolean") is True
    assert _coerce_parameter("b", 0, "boolean") is False
    assert _coerce_parameter("b", True, "boolean") is True
    with pytest.raises(ValueError):
        _coerce_parameter("b", "yep", "boolean")


def test_action_audit_logged(service: OntologyService, store: MetadataStore):
    edit = service.apply_action(
        "update_aircraft_status", "N100", {"status": "grounded"}, actor="alice",
    )
    events = store.list_audit()
    ev = next(e for e in events if e.action == "action_applied")
    assert ev.actor == "alice"
    assert ev.details["edit_id"] == edit.id
    assert ev.details["pk_value"] == "N100"
