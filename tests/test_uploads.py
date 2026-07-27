"""Importing a file through the UI.

Creating a dataset used to require the CLI: the web upload control only
existed on an *existing* dataset's page, so a new user's first five minutes
were spent somewhere other than the product.

Two things make the import a decision rather than a guess — a preview that
shows the inferred schema before anything is created, and a suggested name
that turns "Q3 Orders (final).csv" into something the name rules accept.
"""

import io

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog.catalog import suggest_dataset_name
from laurelin.core.config import Workspace


@pytest.fixture()
def client(tmp_path) -> TestClient:
    # Import is a data-plane concern; the viewer-denial assertion for
    # /datasets/preview lives with the rest of the RBAC matrix in test_auth.py.
    ws = Workspace.init(tmp_path / "ws", name="uploads")
    return TestClient(create_app(ws, no_auth=True))

CSV = b"order_id,region,amount\n1,us,10.5\n2,eu,20.0\n3,apac,30.25\n"


def upload(client, name, data=CSV, filename="orders.csv", **params):
    return client.post(
        f"/api/v1/datasets/{name}/upload",
        files={"file": (filename, io.BytesIO(data), "text/csv")},
        params=params,
    )


def preview(client, data=CSV, filename="orders.csv"):
    return client.post(
        "/api/v1/datasets/preview",
        files={"file": (filename, io.BytesIO(data), "text/csv")},
    )


# -- name suggestion ---------------------------------------------------------

@pytest.mark.parametrize("filename, expected", [
    ("orders.csv", "orders"),
    ("Q3 Orders (final).csv", "q3_orders_final"),
    ("sales-2024.parquet", "sales_2024"),
    ("2024-data.csv", "d_2024_data"),          # names must start with a letter
    ("___.csv", "dataset"),                     # nothing usable left
    ("MiXeD CaSe.csv", "mixed_case"),
])
def test_a_filename_becomes_a_valid_dataset_name(filename, expected):
    assert suggest_dataset_name(filename) == expected


def test_a_suggested_name_always_satisfies_the_name_rule():
    from laurelin.catalog.catalog import _NAME_RE
    for filename in ["...csv", "9.csv", "a b c.parquet", "!!!.csv", "x" * 200 + ".csv"]:
        assert _NAME_RE.match(suggest_dataset_name(filename)), filename


# -- preview -----------------------------------------------------------------

def test_preview_infers_the_schema_without_creating_anything(client):
    before = client.get("/api/v1/datasets").json()

    body = preview(client).json()
    assert body["suggested_name"] == "orders"
    assert [c["name"] for c in body["columns"]] == ["order_id", "region", "amount"]
    # DuckDB's inference is the point: these must not all come back as strings.
    types = {c["name"]: c["type"] for c in body["columns"]}
    assert "int" in types["order_id"]
    assert "double" in types["amount"] or "float" in types["amount"]
    assert types["region"] == "string"

    assert body["rows"][0]["order_id"] == 1
    assert body["sampled_rows"] == 3
    assert body["truncated"] is False

    assert client.get("/api/v1/datasets").json() == before, "preview must not create"


def test_preview_caps_the_rows_it_reads(client):
    rows = b"".join(f"{i},us,1.0\n".encode() for i in range(500))
    body = preview(client, data=b"order_id,region,amount\n" + rows).json()
    assert body["sampled_rows"] == 50
    assert body["truncated"] is True, "a capped sample must say so, not imply a total"


def test_preview_rejects_an_unsupported_file_type(client):
    r = client.post(
        "/api/v1/datasets/preview",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert r.status_code == 400
    assert "csv or .parquet" in r.json()["detail"]


# -- import ------------------------------------------------------------------

def test_uploading_creates_the_dataset(client):
    r = upload(client, "orders")
    assert r.status_code == 200
    assert r.json()["row_count"] == 3
    assert client.get("/api/v1/datasets/orders").status_code == 200


def test_append_mode_adds_rows_instead_of_replacing(client):
    upload(client, "orders")
    more = b"order_id,region,amount\n4,us,40.0\n"
    r = upload(client, "orders", data=more, mode="append")
    assert r.status_code == 200
    assert r.json()["row_count"] == 4, "append must keep the existing rows"
    assert r.json()["version"] == 2


def test_replace_is_the_default(client):
    upload(client, "orders")
    only_one = b"order_id,region,amount\n9,us,9.0\n"
    r = upload(client, "orders", data=only_one)
    assert r.json()["row_count"] == 1


def test_an_unknown_mode_is_rejected(client):
    """400, not 422: the app maps validation errors to a readable message
    rather than FastAPI's nested default."""
    r = upload(client, "orders", mode="merge")
    assert r.status_code == 400
    assert "replace|append" in r.json()["detail"]


def test_appending_an_incompatible_schema_fails_clearly(client):
    upload(client, "orders")
    wrong = b"totally,different\n1,2\n"
    r = upload(client, "orders", data=wrong, filename="other.csv", mode="append")
    assert r.status_code == 400
    assert "schema" in r.json()["detail"].lower()
