"""CLI + demo generator tests (typer CliRunner over tmp_path workspaces)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from laurelin.catalog import DatasetCatalog
from laurelin.cli import app
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.demo import create_demo

runner = CliRunner()


def _engine(root: Path) -> tuple[Workspace, MetadataStore, DatasetCatalog]:
    ws = Workspace(root)
    store = MetadataStore(ws.metadata_path)
    return ws, store, DatasetCatalog(ws, store)


# -- init ---------------------------------------------------------------------

def test_init_creates_workspace_structure(tmp_path):
    root = tmp_path / "ws"
    result = runner.invoke(
        app, ["init", str(root), "--name", "myws", "--description", "test space"]
    )
    assert result.exit_code == 0, result.output
    assert (root / "laurelin.yml").exists()
    assert (root / "data").is_dir()
    assert (root / "pipelines").is_dir()
    assert (root / "ontology").is_dir()
    ws = Workspace(root)
    assert ws.name == "myws"
    assert ws.description == "test space"


# -- demo -----------------------------------------------------------------------

def test_demo_no_build_creates_raw_datasets_and_files(tmp_path):
    root = tmp_path / "demo"
    result = runner.invoke(app, ["demo", str(root), "--no-build"])
    assert result.exit_code == 0, result.output

    _, store, catalog = _engine(root)
    names = {d.name for d in store.list_datasets()}
    assert names == {"raw_aircraft", "raw_flights"}
    assert catalog.read("raw_aircraft").num_rows == 12
    assert catalog.read("raw_flights").num_rows == 60
    assert store.get_version("raw_aircraft").version == 1
    assert store.get_version("raw_flights").version == 1
    assert (root / "pipelines" / "aviation.py").exists()
    assert (root / "ontology" / "aviation.yml").exists()


def test_demo_default_builds_pipeline(tmp_path):
    root = tmp_path / "demo"
    result = runner.invoke(app, ["demo", str(root)])
    assert result.exit_code == 0, result.output

    _, store, catalog = _engine(root)
    names = {d.name for d in store.list_datasets()}
    assert {"clean_aircraft", "clean_flights", "flight_stats"} <= names

    assert catalog.read("clean_aircraft").num_rows == 12
    # 60 raw flights minus 4 malformed rows
    assert catalog.read("clean_flights").num_rows == 56
    stats = catalog.read("flight_stats")
    assert stats.num_rows == 12
    assert {"tail_number", "model", "flight_count", "avg_delay_minutes"} <= set(
        stats.column_names
    )

    clean = catalog.read("clean_flights")
    tails = clean.column("tail_number").to_pylist()
    assert all(t is not None for t in tails)
    delays = clean.column("delay_minutes").to_pylist()
    assert all(d is None or d >= -60 for d in delays)


def test_demo_ontology_is_loadable(tmp_path):
    root = tmp_path / "demo"
    create_demo(root, build=False)
    from laurelin.ontology import load_ontology

    ontology = load_ontology(root / "ontology")
    assert {o.api_name for o in ontology.object_types} == {"aircraft", "flight"}
    assert {l.api_name for l in ontology.link_types} == {"aircraft_flights"}
    assert {a.api_name for a in ontology.actions} == {
        "update_aircraft_status",
        "cancel_flight",
        "add_aircraft",
    }
    # Every action payload key must be a declared property of its object type.
    for action in ontology.actions:
        ot = ontology.object_type(action.object_type)
        declared = set(ot.properties) | {ot.primary_key}
        assert set(action.parameters) <= declared


# -- build ------------------------------------------------------------------------

def test_build_command_on_demo_workspace(tmp_path):
    root = tmp_path / "demo"
    create_demo(root, build=False)
    result = runner.invoke(app, ["build", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output

    _, store, catalog = _engine(root)
    assert catalog.read("flight_stats").num_rows == 12
    builds = store.list_builds()
    assert len(builds) == 1
    assert builds[0].status.value == "succeeded"
    assert len(builds[0].tasks) == 3


def test_build_command_with_target(tmp_path):
    root = tmp_path / "demo"
    create_demo(root, build=False)
    result = runner.invoke(
        app, ["build", "clean_flights", "--workspace", str(root)]
    )
    assert result.exit_code == 0, result.output

    _, store, catalog = _engine(root)
    assert catalog.read("clean_flights").num_rows == 56
    assert store.get_dataset("flight_stats") is None


def test_build_unknown_target_fails(tmp_path):
    root = tmp_path / "demo"
    create_demo(root, build=False)
    result = runner.invoke(app, ["build", "nope", "--workspace", str(root)])
    assert result.exit_code == 1


# -- datasets ---------------------------------------------------------------------

def test_datasets_list_output(tmp_path):
    root = tmp_path / "demo"
    create_demo(root, build=False)
    result = runner.invoke(app, ["datasets", "list", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert "raw_aircraft" in result.output
    assert "raw_flights" in result.output


def test_datasets_show_output(tmp_path):
    root = tmp_path / "demo"
    create_demo(root, build=False)
    result = runner.invoke(
        app, ["datasets", "show", "raw_flights", "--workspace", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "raw_flights" in result.output
    assert "flight_id" in result.output
    assert "tail_number" in result.output
    assert "LL0001" in result.output


def test_datasets_show_missing_dataset(tmp_path):
    root = tmp_path / "ws"
    Workspace.init(root)
    result = runner.invoke(
        app, ["datasets", "show", "nope", "--workspace", str(root)]
    )
    assert result.exit_code == 1


# -- upload -----------------------------------------------------------------------

def test_upload_csv(tmp_path):
    root = tmp_path / "ws"
    Workspace.init(root)
    csv_path = tmp_path / "cities.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["city", "population"])
        writer.writerow(["Oakland", 440000])
        writer.writerow(["Seattle", 750000])

    result = runner.invoke(
        app, ["upload", "cities", str(csv_path), "--workspace", str(root)]
    )
    assert result.exit_code == 0, result.output

    _, store, catalog = _engine(root)
    table = catalog.read("cities")
    assert table.num_rows == 2
    assert set(table.column_names) == {"city", "population"}
    assert store.get_version("cities").version == 1


def test_upload_bad_extension_fails(tmp_path):
    root = tmp_path / "ws"
    Workspace.init(root)
    bad = tmp_path / "data.json"
    bad.write_text("{}")
    result = runner.invoke(
        app, ["upload", "stuff", str(bad), "--workspace", str(root)]
    )
    assert result.exit_code == 1


# -- workspace discovery errors -----------------------------------------------------

def test_missing_workspace_errors_clearly(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(
        app, ["datasets", "list", "--workspace", str(empty)]
    )
    assert result.exit_code == 1


def test_create_demo_returns_workspace(tmp_path):
    ws = create_demo(tmp_path / "d", build=False)
    assert isinstance(ws, Workspace)
    assert ws.name == "aviation-demo"
