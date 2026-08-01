"""A policy defined on text is only a policy where the engines agree on the text.

A row policy is ``<column as text> IN (<allowlist>)`` and a hash mask is
``sha256(<column as text>)``. There are four stringifiers involved and no two of
them are the same function:

===========================  ==================================================
Arrow, row key               ``pyarrow.compute.cast(col, string)``
Arrow, digest input          ``str(value)`` on the Python object
DuckDB                       ``CAST(c AS VARCHAR)``
ClickHouse                   ``toString(c)``
StarRocks                    ``CAST(c AS STRING)``
===========================  ==================================================

Where they disagree the *same* policy admits a different set of rows, or emits
a token that will not join, depending on which engine happened to run it. That
was not theoretical: on a ``decimal(12,2)`` tenant key with the policy value
``'1.1'``, the Arrow row API returned nothing and the ClickHouse renderer
returned another tenant's rows through ``/api/v1/query``.

``SqlDialect.row_key_matches_arrow`` and ``hash_text_matches_arrow`` are the
dialects' claims about where they agree, and the renderer refuses everything
they do not claim. This file exists because a *claim* about another program's
formatting is worthless unless something re-checks it: every case below writes
a real Parquet file and asks the real engines, so a chdb or DuckDB upgrade that
changes a rendering fails here instead of quietly changing a policy.

Both directions are asserted. A dialect that under-claims is a denial of
service and would be caught by nobody, so ``test_no_dialect_under_claims``
fails if an engine actually agrees on a type the dialect refuses.

**StarRocks is measured separately, and the difference is not cosmetic.** The
DuckDB and ClickHouse arms share one Parquet file, because both engines read
files. StarRocks is a server: the corpus has to be *loaded into it*, and the
Arrow column it is compared against is the one Laurelin's own reader produces
from the driver's rows. That is the right comparison — it is the path a
governed read actually takes — but it means the two halves of this file are not
interchangeable, and a type that agrees here has been shown to agree for a
**native StarRocks column** only. Reading the same table through an Iceberg
external catalog is **not covered**: no catalog was stood up, and the
Iceberg→StarRocks type mapping could move DECIMAL scale or DATETIME precision.
Until that is measured, the flagship Iceberg claim does not rest on this file.
"""

import datetime
import decimal

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from laurelin.core import clickhouse, starrocks
from laurelin.core.dialects import CLICKHOUSE, DUCKDB, STARROCKS
from tests import starrocks_env


def dt(*a):
    return datetime.datetime(*a)


def as_text(column) -> list:
    """The engine's rendering as Python values.

    Not simply ``to_pylist()``: ``toString`` over a Binary column hands back a
    String column whose bytes are not UTF-8, and pyarrow raises rather than
    returning it. Undecodable output is *itself* a divergence from the Arrow
    reference, so it is recorded as the raw bytes and compared, not skipped.
    """
    out = []
    for value in column.cast(pa.binary()).to_pylist():
        if value is None:
            out.append(None)
            continue
        try:
            out.append(value.decode())
        except UnicodeDecodeError:
            out.append(value)
    return out


