"""Two renderers, one decision — and the first one must not have moved.

Adding a second SQL engine to a governance layer is mostly a refactor, and a
refactor of enforcement code is exactly where a leak gets introduced without
anyone noticing. So the first half of this file is a *golden* test: the strings
DuckDB emits are checked in as literals, copied from the output of the renderer
before dialects existed. If a byte changes, this fails, and "I only added a
seam" stops being a claim and becomes a measurement.

The second half is about ClickHouse's rules, each of which was measured against
chdb in this repo's venv rather than read from a manual — see the comments in
``laurelin/core/dialects.py``.
"""

import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from laurelin.core import clickhouse
from laurelin.core.dialects import CLICKHOUSE, DUCKDB
from laurelin.core.models import MaskMode
from laurelin.core.permissions import PolicyDecision, PolicyRenderError, SqlPolicy

COLUMNS = ["id", "region", "ssn", 'we"ird']

# The renderer needs types now: a row filter and a hash mask are both text
# comparisons, and whether an engine spells a value the way Arrow does is a
# fact about the type. All four here are types every dialect carries exactly,
# so supplying them cannot change any golden string below.
TYPES = {
    "id": pa.int64(),
    "region": pa.string(),
    "ssn": pa.string(),
    'we"ird': pa.string(),
}


# ---------------------------------------------------------------------------
# The golden test: DuckDB's bytes, frozen
# ---------------------------------------------------------------------------
#
# Captured by running SqlPolicy.render on the pre-dialect code. Do not
# "tidy" these strings — their whole purpose is to be unmaintained.

GOLDEN = {
    "no policy": (
        PolicyDecision(),
        COLUMNS,
        '"id", "region", "ssn", "we""ird"',
        "TRUE",
        [],
    ),
    "denies all": (
        PolicyDecision(denies_all=True),
        COLUMNS,
        "*",
        "FALSE",
        [],
    ),
    "mask null": (
        PolicyDecision(masks=[("ssn", MaskMode.null)]),
        COLUMNS,
        '"id", "region", NULLIF("ssn", "ssn") AS "ssn", "we""ird"',
        "TRUE",
        [],
    ),
    "mask redact": (
        PolicyDecision(masks=[("ssn", MaskMode.redact)]),
        COLUMNS,
        '"id", "region", \'***\' AS "ssn", "we""ird"',
        "TRUE",
        [],
    ),
    "mask hash": (
        PolicyDecision(masks=[("ssn", MaskMode.hash)]),
        COLUMNS,
        '"id", "region", CASE WHEN "ssn" IS NULL THEN NULL ELSE '
        'substr(sha256(CAST("ssn" AS VARCHAR)), 1, 16) END AS "ssn", "we""ird"',
        "TRUE",
        [],
    ),
    "row policy, three values": (
        PolicyDecision(row_column="region", allowed_values=["us", "eu", "apac"]),
        COLUMNS,
        '"id", "region", "ssn", "we""ird"',
        'CAST("region" AS VARCHAR) IN (?, ?, ?)',
        ["us", "eu", "apac"],
    ),
    "row policy and two masks": (
        PolicyDecision(
            row_column="region",
            allowed_values=["us"],
            masks=[("ssn", MaskMode.hash), ("id", MaskMode.null)],
        ),
        COLUMNS,
        'NULLIF("id", "id") AS "id", "region", CASE WHEN "ssn" IS NULL THEN NULL '
        'ELSE substr(sha256(CAST("ssn" AS VARCHAR)), 1, 16) END AS "ssn", "we""ird"',
        'CAST("region" AS VARCHAR) IN (?)',
        ["us"],
    ),
    "quoting a column with a quote in it": (
        PolicyDecision(masks=[('we"ird', MaskMode.redact)]),
        COLUMNS,
        '"id", "region", "ssn", \'***\' AS "we""ird"',
        "TRUE",
        [],
    ),
    "no columns, nothing applies": (
        PolicyDecision(),
        [],
        "*",
        "TRUE",
        [],
    ),
}


@pytest.mark.parametrize("case", list(GOLDEN), ids=list(GOLDEN))
def test_duckdb_rendering_has_not_moved(case):
    decision, columns, select_list, where, params = GOLDEN[case]
    policy = SqlPolicy.render(decision, columns, column_types=TYPES)
    assert policy.select_list == select_list
    assert policy.where == where
    assert policy.params == params
    assert policy.dialect is DUCKDB


def test_duckdb_statement_assembly_has_not_moved():
    """The one statement shape federated_table has always emitted."""
    assert DUCKDB.assemble("*", "read_parquet(?)", "TRUE") == (
        "SELECT * FROM read_parquet(?) WHERE TRUE"
    )
    assert DUCKDB.assemble("*", "read_parquet(?)", "FALSE", limit=10) == (
        "SELECT * FROM read_parquet(?) WHERE FALSE LIMIT 10"
    )
    # DuckDB budgets live on the connection; a settings string is not a thing
    # it can express, so it is ignored rather than silently concatenated.
    assert DUCKDB.assemble("*", "t", "TRUE", settings="max_execution_time=1") == (
        "SELECT * FROM t WHERE TRUE"
    )


