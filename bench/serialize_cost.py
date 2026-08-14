"""What the audience projection costs, per serialized response.

Every model that becomes a response body goes through
``laurelin.core.serialize.dump``, which decides per field whether this principal
may read it (R2: if you cannot write it, you cannot read it). That work runs on
every record of every response and **no other benchmark in this repo touches
it** — ``bench/benchmark.py`` measures the service layer and never goes through
serialization, which is exactly why a 3–11x regression in it went unnoticed
until somebody went looking.

This exists so the table in docs/SCALE.md ("What the audience projection
costs") is reproducible. The index table in that same document is the
cautionary tale: its numbers came from a script that was never committed, so
when they stopped being true there was no way to tell.

Absolute milliseconds here are a property of the machine. What is portable is
the **ratio between roles**, printed alongside, because an admin receives every
field and pays the per-field check on all of them while a viewer's projection
drops most fields first. That relationship is a property of the design and
should survive a change of hardware.

    python bench/serialize_cost.py
    python bench/serialize_cost.py --json out.json
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laurelin.core.models import (  # noqa: E402
    DashboardInfo,
    DashboardPanel,
    DatasetInfo,
    Role,
)
from laurelin.core.serialize import dump_as  # noqa: E402


def timed(fn, repeat: int = 7) -> float:
    """Best-of-N wall clock in ms. Best, not mean: least contaminated by a
    noisy neighbour, the same convention bench/regression.py uses."""
    best = float("inf")
    for _ in range(repeat):
        gc.collect()
        t0 = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def datasets(n: int) -> list[DatasetInfo]:
    """Federated datasets: the shape that carries an ADMIN-authored `source`
    inside an EDITOR-authored record, so the admin path also pays
    `redacted_source`. That is the realistic worst case and the reason the
    admin column is the expensive one.

    One deliberate infidelity, left alone rather than fixed: a real federated
    registration spells this key `type` (`routes.register_federated_dataset`),
    which is on `redaction._DISCLOSABLE_KEYS`, whereas `kind` is not and comes
    back withheld. The work either way is one dict entry and the difference is
    far below the noise floor, but the published Before/After/Now columns in
    docs/SCALE.md were all taken with *this* dict — so changing it would break
    comparability across the three columns to buy nothing measurable."""
    return [
        DatasetInfo(
            name=f"ds-{i:05d}",
            description="a dataset with a description of realistic length",
            latest_version=i % 17,
            kind="federated",
            source={
                "kind": "postgres",
                "dsn": "postgresql://u:p@db.internal:5432/warehouse",
                "table": f"public.t{i}",
            },
        )
        for i in range(n)
    ]


def dashboards(n: int, panels: int = 6) -> list[DashboardInfo]:
    """Nested records: a viewer gets the layout and never the panel SQL, so
    this is the case where the projection actually recurses."""
    return [
        DashboardInfo(
            name=f"db-{i:04d}",
            title=f"Dashboard {i}",
            description="a board with a description of realistic length",
            panels=[
                DashboardPanel(
                    id=f"p{j}",
                    title=f"Panel {j}",
                    chart="bar",
                    sql=f"SELECT region, sum(amount) FROM items WHERE id > {j} GROUP BY 1",
                    x="region",
                    y=["total"],
                )
                for j in range(panels)
            ],
        )
        for i in range(n)
    ]


CASES = [
    ("100 datasets", lambda: datasets(100)),
    ("1000 datasets", lambda: datasets(1000)),
    ("100 dashboards x 6 panels", lambda: dashboards(100)),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default="", help="Write raw results to this path.")
    args = ap.parse_args()

    print("Cost of the audience projection, per serialized response\n")
    print(f"{'response':<28}{'admin':>10}{'viewer':>10}{'admin/viewer':>14}")
    results = []
    for label, build in CASES:
        models = build()
        row = {"case": label}
        for role in (Role.admin, Role.viewer):
            row[role.value] = timed(lambda: [dump_as(m, role) for m in models])
        row["admin_over_viewer"] = row["admin"] / row["viewer"]
        results.append(row)
        print(f"{label:<28}{row['admin']:>9.2f}ms{row['viewer']:>9.2f}ms"
              f"{row['admin_over_viewer']:>13.2f}x")

    print("\nAn admin costs more than a viewer, which is the opposite of the\n"
          "intuition: a viewer's projection drops most fields and there is less\n"
          "left to walk, while an admin receives all of them and pays the\n"
          "per-field check on every one.")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
