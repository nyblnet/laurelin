"""CLI + demo generator tests (typer CliRunner over tmp_path workspaces)."""

from __future__ import annotations

import csv
from pathlib import Path

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
    assert {lt.api_name for lt in ontology.link_types} == {"aircraft_flights"}
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
    ws, build = create_demo(tmp_path / "d", build=False)
    assert isinstance(ws, Workspace)
    assert ws.name == "aviation-demo"
    assert build is None, "no build was asked for, so there is nothing to report"


# -- a second run over an existing workspace ------------------------------------
#
# `Workspace.init` is documented idempotent and stays that way: two concurrent
# callers of it must both succeed, and `api/context._bundle` relies on that.
# What was wrong is that a CLI inherited the silence — `init ./ws --name X` on
# an existing workspace discarded `--name` and announced the OLD name as though
# it had just created it, and `demo` re-ran over a populated workspace,
# overwriting the pipeline and ontology files and bumping every dataset a
# version, while printing "created". Exit code 2 is this CLI's "refused, and
# here is the flag that overrides it", the same as the export path.


def test_init_and_demo_refuse_a_path_that_already_holds_a_workspace(tmp_path):
    root = tmp_path / "ws"
    assert runner.invoke(app, ["init", str(root), "--name", "first"]).exit_code == 0

    again = runner.invoke(app, ["init", str(root), "--name", "clobbered"])
    assert again.exit_code == 2, again.output
    assert "already holds the workspace 'first'" in again.output
    assert "--force" in again.output
    assert Workspace(root).name == "first", "and the refusal changed nothing"

    demo_again = runner.invoke(app, ["demo", str(root), "--no-build"])
    assert demo_again.exit_code == 2, demo_again.output
    assert Workspace(root).name == "first"

    forced = runner.invoke(app, ["init", str(root), "--force", "--name", "ignored"])
    assert forced.exit_code == 0, forced.output
    assert Workspace(root).name == "first", (
        "--force means proceed, not rename: the marker is still written once, "
        "and saying otherwise would be a second silent surprise"
    )


def test_init_over_the_same_workspace_says_it_changed_nothing(tmp_path):
    """The narrow rule, and the reason it is narrow. `Workspace.init` is
    idempotent by design and two concurrent callers must both succeed, so a
    re-run that would discard nothing is not an error — it is a no-op, and the
    only bug was announcing it as an initialization."""
    root = tmp_path / "ws"
    assert runner.invoke(app, ["init", str(root), "--name", "Orders"]).exit_code == 0

    again = runner.invoke(app, ["init", str(root), "--name", "Orders"])
    assert again.exit_code == 0, again.output
    assert "already exists" in again.output and "Nothing to do." in again.output
    assert "Initialized" not in again.output


def test_a_plain_reinit_never_blames_a_flag_the_operator_did_not_type(tmp_path):
    """The refusal diffed an INVENTED `--name` against the stored one.

    `wanted = name or Path(path).name` substituted the directory basename as
    though the operator had typed it, so a bare `laurelin init clitest` — no
    flags at all — exited 2 with "would leave it as it is and ignore --name",
    naming a flag nobody passed, for every workspace whose name differs from
    its directory. The documented-idempotent re-run stopped working, and the
    rule it was supposed to enforce ("a run that would discard nothing
    proceeds") held only by coincidence, when the directory happened to share
    the workspace's name.
    """
    root = tmp_path / "clitest"
    assert runner.invoke(app, ["init", str(root), "--name", "First"]).exit_code == 0

    bare = runner.invoke(app, ["init", str(root)])
    assert bare.exit_code == 0, bare.output
    assert "--name" not in bare.output, (
        "a run that passed no flags must not be refused for one"
    )
    assert "Nothing to do." in bare.output

    # A --name that WOULD be discarded is still refused, naming only it.
    clash = runner.invoke(app, ["init", str(root), "--name", "Second"])
    assert clash.exit_code == 2, clash.output
    assert "--name" in clash.output and "--description" not in clash.output


def test_demo_reports_the_targets_that_actually_built(tmp_path):
    """The old line was a literal — three names printed whenever a build was
    requested, inspecting nothing. It is the first build result a new operator
    reads, so it is the last place a sentence should be unable to go red."""
    root = tmp_path / "demo"
    result = runner.invoke(app, ["demo", str(root)])
    assert result.exit_code == 0, result.output

    _, store, _ = _engine(root)
    build = store.list_builds()[0]
    built = [t.output_dataset for t in build.tasks if t.status.value == "succeeded"]
    assert built, "the demo build produced something"
    assert f"Pipeline built: {', '.join(built)}" in result.output
    assert "Did NOT build" not in result.output


def test_the_demo_build_line_goes_red_when_the_build_does(capsys):
    """The half a green demo cannot prove. Today's three demo transforms all
    succeed, so a hardcoded line and an honest one print the same words — which
    is exactly why the honest one has to be exercised against a build that
    failed, or the guard is a coincidence."""
    from laurelin.cli import _echo_build_result
    from laurelin.core.models import BuildInfo, BuildStatus, BuildTaskInfo

    info = BuildInfo(
        id="b1",
        status=BuildStatus.failed,
        tasks=[
            BuildTaskInfo(transform_name="clean_aircraft",
                          output_dataset="clean_aircraft",
                          status=BuildStatus.succeeded),
            BuildTaskInfo(transform_name="flight_stats",
                          output_dataset="flight_stats",
                          status=BuildStatus.failed),
        ],
    )
    _echo_build_result(info)
    out = capsys.readouterr()
    assert "Pipeline built: clean_aircraft" in out.out
    assert "flight_stats" not in out.out, "a failed target is not a built one"
    assert "Did NOT build: flight_stats" in out.err

    _echo_build_result(BuildInfo(id="b2", status=BuildStatus.succeeded, tasks=[]))
    assert "declares no pipelines" in capsys.readouterr().out