# Values chosen for what they do to a formatter: zero and negative zero, the
# magnitudes where scientific notation starts, sub-second precision, scales
# with and without trailing zeros, and the type boundaries.
CASES = [
    ("string", pa.string(), ["us", "", "a b", "ß", "0", "00", None]),
    ("large_string", pa.large_string(), ["us", "", "ß", None]),
    ("bool", pa.bool_(), [True, False, None]),
    ("int8", pa.int8(), [0, 1, -1, 127, -128, None]),
    ("int32", pa.int32(), [0, -2147483648, 2147483647, None]),
    ("int64", pa.int64(), [0, -1, 9223372036854775807, -9223372036854775808, None]),
    ("uint64", pa.uint64(), [0, 18446744073709551615, None]),
    ("float32", pa.float32(), [0.0, -0.0, 1.0, 2.5, 1e10, 1e-7, None]),
    ("float64", pa.float64(), [0.0, -0.0, 1.0, 0.1, 1e10, 1e16, 1e20, 1e-7,
                               1e-300, float("inf"), None]),
    ("date32", pa.date32(), [datetime.date(1970, 1, 1), datetime.date(2024, 1, 2), None]),
    ("date64", pa.date64(), [datetime.date(2024, 1, 2), None]),
    ("time64", pa.time64("us"), [datetime.time(1, 2, 3, 4), datetime.time(1, 2, 3, 400000),
                                 datetime.time(0, 0), None]),
    ("timestamp_s", pa.timestamp("s"), [dt(2024, 1, 2, 3, 4, 5), None]),
    ("timestamp_ms", pa.timestamp("ms"), [dt(2024, 1, 2, 3, 4, 5, 500000), None]),
    ("timestamp_us", pa.timestamp("us"), [dt(2024, 1, 2, 3, 4, 5, 123456), None]),
    ("timestamp_tz", pa.timestamp("us", tz="America/New_York"), [dt(2024, 1, 2, 3, 4, 5), None]),
    ("decimal_0", pa.decimal128(12, 0), [decimal.Decimal("7"), None]),
    ("decimal_2", pa.decimal128(12, 2), [decimal.Decimal("0.00"), decimal.Decimal("1.10"),
                                         decimal.Decimal("-3.05"), None]),
    ("decimal_6", pa.decimal128(20, 6), [decimal.Decimal("1.000000"), None]),
    # Past the boundary: Python's Decimal — which is what pyarrow's cast
    # produces — switches to scientific notation once the adjusted exponent
    # falls below -6, so zero at scale 7 is '0E-7'. Both engines render it
    # '0.0000000'. Sampled *because* the corpus stopped one step short.
    ("decimal_7", pa.decimal128(20, 7), [decimal.Decimal("0.0000000"),
                                         decimal.Decimal("1.1000000"), None]),
    ("binary", pa.binary(), [b"ab", b"\x00\x01", b"\xff\xfe", None]),
    ("struct", pa.struct([("a", pa.int64())]), [{"a": 1}, {"a": None}, None]),
    ("list", pa.list_(pa.string()), [["a", "b"], None]),
]

# `row_key_matches_arrow`/`hash_text_matches_arrow` answer per Arrow *type*,
# and several of these types are families whose members share one predicate —
# `pa.types.is_timestamp` cannot tell milliseconds from seconds, and
# `is_decimal` cannot tell scale 0 from scale 2. A claim therefore has to hold
# for every member sampled, which is the whole reason `decimal_0` and
# `timestamp_us` are in the list next to the ones that disagree: on their own
# they would justify a claim that is false for their siblings.
FAMILIES = {
    "string": ["string", "large_string"],
    "bool": ["bool"],
    "integer": ["int8", "int32", "int64", "uint64"],
    "float32": ["float32"],
    "float64": ["float64"],
    "date": ["date32", "date64"],
    "time": ["time64"],
    "timestamp": ["timestamp_s", "timestamp_ms", "timestamp_us", "timestamp_tz"],
    # Split, because the predicate is scale-dependent: grouping every decimal
    # under one family would ask whether a claim holding for scale 0 must hold
    # for scale 7, which is the mistake being fixed.
    "decimal_narrow": ["decimal_0", "decimal_2", "decimal_6"],
    "decimal_wide": ["decimal_7"],
    "binary": ["binary"],
    "struct": ["struct"],
    "list": ["list"],
}

# Not a module-level `pytestmark`: the StarRocks half of this file has its own
# gate, and a missing chdb must not silently take it with it.
needs_chdb = pytest.mark.skipif(
    not clickhouse.available(), reason="needs chdb: pip install 'laurelin[clickhouse]'"
)


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    """Ask the live engines to render every case, once."""
    root = tmp_path_factory.mktemp("agreement")
    out = {}
    for name, typ, values in CASES:
        path = root / f"{name}.parquet"
        table = pa.table({"c": pa.array(values, typ)})
        pq.write_table(table, path)

        try:
            arrow_row_key = pc.cast(table.column("c"), pa.string()).to_pylist()
        except (pa.lib.ArrowNotImplementedError, pa.lib.ArrowInvalid):
            # Nested types have no text form at all; binary has one only while
            # the bytes happen to be valid UTF-8. Either way the reference
            # cannot produce a key, so no dialect may claim to reproduce one.
            arrow_row_key = None
        arrow_digest_input = [
            None if v is None else str(v) for v in table.column("c").to_pylist()
        ]

        con = duckdb.connect()
        duck = [r[0] for r in con.execute(
            f"SELECT CAST(c AS VARCHAR) FROM read_parquet('{path}')"
        ).fetchall()]
        con.close()

        scan = clickhouse.scan_expression({"type": "parquet", "path": str(path)})
        ch = as_text(clickhouse.run(f"SELECT toString(c) AS t FROM {scan}").column("t"))

        out[name] = {
            "type": table.schema.field("c").type,
            "arrow_row_key": arrow_row_key,
            "arrow_digest_input": arrow_digest_input,
            DUCKDB.name: duck,
            CLICKHOUSE.name: ch,
        }
    return out


