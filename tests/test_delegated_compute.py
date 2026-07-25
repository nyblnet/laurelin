"""Delegated compute: the cluster does the work, Laurelin governs the result.

Laurelin owns no distributed engine. A remote transform submits SQL to one
that already exists (Trino/Dremio/Databricks via Flight SQL) and stores what
comes back as an ordinary managed dataset.

The engine client is a one-method protocol, so everything Laurelin actually
owns — orchestration, lineage, guardrails, error handling — is tested here
without a cluster. The ADBC adapter behind it is deliberately thin.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core import engines
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.transforms import Builder, collect_transforms


class FakeEngine:
    """Stands in for a remote cluster: records what it was asked, returns Arrow."""

    def __init__(self, result: pa.Table = None, fail: Exception = None):
        self.result = result if result is not None else pa.table({"region": ["eu"], "total": [7]})
        self.fail = fail
        self.queries: list[str] = []
        self.closed = False

    def query(self, sql: str, params=None) -> pa.Table:
        self.queries.append(sql)
        if self.fail is not None:
            raise self.fail
        return self.result

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="delegated")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    store.upsert_engine("warehouse", "flightsql", "grpc+tls://trino.internal:443",
                        {"adbc.flight.sql.rpc.call_header.authorization": "Bearer tok"})
    return ws, store, catalog


REMOTE = """
from laurelin.transforms import remote_transform, Output

@remote_transform(
    output=Output("revenue_by_region", description="Aggregated on the cluster"),
    engine="warehouse",
    query="SELECT region, sum(amount) AS total FROM events GROUP BY region",
)
def revenue_by_region(): ...
"""


def build(ws, store, catalog, source, engine=None):
    (ws.pipelines_dir / "p.py").write_text(source)
    registry = collect_transforms(ws.pipelines_dir)
    factory = (lambda cfg, timeout: engine) if engine else None
    return Builder(ws, catalog, store, registry, engine_factory=factory).build()


# -- engine registry -------------------------------------------------------------

@pytest.mark.parametrize("config, msg", [
    ({"type": "spark", "uri": "grpc://h:443"}, "Unknown engine type"),
    ({"type": "flightsql", "uri": "https://h"}, "grpc://"),
    ({"type": "flightsql", "uri": ""}, "grpc://"),
    ({"type": "flightsql", "uri": "grpc://h:443", "options": {"a": 1}}, "string-to-string"),
])
def test_engine_validation(config, msg):
    with pytest.raises(ValueError, match=msg):
        engines.validate_engine(config)


def test_engine_accepts_good_config():
    engines.validate_engine({"type": "flightsql", "uri": "grpc+tls://trino:443",
                             "options": {"adbc.flight.sql.rpc.call_header.authorization": "Bearer x"}})


def test_engine_credentials_are_redacted():
    cfg = engines.EngineConfig(
        name="w", uri="grpc+tls://user:hunter2@trino:443",
        options={"authorization_token": "secret-value", "region": "eu"},
    )
    red = cfg.redacted()
    assert "hunter2" not in red["uri"] and "user" in red["uri"]
    assert red["options"]["authorization_token"] == "*****"
    assert red["options"]["region"] == "eu"


def test_engine_registry_roundtrip(env):
    _, store, _ = env
    assert store.get_engine("warehouse")["uri"].startswith("grpc+tls://")
    assert [e["name"] for e in store.list_engines()] == ["warehouse"]
    assert store.delete_engine("warehouse") is True
    assert store.get_engine("warehouse") is None


# -- remote transforms -----------------------------------------------------------

def test_remote_transform_lands_the_engines_result(env):
    ws, store, catalog = env
    engine = FakeEngine(pa.table({"region": ["eu", "us"], "total": [7, 11]}))
    result = build(ws, store, catalog, REMOTE, engine)

    assert result.status.value == "succeeded", result.tasks
    assert "GROUP BY region" in engine.queries[0], "the cluster does the aggregation"
    assert engine.closed, "the connection must be released"

    # It becomes an ordinary managed dataset.
    info = store.get_dataset("revenue_by_region")
    assert info.kind == "managed" and info.latest_version == 1
    assert catalog.read("revenue_by_region").num_rows == 2
    assert result.tasks[0].rows_written == 2


def test_lineage_records_the_engine_as_upstream(env):
    """Otherwise the result would appear to come from nowhere."""
    ws, store, catalog = env
    build(ws, store, catalog, REMOTE, FakeEngine())
    edges = store.list_lineage()
    assert any(
        e.upstream_dataset == "engine:warehouse"
        and e.downstream_dataset == "revenue_by_region"
        for e in edges
    ), edges


def test_unregistered_engine_fails_the_task(env):
    ws, store, catalog = env
    store.delete_engine("warehouse")
    result = build(ws, store, catalog, REMOTE, FakeEngine())
    assert result.status.value == "failed"
    assert "not registered" in result.tasks[0].error


def test_engine_errors_surface_on_the_task(env):
    ws, store, catalog = env
    engine = FakeEngine(fail=engines.EngineError("Trino: table not found"))
    result = build(ws, store, catalog, REMOTE, engine)
    assert result.status.value == "failed"
    assert "table not found" in result.tasks[0].error
    assert engine.closed, "the connection is released even when the query fails"


# -- guardrails ------------------------------------------------------------------

def test_oversized_results_are_refused(env, monkeypatch):
    """Delegation exists so the cluster reduces; a huge result means it didn't."""
    monkeypatch.setenv("LAURELIN_ENGINE_MAX_ROWS", "100")
    ws, store, catalog = env
    engine = FakeEngine(pa.table({"i": list(range(500))}))
    result = build(ws, store, catalog, REMOTE, engine)
    assert result.status.value == "failed"
    assert "above the" in result.tasks[0].error
    assert "aggregate on the cluster" in result.tasks[0].error.lower()


def test_size_cap_can_be_disabled():
    table = pa.table({"i": list(range(1000))})
    assert engines.check_result_size(table, 0, "w") is table  # 0 = unlimited
    with pytest.raises(engines.EngineError):
        engines.check_result_size(table, 10, "w")


# -- declaration errors ----------------------------------------------------------

def test_remote_transform_requires_engine_and_query():
    from laurelin.transforms import Output, remote_transform

    with pytest.raises(ValueError, match="must name an engine"):
        @remote_transform(output=Output("x"), engine="", query="SELECT 1")
        def no_engine(): ...  # pragma: no cover

    with pytest.raises(ValueError, match="needs a query"):
        @remote_transform(output=Output("y"), engine="w", query="  ")
        def no_query(): ...  # pragma: no cover


def test_flight_sql_client_reports_a_clear_connection_error():
    """A bad address must fail with something actionable, not a driver stack."""
    cfg = engines.EngineConfig(name="w", uri="grpc://127.0.0.1:1", options={})
    with pytest.raises(engines.EngineError, match="Could not connect|rejected"):
        client = engines.connect(cfg, timeout_s=2)
        client.query("SELECT 1")
