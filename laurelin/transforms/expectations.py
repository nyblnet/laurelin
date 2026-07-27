"""Data expectations: assertions a transform's output must satisfy to be
published.

A pipeline that silently produces wrong data is worse than one that fails.
Wrong data spreads — into the ontology, into dashboards, into decisions —
and by the time anyone notices, several downstream builds have been run on
it. A failed build is loud, local, and immediately actionable.

So expectations are checked **before the version is committed**, not after.
A dataset version is published by inserting its manifest row, so validating
the written Parquet parts before that insert means data that fails its
expectations is never visible to anyone: no reader sees it, no downstream
build consumes it, and the orphaned parts are deleted.

    @expect(not_null("order_id"), unique("order_id"), row_count(min=1))
    @transform(output=Output("clean_orders"), raw=Input("raw_orders"))
    def clean_orders(raw):
        ...

Every check is expressed as SQL over the written parts and evaluated by
DuckDB, so a streaming transform stays streaming — the point of checking a
million rows is lost if checking them materializes a million rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Expectation:
    """One assertion about an output.

    ``sql`` counts *offending* rows: zero means the expectation holds. Phrasing
    every check as "how many rows are wrong" gives a useful failure message for
    free — a count, not just a boolean.
    """

    name: str
    description: str
    sql: str
    # A check that isn't about individual rows (row_count) reports differently:
    # its measurement is the value itself, bounded by lo/hi, not a violation count.
    aggregate: bool = False
    lo: Optional[int] = None
    hi: Optional[int] = None
    severity: str = "error"  # "error" fails the build; "warn" records and continues

    def holds(self, measured: int) -> bool:
        if not self.aggregate:
            return measured == 0
        return ((self.lo is None or measured >= self.lo)
                and (self.hi is None or measured <= self.hi))

    def failure_message(self, offending: int) -> str:
        if self.aggregate:
            return f"{self.description} (actual: {offending})"
        return f"{self.description} — {offending:,} row(s) violate it"


def _q(identifier: str) -> str:
    """Quote a column name. These come from pipeline source, but a transform
    author typo shouldn't produce a confusing SQL error."""
    return '"' + identifier.replace('"', '""') + '"'


# -- the checks --------------------------------------------------------------

def not_null(column: str, severity: str = "error") -> Expectation:
    """No NULLs in ``column``."""
    return Expectation(
        name=f"not_null({column})",
        description=f"{column!r} must not contain NULLs",
        sql=f"SELECT count(*) FROM t WHERE {_q(column)} IS NULL",
        severity=severity,
    )


def unique(column: str, severity: str = "error") -> Expectation:
    """No duplicate values in ``column``. NULLs are not compared."""
    return Expectation(
        name=f"unique({column})",
        description=f"{column!r} must be unique",
        # Rows *in excess of* one per value, so the count is "how many rows
        # would have to go away", not "how many values are duplicated".
        sql=(
            f"SELECT coalesce(sum(n - 1), 0) FROM ("
            f"  SELECT count(*) AS n FROM t WHERE {_q(column)} IS NOT NULL"
            f"  GROUP BY {_q(column)} HAVING count(*) > 1)"
        ),
        severity=severity,
    )


def accepted_values(column: str, values, severity: str = "error") -> Expectation:
    """Every non-NULL value of ``column`` is one of ``values``."""
    allowed = list(values)
    if not allowed:
        raise ValueError("accepted_values needs at least one permitted value")
    literals = ", ".join("'" + str(v).replace("'", "''") + "'" for v in allowed)
    shown = ", ".join(repr(str(v)) for v in allowed[:5])
    if len(allowed) > 5:
        shown += f", … ({len(allowed)} total)"
    return Expectation(
        name=f"accepted_values({column})",
        description=f"{column!r} must be one of {shown}",
        sql=(
            f"SELECT count(*) FROM t WHERE {_q(column)} IS NOT NULL "
            f"AND CAST({_q(column)} AS VARCHAR) NOT IN ({literals})"
        ),
        severity=severity,
    )


def row_count(min: Optional[int] = None, max: Optional[int] = None,
              severity: str = "error") -> Expectation:
    """Output row count within bounds.

    ``row_count(min=1)`` is the one worth reaching for by default: a transform
    that silently produces nothing is the failure most likely to go unnoticed,
    because every downstream build "succeeds" on an empty input.
    """
    if min is None and max is None:
        raise ValueError("row_count needs at least one of min= or max=")
    bounds = []
    if min is not None:
        bounds.append(f"at least {min:,}")
    if max is not None:
        bounds.append(f"at most {max:,}")
    return Expectation(
        name=f"row_count(min={min}, max={max})",
        description=f"output must have {' and '.join(bounds)} row(s)",
        sql="SELECT count(*) FROM t",
        aggregate=True,
        lo=min,
        hi=max,
        severity=severity,
    )


def expression(name: str, predicate: str, severity: str = "error") -> Expectation:
    """Escape hatch: every row must satisfy a SQL ``predicate``.

    For anything the named checks don't cover — ``expression("positive",
    "amount > 0")``. The predicate is evaluated by DuckDB against the output.
    """
    return Expectation(
        name=name,
        description=f"every row must satisfy {predicate!r}",
        sql=f"SELECT count(*) FROM t WHERE NOT ({predicate})",
        severity=severity,
    )


class ExpectationError(RuntimeError):
    """Raised when an output fails an expectation, aborting the build."""

    def __init__(self, dataset: str, failures: list[dict]):
        self.dataset = dataset
        self.failures = failures
        detail = "; ".join(f["message"] for f in failures)
        super().__init__(
            f"{dataset!r} failed {len(failures)} expectation(s): {detail}. "
            "The version was not published."
        )


def check(conn, expectations: list[Expectation], dataset: str) -> list[dict]:
    """Evaluate expectations against a DuckDB connection where the output is
    registered as ``t``. Returns one result dict per expectation.

    Does not raise — the caller decides what a failure means, because a
    ``warn`` expectation is recorded exactly like an ``error`` one and only
    differs in whether it stops the build.
    """
    results = []
    for exp in expectations:
        measured = int(conn.execute(exp.sql).fetchone()[0])
        ok = exp.holds(measured)
        results.append({
            "expectation": exp.name,
            "passed": ok,
            "severity": exp.severity,
            "measured": measured,
            "message": "" if ok else exp.failure_message(measured),
        })
    return results


def expect(*expectations: Expectation) -> Callable:
    """Attach expectations to a transform.

    Applied *above* ``@transform`` so the spec already exists::

        @expect(not_null("id"))
        @transform(output=Output("out"), src=Input("src"))
        def build(src): ...
    """
    def decorator(fn: Callable) -> Callable:
        spec = getattr(fn, "__transform_spec__", None)
        if spec is None:
            raise ValueError(
                f"@expect on {fn.__name__!r} must be applied above @transform "
                "(or @sql_transform / @remote_transform), not below it — "
                "there is no transform to attach the expectations to."
            )
        spec.expectations = [*spec.expectations, *expectations]
        return fn

    return decorator