@needs_chdb
@pytest.mark.parametrize("dialect", [DUCKDB, CLICKHOUSE], ids=lambda d: d.name)
@pytest.mark.parametrize("case", [c[0] for c in CASES])
def test_no_dialect_over_claims(measured, dialect, case):
    """The direction that leaks: a dialect saying it agrees when it does not.

    Every claim here is what lets a row filter or a hash mask through to the
    engine, so a false one is a policy silently meaning something else.
    """
    m = measured[case]
    if dialect.row_key_matches_arrow(m["type"]):
        assert m[dialect.name] == m["arrow_row_key"], (
            f"{dialect.name} claims to render {m['type']} as Arrow's row key does, "
            f"but got {m[dialect.name]} where Arrow gives {m['arrow_row_key']}"
        )
    if dialect.hash_text_matches_arrow(m["type"]):
        assert m[dialect.name] == m["arrow_digest_input"], (
            f"{dialect.name} claims to hash {m['type']} the way the Arrow path "
            f"does, but got {m[dialect.name]} where Arrow digests "
            f"{m['arrow_digest_input']}"
        )


@needs_chdb
@pytest.mark.parametrize("dialect", [DUCKDB, CLICKHOUSE], ids=lambda d: d.name)
@pytest.mark.parametrize("family", list(FAMILIES), ids=list(FAMILIES))
def test_no_dialect_under_claims(measured, dialect, family):
    """The direction that denies service, which nothing else would notice.

    If an engine really does agree on a type — every sampled member of it —
    refusing costs a working dataset for no safety gain. Widen the claim (and
    let the test above hold it honest) rather than leaving this failing.
    """
    cases = [measured[c] for c in FAMILIES[family]]
    arrow_type = cases[0]["type"]

    if all(c["arrow_row_key"] is not None and c[dialect.name] == c["arrow_row_key"]
           for c in cases):
        assert dialect.row_key_matches_arrow(arrow_type), (
            f"{dialect.name} renders every sampled {family} exactly as Arrow's "
            "row key does, but the dialect refuses row policies on it"
        )
    if all(c[dialect.name] == c["arrow_digest_input"] for c in cases):
        assert dialect.hash_text_matches_arrow(arrow_type), (
            f"{dialect.name} renders every sampled {family} exactly as the Arrow "
            "digest input, but the dialect refuses hash masks on it"
        )


@needs_chdb
def test_the_disagreements_are_real_and_not_a_test_artefact(measured):
    """Guard against the whole file passing vacuously.

    If a future pyarrow made every engine agree, both tests above would pass
    with nothing measured. These four are the disagreements that motivated the
    refusal, each reproduced end to end in the governance suite.
    """
    assert measured["decimal_2"][CLICKHOUSE.name][0] == "0"
    assert measured["decimal_2"]["arrow_row_key"][0] == "0.00"

    # The scale boundary, where it is *Arrow* that changes form rather than the
    # engine. Both dialects claimed the whole decimal family and the corpus
    # sampled only scales 0, 2 and 6 — so the guard whose entire job is to stop
    # a policy meaning two things on two engines was itself answering wrongly.
    assert measured["decimal_7"][DUCKDB.name][0] == "0.0000000"
    assert measured["decimal_7"]["arrow_row_key"][0] == "0E-7"
    assert not DUCKDB.row_key_matches_arrow(measured["decimal_7"]["type"])
    assert not DUCKDB.hash_text_matches_arrow(measured["decimal_7"]["type"])

    assert measured["float64"][CLICKHOUSE.name][4] == "10000000000"
    assert measured["float64"]["arrow_row_key"][4] == "1e+10"

    assert measured["timestamp_s"][CLICKHOUSE.name][0].endswith(".000")
    assert measured["timestamp_s"]["arrow_row_key"][0].endswith(":05")

    # Bool is the one both SQL engines get "wrong" relative to `str(True)`, and
    # it predates ClickHouse entirely.
    assert measured["bool"][DUCKDB.name][0] == "true"
    assert measured["bool"]["arrow_digest_input"][0] == "True"


@needs_chdb
def test_nested_columns_have_no_text_form_on_any_dialect(measured):
    """A list/struct column cannot be a row policy column anywhere: Arrow — the
    reference — cannot even produce the key. Before the guard, ClickHouse
    filtered on ``['a','b']`` and returned rows, DuckDB returned none, and the
    Arrow path raised. Three renderers, three answers, and only one of them
    served data."""
    for case in ("list", "struct"):
        assert measured[case]["arrow_row_key"] is None
        for dialect in (DUCKDB, CLICKHOUSE):
            assert not dialect.row_key_matches_arrow(measured[case]["type"])
            assert not dialect.hash_text_matches_arrow(measured[case]["type"])


