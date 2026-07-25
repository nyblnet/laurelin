"""The storage layer and the object-storage commit protocol.

Object stores have no atomic directory rename, so committing a version can't
depend on one. Parts go to unique keys; inserting the manifest row is the
commit. These tests pin that protocol, and that the whole catalog works
against a storage backend that is not the workspace directory.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.storage import Storage, is_remote_uri, storage_for


def table(n: int = 5) -> pa.Table:
    return pa.table({"id": list(range(n)), "label": [f"r{i}" for i in range(n)]})


# -- URI handling --------------------------------------------------------------

@pytest.mark.parametrize("uri, remote", [
    ("s3://bucket/prefix", True),
    ("gs://bucket", True),
    ("abfss://container@acct.dfs.core.windows.net/x", True),
    ("/var/lib/laurelin", False),
    ("relative/path", False),
])
def test_remote_uri_detection(uri, remote):
    assert is_remote_uri(uri) is remote


def test_local_storage_resolves_under_its_base(tmp_path):
    st = Storage.for_uri(tmp_path / "ws")
    assert not st.is_remote
    assert st.resolve("data/x/parts/a.parquet").startswith(str(tmp_path))


def test_part_keys_are_unique(tmp_path):
    st = Storage.for_uri(tmp_path)
    keys = {st.new_part_key("orders") for _ in range(200)}
    assert len(keys) == 200, "part keys must not collide between writers"
    assert all(k.startswith("data/orders/parts/") for k in keys)


# -- round trip ----------------------------------------------------------------

def test_write_read_delete(tmp_path):
    st = Storage.for_uri(tmp_path)
    key = st.new_part_key("orders")
    st.write_table(table(3), key)

    assert st.exists(key)
    assert st.read_table([key]).num_rows == 3
    assert st.dataset([key]).count_rows() == 3

    st.delete(key)
    assert not st.exists(key)
    st.delete(key)  # deleting twice is not an error


def test_streaming_writer(tmp_path):
    st = Storage.for_uri(tmp_path)
    key = st.new_part_key("orders")
    schema = table(1).schema
    writer = st.writer(key, schema)
    writer.write_table(table(2))
    writer.write_table(table(3))
    writer.close()
    assert st.read_table([key]).num_rows == 5


def test_list_keys_is_not_recursive(tmp_path):
    """The legacy-layout fallback must not descend into parts/, or it would
    sweep every part of every version into one version's manifest."""
    st = Storage.for_uri(tmp_path)
    st.write_table(table(1), "data/orders/v0001/data.parquet")
    st.write_table(table(1), "data/orders/parts/deadbeef.parquet")

    assert st.list_keys("data/orders/v0001") == ["data/orders/v0001/data.parquet"]
    assert st.list_keys("data/orders") == []  # only subdirectories live here


# -- the commit protocol -------------------------------------------------------

def test_version_number_race_is_resolved_by_the_database(tmp_path):
    """Two writers picking the same version number: the primary key decides,
    and the loser retries with the next number rather than losing its bytes."""
    ws = Workspace.init(tmp_path / "ws", name="race")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)

    catalog.write("orders", table(2))

    # Force next_version to hand out an already-taken number, as it would if
    # another replica committed between the read and the insert.
    original = store.next_version
    calls = {"n": 0}

    def stale_version(name):
        calls["n"] += 1
        return 1 if calls["n"] == 1 else original(name)

    store.next_version = stale_version
    info = catalog.write("orders", table(4))

    assert info.version == 2, "the loser must take the next free version"
    assert catalog.read("orders").num_rows == 4
    assert catalog.read("orders", version=1).num_rows == 2


def test_failed_write_leaves_no_registered_version(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="fail")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("orders", table(2))

    boom = RuntimeError("storage exploded")
    original_write = catalog.storage.write_table

    def failing(tbl, key):
        raise boom

    catalog.storage.write_table = failing
    with pytest.raises(RuntimeError):
        catalog.write("orders", table(9))
    catalog.storage.write_table = original_write

    # The dataset still reads as it did: no half-registered version.
    assert store.get_dataset("orders").latest_version == 1
    assert catalog.read("orders").num_rows == 2


# -- a catalog on storage outside the workspace ---------------------------------

def test_catalog_works_against_detached_storage(tmp_path):
    """The data plane need not live in the workspace directory — which is what
    makes an object-store backend possible."""
    ws = Workspace.init(tmp_path / "ws", name="detached")
    store = MetadataStore(ws.metadata_path)
    data_root = tmp_path / "elsewhere"
    catalog = DatasetCatalog(ws, store, storage=Storage.for_uri(data_root))

    catalog.write("orders", table(3))
    catalog.append("orders", table(2))
    assert catalog.read("orders").num_rows == 5
    assert catalog.rows("orders", limit=10)
    assert catalog.query("SELECT count(*) AS n FROM orders")["rows"][0]["n"] == 5

    # Nothing landed in the workspace's own data directory.
    assert not any(ws.data_dir.rglob("*.parquet"))
    assert any(data_root.rglob("*.parquet"))


def test_data_uri_env_redirects_workspace_storage(tmp_path, monkeypatch):
    elsewhere = tmp_path / "pool"
    monkeypatch.setenv("LAURELIN_DATA_URI", str(elsewhere))
    ws = Workspace.init(tmp_path / "ws" / "myspace", name="myspace")
    st = storage_for(ws)
    assert st.base.endswith("myspace")
    assert str(elsewhere) in st.base
