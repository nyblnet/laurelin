"""Smoke test for bench/benchmark.py.

docs/SCALE.md publishes numbers and tells readers to reproduce them with this
script, so it must keep working as the APIs it exercises evolve. Runs at a
tiny size — this checks the harness, not performance.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench" / "benchmark.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("laurelin_bench", BENCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["laurelin_bench"] = module
    spec.loader.exec_module(module)
    return module


def test_bench_query_and_ontology_and_build(bench, tmp_path):
    result = bench.bench_size(200, tmp_path)
    assert result["rows"] == 200
    # Every measured query reports a duration.
    for key in ("ingest_ms", "q_aggregate_ms", "q_filter_topn_ms",
                "q_point_lookup_ms", "q_aggregate_rls_ms", "rows_page_ms"):
        assert isinstance(result[key], float), key
    assert result["parquet_mb"] >= 0

    onto = bench.bench_ontology(tmp_path, 200)
    assert onto["objects_page_ms"] > 0
    assert onto["objects_search_ms"] > 0

    build = bench.bench_build(tmp_path, 200)
    assert build["status"] == "succeeded"
    assert build["tasks"] == 2


def test_make_table_shape(bench):
    table = bench.make_table(50)
    assert table.num_rows == 50
    assert {"order_id", "region", "status", "amount"} <= set(table.column_names)


@pytest.fixture(scope="module")
def serialize_cost():
    path = Path(__file__).resolve().parents[1] / "bench" / "serialize_cost.py"
    spec = importlib.util.spec_from_file_location("laurelin_serialize_cost", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["laurelin_serialize_cost"] = module
    spec.loader.exec_module(module)
    return module


def test_the_serialization_harness_still_measures_a_projection_that_does_something(
    serialize_cost,
):
    """The published cost is only meaningful while the cases still *exercise*
    the projection: an admin must receive fields a viewer does not.

    Asserting the models merely construct would not catch the drift that
    matters. Pydantic ignores unknown keyword arguments, so a renamed field
    leaves the harness building a degenerate record that still times fine and
    still publishes a number — the index table's failure mode with extra steps.
    So this pins the *difference* between the two roles, which is the thing
    being timed.
    """
    from laurelin.core.models import Role
    from laurelin.core.serialize import dump_as

    ds = serialize_cost.datasets(1)[0]
    admin = dump_as(ds, Role.admin)
    viewer = dump_as(ds, Role.viewer)
    assert set(viewer) < set(admin), "a viewer must receive strictly fewer keys"
    assert "source" not in viewer
    # Content, not just the key: pydantic ignores unknown keyword arguments, so
    # a renamed field leaves `source` present and empty and the case degenerate.
    assert admin["source"]["table"] == "public.t0"

    board = serialize_cost.dashboards(1, panels=2)[0]
    assert len(board.panels) == 2
    viewer_board = dump_as(board, Role.viewer)
    assert "sql" not in json.dumps(viewer_board), "panel SQL must not reach a viewer"
    assert any("sql" in p for p in dump_as(board, Role.admin)["panels"])


def test_the_harness_measures_the_projection_and_not_a_constant(serialize_cost):
    """`timed` must actually call the work: a harness that returns a number
    without exercising `dump_as` would publish a stable, meaningless figure."""
    calls = []
    elapsed = serialize_cost.timed(lambda: calls.append(1), repeat=3)
    assert len(calls) == 3
    assert elapsed >= 0.0
