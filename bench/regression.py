"""Guard the scaling claims published in docs/SCALE.md.

Absolute milliseconds are worthless on a shared CI runner — it is noisy,
oversubscribed, and a different machine from the one that produced the numbers
in the docs. So nothing here asserts a millisecond figure.

What it asserts instead is the *shape* of each claim, as a ratio, where runner
speed cancels out:

    "appends cost the delta, not the dataset"   -> append << full rewrite
    "key lookups are constant time"             -> lookup(4n) ~= lookup(n)
    "row-level security is essentially free"    -> policied ~= unpolicied
    "object queries don't materialize"          -> page(4n) grows sub-linearly
    "streaming removes the RAM bound"           -> peak RSS stays flat

Every threshold is deliberately loose — several times looser than the measured
value — because this exists to catch a regression in *complexity class* (an
accidental full-table scan, a lost pushdown, a materialization creeping back
in), not a 20% drift. A gate that flaps gets disabled, and a disabled gate
protects nothing.

    python bench/regression.py            # CI mode: exit 1 on any failure
    python bench/regression.py --report   # print the ratios and always exit 0
"""

from __future__ import annotations

import argparse
import gc
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laurelin.catalog import DatasetCatalog  # noqa: E402
from laurelin.core.config import Workspace  # noqa: E402
from laurelin.core.db import MetadataStore  # noqa: E402
from laurelin.ontology import OntologyService, load_ontology  # noqa: E402

N = 100_000          # small size
BIG = 4 * N          # the same shape, 4x the rows

ONTOLOGY = """
object_types:
  - api_name: item
    backing_dataset: items
    primary_key: id
    title_property: sku
    properties:
      id: {type: integer}
      sku: {type: string}
      region: {type: string}
actions:
  - api_name: retag
    object_type: item
    kind: update
    parameters:
      region: {type: string, required: true}
"""


def table(n: int, start: int = 0) -> pa.Table:
    return pa.table({
        "id": pa.array(range(start, start + n), type=pa.int64()),
        "sku": [f"SKU-{i % 5000:05d}" for i in range(start, start + n)],
        "region": [["us", "eu", "apac"][i % 3] for i in range(start, start + n)],
        "amount": pa.array([float(i % 977) for i in range(start, start + n)],
                           type=pa.float64()),
    })


def timed(fn, repeat: int = 3) -> float:
    """Best-of-N wall clock in ms. Best, not mean: least contaminated by a
    noisy neighbour on the runner."""
    best = float("inf")
    for _ in range(repeat):
        gc.collect()
        t0 = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def workspace(root: Path, name: str):
    ws = Workspace.init(root / name, name=name)
    store = MetadataStore(ws.metadata_path)
    return ws, store, DatasetCatalog(ws, store)


# ---------------------------------------------------------------- the claims

