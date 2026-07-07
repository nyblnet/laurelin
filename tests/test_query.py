"""Tests for the read-only SQL query endpoint powering the SQL workbench."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="queryws")
    catalog = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    catalog.write(
        "orders",
        pa.table({"region": ["us", "us", "eu"], "amount": [10.0, 5.0, 7.0]}),
    )
    return ws


@pytest.fixture()
def client(ws):
    return TestClient(create_app(ws, no_auth=True))


def test_query_aggregates_over_dataset_view(client):
    resp = client.post(
        "/api/v1/query",
        json={"sql": "SELECT region, sum(amount) AS total FROM orders GROUP BY region ORDER BY region"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["columns"] == ["region", "total"]
    assert {r["region"]: r["total"] for r in body["rows"]} == {"eu": 7.0, "us": 15.0}
    assert body["truncated"] is False


def test_query_truncates_and_flags(client):
    resp = client.post(
        "/api/v1/query",
        json={"sql": "SELECT * FROM range(10) t(n)", "max_rows": 3},
    )
    body = resp.json()
    assert body["row_count"] == 3
    assert body["truncated"] is True


def test_query_syntax_error_is_400(client):
    resp = client.post("/api/v1/query", json={"sql": "SELECT FROM nope nope"})
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str)


def test_query_cannot_read_or_write_filesystem(client):
    # External access is disabled: no reading arbitrary files, no writing them.
    leak = client.post(
        "/api/v1/query", json={"sql": "SELECT * FROM read_csv_auto('/etc/hostname')"}
    )
    assert leak.status_code == 400
    write = client.post(
        "/api/v1/query", json={"sql": "COPY (SELECT 1) TO '/tmp/laurelin_pwn.csv'"}
    )
    assert write.status_code == 400


def test_query_requires_auth_when_enabled(ws, monkeypatch):
    client = TestClient(create_app(ws))  # auth on, no user yet -> setup mode
    resp = client.post("/api/v1/query", json={"sql": "SELECT 1"})
    assert resp.status_code == 401
