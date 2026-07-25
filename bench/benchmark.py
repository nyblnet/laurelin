"""Reproducible Laurelin benchmarks — the numbers published in docs/SCALE.md.

Deliberately measures the paths users actually hit, at sizes where the answer
changes: ingest, aggregate/filter/point-lookup queries, ontology
materialization, and a pipeline build. Everything runs against a real
workspace through the real API, not micro-benchmarked internals.

    python bench/benchmark.py                 # default sizes
    python bench/benchmark.py --rows 1e5,1e6  # custom
    python bench/benchmark.py --json out.json

Report wall-clock and peak RSS. One machine's numbers are a data point, not a
guarantee — the point is the *shape* of the curve and where it breaks.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import resource
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laurelin.catalog import DatasetCatalog  # noqa: E402
from laurelin.core.config import Workspace  # noqa: E402
from laurelin.core.db import MetadataStore  # noqa: E402
from laurelin.core.models import Role, User  # noqa: E402
from laurelin.core.permissions import PermissionService  # noqa: E402

ADMIN = User(id="bench", username="bench", role=Role.admin)

REGIONS = ["us-east", "us-west", "eu-central", "apac", "latam"]
STATUSES = ["open", "shipped", "returned"]


def peak_rss_mb() -> float:
    # ru_maxrss is KiB on Linux, bytes on macOS.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1024 if sys.platform != "darwin" else raw / (1024 * 1024)


def timed(fn, repeat: int = 3) -> tuple[float, object]:
    """Best-of-N wall clock in ms (best, not mean: least contaminated by noise)."""
    best, out = float("inf"), None
    for _ in range(repeat):
        gc.collect()
        t0 = time.perf_counter()
        out = fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best, out


def make_table(n: int) -> pa.Table:
    """A realistic-ish orders table: mixed types, 8 columns."""
    return pa.table(
        {
            "order_id": pa.array(range(n), type=pa.int64()),
            "customer_id": pa.array([i % max(1, n // 20) for i in range(n)], type=pa.int64()),
            "region": pa.array([REGIONS[i % len(REGIONS)] for i in range(n)]),
            "status": pa.array([STATUSES[i % len(STATUSES)] for i in range(n)]),
            "amount": pa.array([round((i % 997) * 1.37, 2) for i in range(n)], type=pa.float64()),
            "quantity": pa.array([(i % 17) + 1 for i in range(n)], type=pa.int32()),
            "sku": pa.array([f"SKU-{i % 5000:05d}" for i in range(n)]),
            "note": pa.array([f"order {i} placed via web checkout" for i in range(n)]),
        }
    )


def bench_size(n: int, root: Path) -> dict:
    ws = Workspace.init(root / f"ws{n}", name=f"bench{n}")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    perms = PermissionService(store)
    result: dict[str, object] = {"rows": n}

    table = make_table(n)

    # --- ingest: write a new immutable Parquet version -----------------------
    write_ms, info = timed(lambda: catalog.write("orders", table), repeat=1)
    result["ingest_ms"] = round(write_ms, 1)
    parquet = ws.root / info.path / "data.parquet"
    result["parquet_mb"] = round(parquet.stat().st_size / (1024 * 1024), 2)
    result["arrow_mb"] = round(table.nbytes / (1024 * 1024), 2)

    policy_for = perms.per_dataset_policy_fn(ADMIN)

    # --- queries (through the real query path, incl. lazy Arrow scan) --------
    queries = {
        "q_aggregate": "SELECT region, count(*) AS n, sum(amount) AS total "
                       "FROM orders GROUP BY region ORDER BY total DESC",
        "q_filter_topn": "SELECT order_id, amount FROM orders "
                         "WHERE region = 'apac' AND amount > 500 "
                         "ORDER BY amount DESC LIMIT 100",
        "q_point_lookup": f"SELECT * FROM orders WHERE order_id = {max(0, n - 1)}",
        "q_group_high_card": "SELECT sku, sum(quantity) AS q FROM orders "
                             "GROUP BY sku ORDER BY q DESC LIMIT 20",
        "q_self_join": "SELECT a.region, count(*) AS n FROM orders a "
                       "JOIN orders b USING (customer_id) "
                       "WHERE a.status = 'open' GROUP BY a.region",
    }
    for key, sql in queries.items():
        # The self-join is quadratic in customers; skip it at the top size.
        if key == "q_self_join" and n > 1_000_000:
            result[key + "_ms"] = None
            continue
        ms, _ = timed(lambda s=sql: catalog.query(s, max_rows=1000, policy_for=policy_for))
        result[key + "_ms"] = round(ms, 1)

    # --- the same aggregate with row-level security engaged ------------------
    store.set_dataset_policy(
        "orders",
        {
            "dataset": "orders",
            "row_policy": {
                "column": "region",
                "rules": [{"subject_kind": "everyone", "subject": "", "values": REGIONS[:2]}],
            },
            "column_masks": [],
        },
    )
    viewer = User(id="v", username="viewer", role=Role.viewer)
    rls_policy_for = PermissionService(store).per_dataset_policy_fn(viewer)
    ms, _ = timed(
        lambda: catalog.query(queries["q_aggregate"], max_rows=1000, policy_for=rls_policy_for)
    )
    result["q_aggregate_rls_ms"] = round(ms, 1)
    store.set_dataset_policy("orders", None)

    # --- row API page (what the UI grid hits) --------------------------------
    ms, _ = timed(lambda: catalog.rows("orders", limit=100, offset=0))
    result["rows_page_ms"] = round(ms, 1)

    result["peak_rss_mb"] = round(peak_rss_mb(), 1)
    del table
    gc.collect()
    return result


def bench_incremental(root: Path, n: int, delta_frac: float = 0.01) -> dict:
    """Write amplification: a full rewrite vs an append of the same delta.

    This is the difference between "adding today's rows costs the whole
    dataset" and "adding today's rows costs today's rows".
    """
    ws = Workspace.init(root / f"incr{n}", name=f"incr{n}")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    base = make_table(n)
    catalog.write("orders", base)

    delta_rows = max(1, int(n * delta_frac))
    delta = make_table(delta_rows)

    def full_rewrite():
        combined = pa.concat_tables([catalog.read("orders"), delta])
        return catalog.write("orders_full", combined)

    def append_delta():
        return catalog.append("orders", delta)

    catalog.write("orders_full", base)  # same starting point for a fair compare
    rewrite_ms, _ = timed(full_rewrite, repeat=1)
    append_ms, info = timed(append_delta, repeat=1)

    def dir_bytes(dataset: str, version: int) -> int:
        d = ws.data_dir / dataset / f"v{version:04d}"
        return sum(f.stat().st_size for f in d.glob("*.parquet"))

    return {
        "rows": n,
        "delta_rows": delta_rows,
        "full_rewrite_ms": round(rewrite_ms, 1),
        "append_ms": round(append_ms, 1),
        "speedup": round(rewrite_ms / append_ms, 1) if append_ms else None,
        "full_rewrite_bytes": dir_bytes("orders_full", 2),
        "append_bytes": dir_bytes("orders", info.version),
        "parts_after_append": len(info.files),
    }


def bench_build(root: Path, n: int) -> dict:
    """A two-stage pipeline (python filter -> sql aggregate) over n rows."""
    from laurelin.transforms import Builder, collect_transforms

    ws = Workspace.init(root / f"build{n}", name=f"build{n}")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("raw_orders", make_table(n))

    (ws.pipelines_dir / "pipeline.py").write_text(
        "from laurelin.transforms import transform, sql_transform, Input, Output\n"
        "import pyarrow.compute as pc\n\n"
        "@transform(output=Output('clean_orders'), orders=Input('raw_orders'))\n"
        "def clean_orders(orders):\n"
        "    return orders.filter(pc.not_equal(orders['status'], 'returned'))\n\n"
        "@sql_transform(output=Output('orders_by_region'),\n"
        "               inputs={'o': Input('clean_orders')},\n"
        "               query='SELECT region, count(*) AS n, sum(amount) AS total "
        "FROM o GROUP BY region')\n"
        "def orders_by_region(): ...\n"
    )
    registry = collect_transforms(ws.pipelines_dir)
    builder = Builder(ws, catalog, store, registry)
    ms, build = timed(lambda: builder.build(), repeat=1)
    return {
        "rows": n,
        "build_ms": round(ms, 1),
        "status": build.status.value,
        "tasks": len(build.tasks),
    }


def bench_ontology(root: Path, n: int) -> dict:
    """Ontology object search — the known full-scan path (no index yet)."""
    from laurelin.ontology import OntologyService, load_ontology

    ws = Workspace.init(root / f"onto{n}", name=f"onto{n}")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("orders", make_table(n))
    (ws.ontology_dir / "o.yml").write_text(
        "object_types:\n"
        "  - api_name: order\n"
        "    backing_dataset: orders\n"
        "    primary_key: order_id\n"
        "    title_property: sku\n"
        "    properties:\n"
        "      order_id: {type: integer}\n"
        "      region: {type: string}\n"
        "      status: {type: string}\n"
        "      amount: {type: float}\n"
        "      sku: {type: string}\n"
    )
    ontology = load_ontology(ws.ontology_dir)
    svc = OntologyService(ws, catalog, store, ontology)
    page_ms, _ = timed(lambda: svc.query("order", limit=25, offset=0))
    search_ms, _ = timed(lambda: svc.query("order", search="SKU-04999", limit=25, offset=0))
    get_ms, obj = timed(lambda: svc.get("order", str(n - 1)))
    assert obj is not None, "point lookup should find the last object"
    return {
        "rows": n,
        "objects_page_ms": round(page_ms, 1),
        "objects_search_ms": round(search_ms, 1),
        "objects_get_pk_ms": round(get_ms, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default="10000,100000,1000000,5000000",
                    help="Comma-separated row counts (1e6 notation allowed).")
    ap.add_argument("--json", default="", help="Write raw results to this path.")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    sizes = [int(float(s)) for s in args.rows.split(",") if s.strip()]
    tmp = Path(tempfile.mkdtemp(prefix="laurelin-bench-"))
    print(f"workspace root: {tmp}\n")

    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }
    try:
        import duckdb
        env["duckdb"] = duckdb.__version__
    except Exception:  # noqa: BLE001
        pass
    env["pyarrow"] = pa.__version__
    print(json.dumps(env, indent=2), "\n")

    results = {"env": env, "query": [], "build": [], "ontology": [], "incremental": []}
    try:
        for n in sizes:
            print(f"--- {n:,} rows ---", flush=True)
            row = bench_size(n, tmp)
            results["query"].append(row)
            print(json.dumps(row, indent=2), flush=True)

            incr = bench_incremental(tmp, n)
            results["incremental"].append(incr)
            print(json.dumps(incr, indent=2), flush=True)

            onto = bench_ontology(tmp, n)
            results["ontology"].append(onto)
            print(json.dumps(onto, indent=2), flush=True)

            if not args.skip_build:
                b = bench_build(tmp, n)
                results["build"].append(b)
                print(json.dumps(b, indent=2), flush=True)
            print(flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
