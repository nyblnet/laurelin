"""Tests for pipeline (transform) authoring: file CRUD, validation, generation."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.transforms import Builder, PipelineFiles, collect_transforms

PY_TRANSFORM = (
    "from laurelin.transforms import transform, Input, Output\n\n"
    "@transform(output=Output('clean'), raw=Input('raw'))\n"
    "def clean(raw):\n"
    "    return raw\n"
)


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="authoring")
    DatasetCatalog(ws, MetadataStore(ws.metadata_path)).write(
        "raw", pa.table({"x": [1, 2, 3]})
    )
    return ws


@pytest.fixture()
def files(ws):
    return PipelineFiles(ws.pipelines_dir)


def test_write_read_list_delete(files):
    result = files.write("mypipe", PY_TRANSFORM)
    assert result["name"] == "mypipe"
    assert result["transforms"] == ["clean"]
    assert result["collect_error"] is None

    assert files.read("mypipe")["content"] == PY_TRANSFORM
    listing = files.list()
    # R1: the listing carries a boolean, not a traceback. A pipeline file is
    # `exec`-ed, so its import error is whatever an arbitrary library chose to
    # say — the detail belongs on the detail route and in the log.
    assert listing == [
        {"name": "mypipe", "transforms": ["clean"], "failed": False,
         "bytes": len(PY_TRANSFORM)}
    ]

    files.delete("mypipe")
    assert files.list() == []
    with pytest.raises(KeyError):
        files.read("mypipe")


def test_written_transform_is_buildable(ws, files):
    files.write("pipe", PY_TRANSFORM)
    catalog = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    registry = collect_transforms(ws.pipelines_dir)
    build = Builder(ws, catalog, MetadataStore(ws.metadata_path), registry).build()
    assert build.status.value == "succeeded"
    assert catalog.read("clean").num_rows == 3


def test_syntax_error_rejected_without_writing(files):
    with pytest.raises(ValueError, match="Syntax error"):
        files.write("broken", "def x(:\n")
    assert files.list() == []  # nothing written


def test_path_traversal_rejected(files):
    for bad in ["../evil", "sub/mod", "a.b", "UPPER", "9start", ".hidden"]:
        with pytest.raises(ValueError):
            files.write(bad, "x = 1\n")


def test_leaked_dotfile_temp_is_not_collected_or_executed(ws, files):
    files.write("good", PY_TRANSFORM)
    # Simulate a leaked temp / hidden file that would blow up if exec'd.
    (ws.pipelines_dir / ".good-abcd.py.tmp").write_text("raise RuntimeError('boom')\n")
    (ws.pipelines_dir / ".hidden.py").write_text("raise RuntimeError('boom')\n")
    # list() ignores dotfiles...
    assert [f["name"] for f in files.list()] == ["good"]
    # ...and collect_transforms never compiles/execs them (no RuntimeError).
    registry = collect_transforms(ws.pipelines_dir)
    assert [s.name for s in registry.all()] == ["clean"]


def test_collect_error_surfaced_for_duplicate_output(files):
    files.write("first", PY_TRANSFORM)
    # A second file producing the same output 'clean' is a cross-file conflict:
    # the write succeeds (syntax ok) but collect_error reports the clash.
    result = files.write("second", PY_TRANSFORM.replace("def clean", "def clean2"))
    assert result["collect_error"] is not None
    # A structured failure naming the file, not a formatted exception. It is
    # returned only to the EDITOR who just wrote the file.
    assert result["collect_error"]["code"] == "transform_failed"
    assert result["collect_error"]["subject"] == "pipeline:second"


def test_generate_sql_transform_detects_inputs(files):
    result = files.generate_sql_transform(
        "SELECT region, count(*) AS n FROM raw GROUP BY region",
        output="raw_by_region",
        dataset_names=["raw", "other"],
    )
    content = files.read(result["name"])["content"]
    assert '"raw": Input("raw")' in content
    assert "other" not in content  # not referenced in the SQL
    assert result["transforms"] == ["raw_by_region"]


def test_generate_refuses_existing_file(files):
    files.write("raw_by_region", PY_TRANSFORM)
    with pytest.raises(ValueError, match="already exists"):
        files.generate_sql_transform("SELECT 1", "out", ["raw"], name="raw_by_region")


# -- HTTP surface ------------------------------------------------------------

@pytest.fixture()
def client(ws):
    return TestClient(create_app(ws, no_auth=True))


def test_api_pipeline_crud(client):
    assert client.get("/api/v1/pipelines").json() == []
    w = client.put("/api/v1/pipelines/p1", json={"content": PY_TRANSFORM})
    assert w.status_code == 200
    assert w.json()["transforms"] == ["clean"]
    assert [p["name"] for p in client.get("/api/v1/pipelines").json()] == ["p1"]
    assert client.get("/api/v1/pipelines/p1").json()["content"] == PY_TRANSFORM
    assert client.delete("/api/v1/pipelines/p1").json() == {"ok": True}


def test_api_syntax_error_is_400(client):
    r = client.put("/api/v1/pipelines/bad", json={"content": "def x(:"})
    assert r.status_code == 400


def test_api_from_query(client):
    r = client.post(
        "/api/v1/pipelines/from-query",
        json={"sql": "SELECT count(*) AS n FROM raw", "output": "raw_count"},
    )
    assert r.status_code == 200
    content = client.get("/api/v1/pipelines/raw_count").json()["content"]
    assert '"raw": Input("raw")' in content


def test_a_pipelines_import_failure_is_structured_on_the_editor_only_detail_route(client):
    """A pipeline file is `exec`-ed, so its import error can say anything at
    all. Nothing of what it said is stored or served."""
    client.put("/api/v1/pipelines/boom", json={
        "content": 'raise RuntimeError("connect failed: password=S3KRET")\n'
    })
    listed = [p for p in client.get("/api/v1/pipelines").json() if p["name"] == "boom"]
    assert listed and listed[0]["failed"] is True
    assert "S3KRET" not in client.get("/api/v1/pipelines").text

    detail = client.get("/api/v1/pipelines/boom").json()
    assert detail["failure"]["code"] == "transform_failed"
    assert detail["failure"]["subject"] == "pipeline:boom"
    assert "S3KRET" not in str(detail["failure"])


def test_api_lock_pipelines(ws):
    locked = TestClient(create_app(ws, no_auth=True, lock_pipelines=True))
    assert locked.get("/api/v1/pipelines").status_code == 200  # read still allowed
    assert locked.put("/api/v1/pipelines/x", json={"content": "x=1"}).status_code == 403
    assert locked.delete("/api/v1/pipelines/x").status_code == 403