# The one deliberate behaviour change to the DuckDB renderer, called out
# separately from the golden set so nobody mistakes it for drift.

def test_masks_without_columns_are_a_refusal_not_a_star_select():
    """Before dialects this returned select_list='*' — an *unmasked* read of a
    dataset with masks pending, because the join of an empty parts list fell
    back to a star. Column discovery failing must be a refusal."""
    decision = PolicyDecision(masks=[("ssn", MaskMode.redact)])
    with pytest.raises(PolicyRenderError):
        SqlPolicy.render(decision, [])
    with pytest.raises(PolicyRenderError):
        SqlPolicy.render(decision, [], dialect=CLICKHOUSE)


def test_case_mismatched_mask_is_a_refusal():
    """A mask on a genuinely dropped column keeps working (schema evolution
    must not deny whole datasets); a mask that differs only in case is a typo
    that would serve plaintext, and ClickHouse identifiers are case-sensitive."""
    dropped = PolicyDecision(masks=[("gone", MaskMode.redact)])
    assert SqlPolicy.render(dropped, ["id", "ssn"]).select_list == '"id", "ssn"'

    typo = PolicyDecision(masks=[("SSN", MaskMode.redact)])
    for dialect in (DUCKDB, CLICKHOUSE):
        with pytest.raises(PolicyRenderError, match="case"):
            SqlPolicy.render(typo, ["id", "ssn"], dialect=dialect)


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------

pytestmark_ch = pytest.mark.skipif(
    not clickhouse.available(), reason="needs chdb: pip install 'laurelin[clickhouse]'"
)


def test_the_two_quoters_are_not_the_same_function():
    """Stated as an assertion because the tempting refactor is to share one."""
    assert DUCKDB.quote("x") != CLICKHOUSE.quote("x")
    assert DUCKDB.quote.__func__ is not CLICKHOUSE.quote.__func__


def test_clickhouse_renders_the_measured_expressions():
    assert CLICKHOUSE.null_mask("`amt`") == "if(0, `amt`, NULL)"
    assert CLICKHOUSE.redact_mask("`ssn`") == "'***'"
    assert CLICKHOUSE.hash_mask("`ssn`") == (
        "CASE WHEN `ssn` IS NULL THEN NULL ELSE "
        "substring(lower(hex(SHA256(toString(`ssn`)))), 1, 16) END"
    )
    assert CLICKHOUSE.to_text(CLICKHOUSE.quote("region")) == "toString(`region`)"
    # NULLIF must not survive anywhere in the ClickHouse renderer: it leaks NaN.
    assert "NULLIF" not in CLICKHOUSE.null_mask("`amt`")
    # ...nor may the predicate normalise NULL into a matchable value.
    policy = SqlPolicy.render(
        PolicyDecision(row_column="region", allowed_values=["us"]),
        ["region"],
        dialect=CLICKHOUSE,
        column_types=TYPES,
    )
    assert policy.where == "toString(`region`) IN ('us')"
    for forbidden in ("ifNull", "coalesce", "assumeNotNull"):
        assert forbidden not in policy.where


def test_clickhouse_binds_nothing_and_says_so():
    assert CLICKHOUSE.binds_values is False
    assert DUCKDB.binds_values is True
    with pytest.raises(NotImplementedError):
        CLICKHOUSE.placeholder(0)
    with pytest.raises(NotImplementedError):
        DUCKDB.literal("x")


def test_clickhouse_nests_the_filter_below_the_projection():
    sql = CLICKHOUSE.assemble(
        "'***' AS `region`", "file('/x', Parquet)", "toString(`region`) IN ('us')",
        limit=5, settings="transform_null_in=0",
    )
    assert sql == (
        "SELECT '***' AS `region` FROM (SELECT * FROM file('/x', Parquet) "
        "WHERE toString(`region`) IN ('us')) LIMIT 5 SETTINGS transform_null_in=0"
    )
    # The projection must not be in the same SELECT scope as the filter.
    assert sql.index("WHERE") > sql.index("(SELECT *")


# -- hostile identifiers -------------------------------------------------------

HOSTILE = [
    'a"b',
    "a`b",
    "a\\",
    "a\\`b",
    "sp ace",
    "sel ect",
    "line\nbreak",
    "semi;colon",
    "FALSE",
    "Case",
    "case",
]


