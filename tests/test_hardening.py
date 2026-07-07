"""Regression tests for review findings: concurrency, replay order, validation."""

import threading

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import EditKind, ObjectEdit
from laurelin.ontology import OntologyService, load_ontology
from laurelin.transforms import Input, Output, collect_transforms


@pytest.fixture()
def ws(tmp_path):
    return Workspace.init(tmp_path / "ws", name="hardening")


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def catalog(ws, store):
    return DatasetCatalog(ws, store)


def test_collect_transforms_is_thread_isolated(ws):
    """Concurrent collections must not leak transforms into each other."""
    a = ws.pipelines_dir / "a"
    b = ws.pipelines_dir / "b"
    a.mkdir()
    b.mkdir()
    (a / "p.py").write_text(
        "from laurelin.transforms import transform, Input, Output\n"
        "import time\n"
        "@transform(output=Output('out_a1'), x=Input('src'))\n"
        "def t_a1(x): return x\n"
        "time.sleep(0.05)\n"
        "@transform(output=Output('out_a2'), x=Input('src'))\n"
        "def t_a2(x): return x\n"
    )
    (b / "p.py").write_text(
        "from laurelin.transforms import transform, Input, Output\n"
        "import time\n"
        "@transform(output=Output('out_b1'), x=Input('src'))\n"
        "def t_b1(x): return x\n"
        "time.sleep(0.05)\n"
        "@transform(output=Output('out_b2'), x=Input('src'))\n"
        "def t_b2(x): return x\n"
    )
    results: dict[str, list[str]] = {}

    def run(key, path):
        reg = collect_transforms(path)
        results[key] = sorted(s.name for s in reg.all())

    threads = [
        threading.Thread(target=run, args=("a", a)),
        threading.Thread(target=run, args=("b", b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["a"] == ["t_a1", "t_a2"]
    assert results["b"] == ["t_b1", "t_b2"]


def test_edit_replay_uses_insertion_order(store):
    """Same-timestamp create+delete must replay in insertion order."""
    ts = "2026-01-01T00:00:00+00:00"
    store.add_object_edit(
        ObjectEdit(id="e1", object_type="thing", pk_value="1",
                   kind=EditKind.create, payload={"id": "1"}, created_at=ts)
    )
    store.add_object_edit(
        ObjectEdit(id="e2", object_type="thing", pk_value="1",
                   kind=EditKind.delete, payload={}, created_at=ts)
    )
    kinds = [e.kind for e in store.list_object_edits("thing")]
    assert kinds == [EditKind.create, EditKind.delete]


def test_concurrent_writes_get_distinct_versions(catalog, store):
    table = pa.table({"a": [1, 2, 3]})
    errors: list[Exception] = []

    def write():
        try:
            catalog.write("racy", table)
        except Exception as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    versions = [v.version for v in store.list_versions("racy")]
    assert len(versions) == 6
    assert len(set(versions)) == 6
    for v in versions:
        assert catalog.read("racy", v).num_rows == 3


ONTOLOGY_YML = """
object_types:
  - api_name: parent
    backing_dataset: parents
    primary_key: pid
    properties:
      pid: {type: string}
      key: {type: string}
  - api_name: child
    backing_dataset: children
    primary_key: cid
    properties:
      cid: {type: string}
      key: {type: string}
link_types:
  - api_name: parent_children
    from: parent
    to: child
    cardinality: one_to_many
    from_property: key
    to_property: key
"""


def test_null_join_keys_do_not_link(ws, catalog, store):
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY_YML)
    catalog.write("parents", pa.table({"pid": ["p1"], "key": [None]}))
    catalog.write(
        "children", pa.table({"cid": ["c1", "c2"], "key": [None, "k"]})
    )
    service = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))
    assert service.linked("parent", "p1", "parent_children") == []


@pytest.fixture()
def client(ws, catalog):
    catalog.write("parents", pa.table({"pid": ["p1"], "key": ["k"]}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY_YML)
    return TestClient(create_app(ws))


def test_empty_targets_rejected(client):
    resp = client.post("/api/v1/builds", json={"targets": []})
    assert resp.status_code == 400


def test_negative_paging_is_400_not_500(client):
    assert client.get("/api/v1/datasets/parents/rows?limit=-1").status_code == 400
    assert client.get("/api/v1/datasets/parents/rows?offset=-1").status_code == 400
    assert client.get("/api/v1/ontology/objects/parent?offset=-3&limit=2").status_code == 400


def test_validation_errors_are_400_with_string_detail(client):
    resp = client.get("/api/v1/datasets/parents/rows?version=abc")
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str)


def test_unknown_filter_property_is_400(client):
    resp = client.get("/api/v1/ontology/objects/parent?filter.nope=1")
    assert resp.status_code == 400


def test_auth_preflight_and_scheme(client, monkeypatch):
    monkeypatch.setenv("LAURELIN_TOKEN", "s3cret")
    assert client.get("/api/v1/datasets").status_code == 401
    # CORS preflight must not require the token
    resp = client.options(
        "/api/v1/datasets",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
    # scheme is case-insensitive per RFC 7235
    ok = client.get(
        "/api/v1/datasets", headers={"Authorization": "bearer s3cret"}
    )
    assert ok.status_code == 200
    # API docs are gated too
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401


def test_401_responses_carry_cors_headers(client, monkeypatch):
    monkeypatch.setenv("LAURELIN_TOKEN", "s3cret")
    resp = client.get("/api/v1/datasets", headers={"Origin": "http://example.com"})
    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == "*"