# ---------------------------------------------------------------------------
# StarRocks
# ---------------------------------------------------------------------------
#
# The corpus is loaded into real StarRocks columns, then read back through
# `laurelin.core.starrocks` — so the Arrow side of each comparison is exactly
# the column a governed read produces, not a Parquet file that happens to be
# nearby. That is what makes a claim here mean something about the read path.
#
# Values are chosen for what they do to a formatter, as above. Two are absent
# and their absence is the measurement: StarRocks cannot store a NaN (both
# CAST('nan' AS DOUBLE) and 0/0 evaluate to NULL), and it has no timezone-aware
# timestamp type at all, which is why the dialect claims neither.

SR_CASES = [
    ("string", "VARCHAR(64)", ["us", "", "ß", "0", "00", None]),
    # 128-bit: read back as text, because Arrow has no integer that wide.
    ("largeint", "LARGEINT", ["12345678901234567890", "-1", None]),
    ("bool", "BOOLEAN", [True, False, None]),
    ("int8", "TINYINT", [0, 1, -1, 127, -128, None]),
    ("int32", "INT", [0, -2147483648, 2147483647, None]),
    ("int64", "BIGINT", [0, -1, 9223372036854775807, None]),
    ("float32", "FLOAT", [0.0, -0.0, 1.0, 2.5, 1e10, 1e-7, None]),
    ("float64", "DOUBLE", [0.0, -0.0, 1.0, 0.1, 1e10, 1e16, 1e20, 1e-7, 1e-300, None]),
    ("date", "DATE", ["1970-01-01", "2024-01-02", None]),
    ("datetime_s", "DATETIME", ["2024-01-02 03:04:05", None]),
    ("datetime_ms", "DATETIME", ["2024-01-02 03:04:05.500", None]),
    ("datetime_us", "DATETIME", ["2024-01-02 03:04:05.123456", None]),
    ("decimal_0", "DECIMAL(12,0)", ["7", "0", None]),
    ("decimal_2", "DECIMAL(12,2)", ["0.00", "1.10", "-3.05", None]),
    ("decimal_6", "DECIMAL(20,6)", ["1.000000", None]),
    # Past the boundary; see the DuckDB corpus above. Measured on a live
    # server: the engine gives '0.0000000' where pyarrow gives '0E-7'.
    ("decimal_7", "DECIMAL(20,7)", ["0.0000000", "1.1000000", None]),
]

SR_FAMILIES = {
    "string": ["string", "largeint"],
    "bool": ["bool"],
    "integer": ["int8", "int32", "int64"],
    "float32": ["float32"],
    "float64": ["float64"],
    "date": ["date"],
    "timestamp": ["datetime_s", "datetime_ms", "datetime_us"],
    "decimal_narrow": ["decimal_0", "decimal_2", "decimal_6"],
    "decimal_wide": ["decimal_7"],
}


def _measure_starrocks(time_zone: str) -> dict:
    """Load the corpus, then read it back the way a governed read does."""
    out = {}
    for name, ddl, values in SR_CASES:
        table = starrocks_env.load(
            f"`i` INT, `c` {ddl} NULL", list(enumerate(values)), key="i"
        )
        try:
            source = starrocks_env.source(table)
            con = starrocks_env.connect()
            try:
                cur = con.cursor()
                cur.execute(f"SET time_zone = '{time_zone}'")
                cur.close()
                schema = starrocks.schema_of(source, con)
                scan = starrocks.scan_expression(source)
                column = starrocks.run(
                    f"SELECT `c` FROM {scan} ORDER BY `i`", con=con, schema=schema
                ).column("c")
                engine = starrocks.run(
                    f"SELECT CAST(`c` AS STRING) AS t FROM {scan} ORDER BY `i`",
                    con=con, schema=schema,
                ).column("t").to_pylist()
            finally:
                con.close()
        finally:
            starrocks_env.drop(table)

        out[name] = {
            "type": schema.field("c").type,
            "engine": engine,
            "arrow_row_key": pc.cast(column, pa.string()).to_pylist(),
            "arrow_digest_input": [
                None if v is None else str(v) for v in column.to_pylist()
            ],
        }
    return out


@pytest.fixture(scope="module")
def sr_measured():
    return _measure_starrocks("UTC")