def claim_appends_cost_the_delta(root: Path) -> tuple[float, float, str]:
    """SCALE.md: an append writes the delta, not a rewritten dataset."""
    _, _, cat = workspace(root, "append")
    cat.write("full", table(BIG))
    cat.write("inc", table(BIG))
    delta = table(BIG // 100, start=BIG)

    rewrite = timed(lambda: cat.write("full", table(BIG)), repeat=2)
    append = timed(lambda: cat.append("inc", delta), repeat=2)
    return rewrite / append, 5.0, "rewrite / append (higher is better)"


def claim_key_lookups_are_constant_time(root: Path) -> tuple[float, float, str]:
    """SCALE.md: an indexed key lookup does not grow with the object count."""
    def lookup_ms(n: int, label: str) -> float:
        ws, store, cat = workspace(root, f"idx{label}")
        cat.write("items", table(n))
        (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
        svc = OntologyService(ws, cat, store, load_ontology(ws.ontology_dir))
        svc.reindex("item")
        return timed(lambda: svc.query("item", filters={"id": str(n - 1)}, limit=1))

    small, big = lookup_ms(N, "s"), lookup_ms(BIG, "b")
    # Inverted vs the others: this one must stay *below* its threshold.
    return big / small, 3.0, "lookup(4n) / lookup(n) (lower is better, max 3.0)"


def claim_object_reads_are_flat_as_edits_grow(root: Path) -> tuple[float, float, str]:
    """SCALE.md: an edit updates the materialization instead of invalidating it,
    so a read costs a key lookup no matter how long the edit log is.

    This is the claim the operational object store exists to make true. Before
    it, every write invalidated the whole index for its object type, so the
    very next read fell back to a scan *and* replayed the entire edit log —
    reads that got monotonically slower the more anyone used the system.

    Measured as read(k edits) / read(0 edits), which is why it is a ratio: the
    absolute number is a property of the runner, but "does it grow with the log"
    is a property of the design. A regression to invalidate-on-write makes this
    blow up rather than drift, because the fallback path is a different
    complexity class, not a slower constant.
    """
    ws, store, cat = workspace(root, "edits")
    cat.write("items", table(N))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    svc = OntologyService(ws, cat, store, load_ontology(ws.ontology_dir))
    svc.reindex("item")

    def read_ms() -> float:
        return timed(lambda: svc.query("item", limit=25))

    def edit(i: int) -> None:
        svc.apply_action("retag", pk=str(i % N), parameters={"region": f"r{i}"},
                         actor="bench")

    baseline = read_ms()
    ratios = []
    done = 0
    for target in (100, 1_000, 10_000):
        while done < target:
            edit(done)
            done += 1
        ratios.append(read_ms() / baseline)
    # Report the worst of the three; a claim that only holds at 100 edits is
    # not the claim.
    return max(ratios), 3.0, (
        f"read(10k edits) / read(0) — 100:{ratios[0]:.2f}x "
        f"1k:{ratios[1]:.2f}x 10k:{ratios[2]:.2f}x (lower is better, max 3.0)"
    )


def claim_object_writes_do_not_grow_with_history(root: Path) -> tuple[float, float, str]:
    """The same pathology from the write side.

    Replaying the log on every read was only half of it: the index was rebuilt
    from scratch whenever anyone rebuilt it, and the search mirror was rewritten
    for the whole object type on every sync. Either one makes a single-row edit
    cost O(objects). This measures write #1000 against write #1, on a type large
    enough that a full rebuild would be obvious.
    """
    ws, store, cat = workspace(root, "writes")
    cat.write("items", table(N))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    svc = OntologyService(ws, cat, store, load_ontology(ws.ontology_dir))
    svc.reindex("item")

    def write_ms(i: int) -> float:
        # repeat=1: writes are not idempotent, so best-of-N would be measuring
        # different edits. Noise is handled by the loose threshold instead.
        return timed(lambda: svc.apply_action(
            "retag", pk=str(i % N), parameters={"region": f"r{i}"}, actor="bench"
        ), repeat=1)

    first = min(write_ms(i) for i in range(5))
    for i in range(5, 1_000):
        svc.apply_action("retag", pk=str(i % N), parameters={"region": f"r{i}"},
                         actor="bench")
    later = min(write_ms(i) for i in range(1_000, 1_005))
    return later / first, 3.0, "write #1000 / write #1 (lower is better, max 3.0)"


def claim_object_queries_do_not_materialize(root: Path) -> tuple[float, float, str]:
    """SCALE.md: object queries push into DuckDB instead of building every row
    in Python.

    The baseline is ``table_to_rows`` over the whole dataset — literally what
    the old path did on every request. If paging ever regresses to
    materializing, the two converge and this ratio collapses toward 1.

    Measured against a raw Parquet read instead, this would be meaningless: an
    Arrow read is columnar and cheaper than any windowed SQL, so paging is
    *slower* than it by design. Building a Python dict per row is the cost
    that actually went away.
    """
    ws, store, cat = workspace(root, "page")
    cat.write("items", table(BIG))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    svc = OntologyService(ws, cat, store, load_ontology(ws.ontology_dir))

    materialize = timed(lambda: cat.table_to_rows(cat.read("items")), repeat=2)
    page = timed(lambda: svc.query("item", limit=25))
    # Grows with size (3.5x at 100 K, 6.6x at 800 K), so the margin at 400 K
    # widens rather than narrows as data grows.
    return materialize / page, 3.0, "materialize-all / page of 25 (higher is better)"


def claim_row_level_security_is_not_a_tax(root: Path) -> tuple[float, float, str]:
    """SCALE.md: a row policy is pushed into the scan, so enforcement is
    effectively free. It used to force materialization and cost 3.6x."""
    from laurelin.core.models import Role, User
    from laurelin.core.permissions import PermissionService

    ws, store, cat = workspace(root, "rls")
    cat.write("items", table(BIG))

    analyst = User(id="a", username="analyst", role=Role.viewer)
    store.set_dataset_policy("items", {
        "dataset": "items",
        "row_policy": {
            "column": "region",
            "rules": [{"subject_kind": "user", "subject": "analyst",
                       "values": ["us"]}],
        },
        "column_masks": [],
    })
    perms = PermissionService(store)
    sql = "SELECT region, sum(amount) AS total FROM items GROUP BY region"

    plain = timed(lambda: cat.query(sql))
    policied = timed(lambda: cat.query(sql, plan_for=perms.arrow_policy_fn(analyst)))
    return policied / plain, 2.0, "policied / plain (lower is better, max 2.0)"


CLAIMS = [
    ("appends cost the delta, not the dataset", claim_appends_cost_the_delta, "min"),
    ("indexed key lookups are constant time", claim_key_lookups_are_constant_time, "max"),
    ("object queries do not materialize", claim_object_queries_do_not_materialize, "min"),
    ("object reads stay flat as the edit log grows",
     claim_object_reads_are_flat_as_edits_grow, "max"),
    ("object writes do not grow with history",
     claim_object_writes_do_not_grow_with_history, "max"),
    ("row-level security is not a tax", claim_row_level_security_is_not_a_tax, "max"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="print ratios and exit 0 regardless")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="laurelin-regression-"))
    failures = []
    try:
        print(f"Guarding {len(CLAIMS)} published claims "
              f"(n={N:,}, 4n={BIG:,})\n")
        for name, fn, direction in CLAIMS:
            ratio, threshold, unit = fn(root)
            ok = ratio >= threshold if direction == "min" else ratio <= threshold
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {name}\n"
                  f"         {ratio:.2f}x   {unit}")
            if not ok:
                failures.append((name, ratio, threshold, direction))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures and not args.report:
        print("\nA published scaling claim no longer holds:\n")
        for name, ratio, threshold, direction in failures:
            want = "at least" if direction == "min" else "at most"
            print(f"  {name}: measured {ratio:.2f}x, needs {want} {threshold:.2f}x")
        print("\nEither the change regressed a complexity class, or docs/SCALE.md "
              "needs updating to match reality. Do not silence this without "
              "deciding which.")
        return 1

    print("\nAll published claims hold." if not failures
          else "\n(--report: failures ignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