@pytestmark_ch
@pytest.mark.parametrize("name", HOSTILE, ids=[repr(n) for n in HOSTILE])
def test_clickhouse_quoter_addresses_the_column_it_names(tmp_path, name):
    """Write a one-column file, read it back through the quoter, and require
    the value written. A quoter that resolves to *some other* column is worse
    than one that errors: the mask lands on the wrong data, silently."""
    path = tmp_path / "one.parquet"
    pq.write_table(pa.table({name: ["VALUE"]}), path)
    scan = clickhouse.scan_expression({"type": "parquet", "path": str(path)})
    sql = f"SELECT {CLICKHOUSE.quote(name)} FROM {scan}"
    try:
        table = clickhouse.run(sql)
    except clickhouse.ClickHouseError:
        return  # a refusal is acceptable; a wrong answer is not
    assert table.column(0).to_pylist() == ["VALUE"]


@pytestmark_ch
def test_duckdbs_quoter_on_clickhouse_returns_a_different_columns_data(tmp_path):
    """The measurement that made the dialect seam non-negotiable.

    ``a\\`b`` and ``a`b`` are different columns. DuckDB's doubled-quote rule
    leaves the backslash in place; ClickHouse reads it as an escape, so the
    name collapses onto its neighbour and returns that column's rows. No error,
    no warning — a mask aimed at one column applied to another.
    """
    path = tmp_path / "two.parquet"
    pq.write_table(pa.table({"a`b": ["RIGHT"], "a\\`b": ["WRONG_TARGET"]}), path)
    scan = clickhouse.scan_expression({"type": "parquet", "path": str(path)})

    wrong = clickhouse.run(f"SELECT {DUCKDB.quote('a\\`b')} FROM {scan}")
    assert wrong.column(0).to_pylist() == ["RIGHT"], "the silent-wrong-column bug"

    right = clickhouse.run(f"SELECT {CLICKHOUSE.quote('a\\`b')} FROM {scan}")
    assert right.column(0).to_pylist() == ["WRONG_TARGET"]


# -- the escaper ---------------------------------------------------------------

def _fuzz_values() -> list[str]:
    """Values chosen for what they do to an escaper, not for looking scary."""
    seeds = [
        "", "plain", "us", "\\", "a\\", "\\\\", "a\\\\b", "\\'", "'", "''",
        "o'brien", "a\nb", "a\r\nb", "a\tb", "a\0b", "x\x41", "%s", "%", "_",
        "us') OR 1=1 --", "us\\' OR 1=1 --", "-- comment", "/*x*/", ";DROP",
        "üñí", "日本語", "​", "emoji 🙂", "{p:String}", "?", "$1",
        "\\N", "NULL", "0x41", "\\x41", "a" * 300, "`", "``", "\\`",
    ]
    # Pad past 400 with every byte-ish codepoint wrapped in text, so the fuzz
    # covers control characters the seed list would never think of.
    seeds += [f"a{chr(c)}b" for c in range(1, 400)]
    return seeds


@pytestmark_ch
def test_the_escaper_round_trips_every_value_byte_for_byte():
    """Fidelity, not "the query ran".

    Every one of these executes successfully through ClickHouse's named-
    parameter channel too — which is exactly how ``{p:String}`` fooled two
    investigations. It is not byte-preserving: 'a\\nb' arrives 3 bytes long
    where the value is 4, and a value that arrives shortened matches rows the
    policy never allowed. So the assertion is on length in bytes.
    """
    values = _fuzz_values()
    assert len(values) >= 400
    # One statement per batch keeps this ~15ms rather than 400 x 15ms.
    for start in range(0, len(values), 100):
        batch = values[start:start + 100]
        select = ", ".join(
            f"length({CLICKHOUSE.literal(v)}) AS c{i}" for i, v in enumerate(batch)
        )
        got = clickhouse.run(f"SELECT {select}").to_pylist()[0]
        measured = [got[f"c{i}"] for i in range(len(batch))]
        assert measured == [len(v.encode()) for v in batch], (
            f"batch starting at {start} did not round-trip"
        )


def test_named_parameters_are_used_nowhere():
    """chdb's named-parameter channel is banned for policy data.

    It is not byte-preserving — ``length({p:String})`` measured 3 for a 4-byte
    ``a\\nb``, 1 for a 2-byte ``\\\\``, and a trailing backslash is a hard
    error — so a shortened value would match rows the policy never allowed. The
    guard is two-part: no call site passes ``params=`` to chdb, and no rendered
    policy contains a ``{name:Type}`` placeholder for the engine to substitute.
    """
    root = Path(__file__).resolve().parents[1] / "laurelin"
    call_sites = re.compile(r"chdb[\w.]*\.query\([^)]*params\s*=")
    offenders = [
        str(f) for f in root.rglob("*.py")
        if call_sites.search(f.read_text())
    ]
    assert offenders == [], f"chdb named parameters must not be used: {offenders}"

    hostile = ["{p:String}", "a\nb", "\\", "us') OR 1=1 --"]
    policy = SqlPolicy.render(
        PolicyDecision(row_column="region", allowed_values=hostile),
        ["region"],
        dialect=CLICKHOUSE,
        column_types=TYPES,
    )
    assert not re.search(r"(?<!')\{\w+:\w+\}", policy.where), policy.where
    assert policy.params == [], "a non-binding dialect must bind nothing"