@starrocks_env.needs_starrocks
@pytest.mark.parametrize("case", [c[0] for c in SR_CASES])
def test_starrocks_does_not_over_claim(sr_measured, case):
    """The direction that leaks: a dialect saying it agrees when it does not."""
    m = sr_measured[case]
    if STARROCKS.row_key_matches_arrow(m["type"]):
        assert m["engine"] == m["arrow_row_key"], (
            f"starrocks claims to render {m['type']} as Arrow's row key does, "
            f"but got {m['engine']} where Arrow gives {m['arrow_row_key']}"
        )
    if STARROCKS.hash_text_matches_arrow(m["type"]):
        assert m["engine"] == m["arrow_digest_input"], (
            f"starrocks claims to hash {m['type']} the way the Arrow path does, "
            f"but got {m['engine']} where Arrow digests {m['arrow_digest_input']}"
        )


@starrocks_env.needs_starrocks
@pytest.mark.parametrize("family", list(SR_FAMILIES), ids=list(SR_FAMILIES))
def test_starrocks_does_not_under_claim(sr_measured, family):
    """The direction that denies service, which nothing else would notice."""
    cases = [sr_measured[c] for c in SR_FAMILIES[family]]
    arrow_type = cases[0]["type"]

    if all(c["engine"] == c["arrow_row_key"] for c in cases):
        assert STARROCKS.row_key_matches_arrow(arrow_type), (
            f"starrocks renders every sampled {family} exactly as Arrow's row "
            "key does, but the dialect refuses row policies on it"
        )
    if all(c["engine"] == c["arrow_digest_input"] for c in cases):
        assert STARROCKS.hash_text_matches_arrow(arrow_type), (
            f"starrocks renders every sampled {family} exactly as the Arrow "
            "digest input, but the dialect refuses hash masks on it"
        )


@starrocks_env.needs_starrocks
def test_the_starrocks_disagreements_are_real_and_not_a_test_artefact(sr_measured):
    """Guard against the whole arm passing vacuously.

    Each of these is a refusal the dialect makes, reproduced end to end.
    """
    # Boolean: portable on DuckDB *and* ClickHouse, and not here.
    assert sr_measured["bool"]["engine"][0] == "1"
    assert sr_measured["bool"]["arrow_row_key"][0] == "true"

    # DOUBLE loses scientific notation where Arrow keeps it...
    assert sr_measured["float64"]["engine"][4] == "10000000000"
    assert sr_measured["float64"]["arrow_row_key"][4] == "1e+10"
    # ...and keeps it, differently spelled, where Arrow does too.
    assert sr_measured["float64"]["engine"][7] == "1e-07"
    assert sr_measured["float64"]["arrow_row_key"][7] == "1e-7"

    # A DATETIME with no sub-second part renders without one.
    assert sr_measured["datetime_s"]["engine"][0] == "2024-01-02 03:04:05"
    assert sr_measured["datetime_s"]["arrow_row_key"][0].endswith(".000000")

    # Decimal keeps its scale here and does not on ClickHouse, which is why the
    # two dialects' type tables differ rather than one being a copy.
    assert sr_measured["decimal_2"]["engine"][0] == "0.00"
    assert sr_measured["decimal_2"]["arrow_row_key"][0] == "0.00"
    assert STARROCKS.row_key_matches_arrow(sr_measured["decimal_2"]["type"])
    assert not CLICKHOUSE.row_key_matches_arrow(sr_measured["decimal_2"]["type"])

    # …but only up to scale 6. Past that it is *Arrow* that changes form, and
    # the dialect claimed the whole family — so the guard that exists to stop a
    # policy meaning two things on two engines was itself answering wrongly.
    assert sr_measured["decimal_7"]["engine"][0] == "0.0000000"
    assert sr_measured["decimal_7"]["arrow_row_key"][0] == "0E-7"
    assert not STARROCKS.row_key_matches_arrow(sr_measured["decimal_7"]["type"])
    assert not STARROCKS.hash_text_matches_arrow(sr_measured["decimal_7"]["type"])


@starrocks_env.needs_starrocks
def test_the_type_tables_do_not_move_with_the_session_time_zone():
    """The claim that made timestamps admissible as a hash input.

    StarRocks DATETIME is timezone-naive, so the session variable should not
    reach the rendering — but "should not" is how a policy quietly changes
    meaning per connection. Both admitted sets are re-derived under a second
    time zone and required to be identical.
    """
    utc = _measure_starrocks("UTC")
    other = _measure_starrocks("America/New_York")

    for case in utc:
        assert utc[case]["engine"] == other[case]["engine"], (
            f"{case} renders differently under a different session time zone; "
            "the dialect's claims are per-connection and must be narrowed"
        )
