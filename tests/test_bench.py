"""Smoke test for bench/benchmark.py.

docs/SCALE.md publishes numbers and tells readers to reproduce them with this
script, so it must keep working as the APIs it exercises evolve. Runs at a
tiny size — this checks the harness, not performance.
"""

import importlib.util
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
