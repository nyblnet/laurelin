"""Three renderers, one decision — and the earlier ones must not have moved.

Adding an SQL engine to a governance layer is mostly a refactor, and a refactor
of enforcement code is exactly where a leak gets introduced without anyone
noticing. So this file opens with *golden* tests: the strings DuckDB emits are
checked in as literals copied from the renderer before dialects existed, and
the strings ClickHouse emits are checked in as they were before StarRocks
existed. If a byte changes, this fails, and "I only added a dialect" stops
being a claim and becomes a measurement.

The rest is each dialect's own rules. Every one of them was measured against
the live engine — chdb in this repo's venv for ClickHouse, a StarRocks
container for StarRocks — rather than read from a manual; see the comments in
``laurelin/core/dialects.py``. The StarRocks tests that need the server skip
without ``LAURELIN_TEST_STARROCKS`` (see ``tests/starrocks_env.py``); the ones
that only compare strings run everywhere, which is most of them.

Last: dispatch. ``sql_dialect`` and ``_source_reader`` must both be total, and
a kind that neither knows about must raise rather than quietly being read by
DuckDB.
"""

import re
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from laurelin.core import clickhouse, starrocks
from laurelin.core.dialects import CLICKHOUSE, DIALECTS, DUCKDB, STARROCKS
from laurelin.core.models import DATASET_KINDS, MaskMode
from laurelin.core.permissions import PolicyDecision, PolicyRenderError, SqlPolicy
from tests import starrocks_env

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


@pytest.mark.parametrize(
    "authored, real",
    [
        ("ssn ", "ssn"),       # a trailing space is invisible in a policy form
        ("ssn", "ssn "),       # ...and so is one in the source schema
        ("ﬁle", "file"),  # U+FB01 LATIN SMALL LIGATURE FI
        ("ＳＳＮ", "ssn"),      # fullwidth forms normalise to ASCII
    ],
)
def test_a_mask_that_misses_by_whitespace_or_unicode_form_is_also_a_refusal(authored, real):
    """Case was never the only way to name a column *almost* right.

    Measured against a ClickHouse dataset whose source file Laurelin does not
    own: renaming the masked column from ``ssn`` to ``ssn `` served
    ``TOPSECRET`` in the clear, with no error and no audit entry, because the
    old guard only compared casefold. A rename to a genuinely different name
    still passes through by design — schema evolution must not deny a whole
    dataset — but "the same name, mistyped" must not.
    """
    decision = PolicyDecision(masks=[(authored, MaskMode.redact)])
    for dialect in (DUCKDB, CLICKHOUSE):
        with pytest.raises(PolicyRenderError, match="whitespace and unicode form"):
            SqlPolicy.render(decision, ["id", real], dialect=dialect)


def test_a_casefold_collision_does_not_refuse_a_column_that_really_exists():
    """``'ß'.casefold()`` is ``'ss'``.

    The guard used to fold every column into a dict and look the mask up in
    it, so a dataset holding *both* ``ss`` and ``ß`` reported the real column
    ``ss`` as a typo for its neighbour and refused every read through the SQL
    path — naming a column the operator never wrote and telling them to fix a
    policy that was already correct. Fail-closed, but permanently unreadable.
    """
    decision = PolicyDecision(masks=[("ss", MaskMode.redact)])
    for dialect in (DUCKDB, CLICKHOUSE):
        policy = SqlPolicy.render(decision, ["id", "ss", "ß"], dialect=dialect)
        assert "'***' AS " + dialect.quote("ss") in policy.select_list
        assert dialect.quote("ß") in policy.select_list

    # The typo it exists to catch still is one, and the message names both
    # candidates rather than picking whichever hashed last.
    with pytest.raises(PolicyRenderError) as exc:
        SqlPolicy.render(
            PolicyDecision(masks=[("SS", MaskMode.redact)]), ["ss", "ß"]
        )
    assert "'ss'" in str(exc.value) and "'ß'" in str(exc.value)


def test_two_masks_on_one_column_compose_instead_of_the_last_one_winning():
    """``redact`` then ``hash`` must digest ``'***'``, not the secret.

    The renderer used to collapse the mask list into a dict, so the last entry
    won and was applied to the *raw* column: the SQL path emitted
    ``sha256('TOPSECRET')`` where the Arrow reference emits ``sha256('***')``.
    Not plaintext, but a directly rainbow-tableable token derived from the
    exact value the first mask was there to remove.
    """
    import hashlib

    secret = hashlib.sha256(b"TOPSECRET").hexdigest()[:16]
    redacted = hashlib.sha256(b"***").hexdigest()[:16]
    assert secret != redacted

    decision = PolicyDecision(masks=[("ssn", MaskMode.redact), ("ssn", MaskMode.hash)])
    for dialect in (DUCKDB, CLICKHOUSE):
        select = SqlPolicy.render(
            decision, ["ssn"], dialect=dialect, column_types=TYPES
        ).select_list
        assert "'***'" in select, "the redact must survive underneath the hash"
        assert dialect.quote("ssn") not in select.split(" AS ")[0], (
            "the hash must not reach past the redact to the raw column"
        )

    # null then hash is the other order that mattered: NULL hashes to NULL.
    nulled = PolicyDecision(masks=[("ssn", MaskMode.null), ("ssn", MaskMode.hash)])
    select = SqlPolicy.render(nulled, ["ssn"], column_types=TYPES).select_list
    assert 'NULLIF("ssn", "ssn")' in select


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------

pytestmark_ch = pytest.mark.skipif(
    not clickhouse.available(), reason="needs chdb: pip install 'laurelin[clickhouse]'"
)


def test_the_three_quoters_are_not_the_same_function():
    """Stated as an assertion because the tempting refactor is to share one.

    StarRocks and ClickHouse both spell a plain name with backticks, which is
    exactly the coincidence that makes sharing look free. They diverge on the
    escape: a backslash is an escape character inside a ClickHouse identifier
    and an ordinary character inside a StarRocks one.
    """
    quoters = [DUCKDB.quote, CLICKHOUSE.quote, STARROCKS.quote]
    assert len({q.__func__ for q in quoters}) == 3

    assert DUCKDB.quote("x") == '"x"'
    assert CLICKHOUSE.quote("x") == STARROCKS.quote("x") == "`x`"
    assert CLICKHOUSE.quote("a\\") == "`a\\\\`"
    assert STARROCKS.quote("a\\") == "`a\\`"


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


def test_literal_is_the_only_way_a_value_becomes_clickhouse_sql():
    """The structural half of "no injection", which behaviour cannot cover.

    ClickHouse has no binding channel, so ``ClickHouseDialect.literal`` escapes
    values into the statement text. Fuzzing shows *that function* holds — 4001
    hostile payloads round-tripped byte-exact on the real engine. What fuzzing
    cannot show is that it stays the only door: a future call site formatting a
    value into ClickHouse SQL some other way would be a genuine injection and
    every value test would still pass, because they only ever exercise the
    door that exists.

    So this walks the AST instead. Every statement handed to ``clickhouse.run``
    must be a bare name or an f-string whose interpolations are all names, and
    each of those names must come from ``scan_expression`` or
    ``SqlDialect.assemble`` — the two functions that are themselves built out
    of ``quote`` and ``literal``. Adding a third way to build the string is
    meant to fail here and be argued for.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "laurelin"
    approved_builders = {"scan_expression", "assemble"}
    offenders = []

    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Where each local name last came from, for the check below.
            sources: dict[str, set[str]] = {}
            for node in ast.walk(func):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    called = node.value.func
                    name = getattr(called, "attr", getattr(called, "id", ""))
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            sources.setdefault(target.id, set()).add(name)

            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                called = node.func
                # `clickhouse.run(...)` anywhere, and the bare `run(...)` that
                # the module itself uses — the backend is the likeliest place
                # for a second construction site to be added, so it cannot be
                # the one place the guard does not look.
                qualified = (
                    getattr(called, "attr", None) == "run"
                    and getattr(getattr(called, "value", None), "id", None) == "clickhouse"
                )
                internal = (
                    path.name == "clickhouse.py" and getattr(called, "id", None) == "run"
                )
                if not (qualified or internal):
                    continue
                where = f"{path.relative_to(root)}:{node.lineno}"
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Name):
                    names = [arg.id]
                elif isinstance(arg, ast.JoinedStr):
                    parts = [p for p in arg.values if isinstance(p, ast.FormattedValue)]
                    if not all(isinstance(p.value, ast.Name) for p in parts):
                        offenders.append(f"{where}: interpolates an expression")
                        continue
                    names = [p.value.id for p in parts]
                else:
                    offenders.append(f"{where}: statement is not a name or f-string")
                    continue
                for name in names:
                    if not sources.get(name, set()) & approved_builders:
                        offenders.append(
                            f"{where}: {name!r} is not built by "
                            + "/".join(sorted(approved_builders))
                        )

    assert offenders == [], (
        "ClickHouse SQL must be assembled only from scan_expression/assemble, "
        f"which are built from quote()/literal(): {offenders}"
    )


def test_no_module_rolls_its_own_clickhouse_escaper():
    """The other way a second door appears: someone copies the escape inline.

    ``literal`` is three characters of logic and looks trivial enough to
    reimplement at a call site — where it would then not be the thing the fuzz
    test covers, and the backslash-first ordering is exactly the detail people
    get backwards.
    """
    root = Path(__file__).resolve().parents[1] / "laurelin"
    escape = re.compile(r"""replace\(\s*['"]\\\\?['"]""")
    offenders = [
        str(f.relative_to(root))
        for f in root.rglob("*.py")
        if escape.search(f.read_text()) and f.name != "dialects.py"
    ]
    assert offenders == [], f"quote/literal live in dialects.py only: {offenders}"


# ---------------------------------------------------------------------------
# The golden test, second half: ClickHouse's bytes, frozen too
# ---------------------------------------------------------------------------
#
# The DuckDB set above was captured before dialects existed. This one is
# captured before StarRocks existed, and it exists for the same reason: adding
# a third dialect is a refactor of enforcement code, and "I only added a
# dialect" has to be a measurement rather than a claim. Every string below was
# read out of the renderer on the commit that added ClickHouse.

CLICKHOUSE_GOLDEN = {
    "no policy": (
        PolicyDecision(),
        COLUMNS,
        '`id`, `region`, `ssn`, `we"ird`',
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
        '`id`, `region`, if(0, `ssn`, NULL) AS `ssn`, `we"ird`',
        "TRUE",
        [],
    ),
    "mask redact": (
        PolicyDecision(masks=[("ssn", MaskMode.redact)]),
        COLUMNS,
        '`id`, `region`, \'***\' AS `ssn`, `we"ird`',
        "TRUE",
        [],
    ),
    "mask hash": (
        PolicyDecision(masks=[("ssn", MaskMode.hash)]),
        COLUMNS,
        "`id`, `region`, CASE WHEN `ssn` IS NULL THEN NULL ELSE "
        'substring(lower(hex(SHA256(toString(`ssn`)))), 1, 16) END AS `ssn`, `we"ird`',
        "TRUE",
        [],
    ),
    "row policy, three values": (
        PolicyDecision(row_column="region", allowed_values=["us", "eu", "apac"]),
        COLUMNS,
        '`id`, `region`, `ssn`, `we"ird`',
        "toString(`region`) IN ('us', 'eu', 'apac')",
        [],
    ),
    "row policy and two masks": (
        PolicyDecision(
            row_column="region",
            allowed_values=["us"],
            masks=[("ssn", MaskMode.hash), ("id", MaskMode.null)],
        ),
        COLUMNS,
        "if(0, `id`, NULL) AS `id`, `region`, CASE WHEN `ssn` IS NULL THEN NULL "
        "ELSE substring(lower(hex(SHA256(toString(`ssn`)))), 1, 16) END AS `ssn`, "
        '`we"ird`',
        "toString(`region`) IN ('us')",
        [],
    ),
    "quoting a column with a quote in it": (
        PolicyDecision(masks=[('we"ird', MaskMode.redact)]),
        COLUMNS,
        '`id`, `region`, `ssn`, \'***\' AS `we"ird`',
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


@pytest.mark.parametrize("case", list(CLICKHOUSE_GOLDEN), ids=list(CLICKHOUSE_GOLDEN))
def test_clickhouse_rendering_has_not_moved(case):
    decision, columns, select_list, where, params = CLICKHOUSE_GOLDEN[case]
    policy = SqlPolicy.render(
        decision, columns, dialect=CLICKHOUSE, column_types=TYPES
    )
    assert policy.select_list == select_list
    assert policy.where == where
    assert policy.params == params
    assert policy.dialect is CLICKHOUSE


def test_clickhouse_statement_assembly_has_not_moved():
    """The nested form, byte for byte, including where the settings go."""
    assert CLICKHOUSE.assemble("*", "file('/x', Parquet)", "TRUE") == (
        "SELECT * FROM (SELECT * FROM file('/x', Parquet) WHERE TRUE)"
    )
    assert CLICKHOUSE.assemble(
        "*", "t", "FALSE", limit=10, settings="transform_null_in=0"
    ) == (
        "SELECT * FROM (SELECT * FROM t WHERE FALSE) LIMIT 10 "
        "SETTINGS transform_null_in=0"
    )


# ---------------------------------------------------------------------------
# StarRocks: the strings, checked in
# ---------------------------------------------------------------------------
#
# Unlike the two sets above these are not a *regression* pin — there is nothing
# earlier to regress from. They are a pin on the expressions the engine was
# measured to want, so that a "tidy-up" of any one of them has to argue with a
# recorded measurement. What each choice was measured against is in the
# docstrings of laurelin/core/dialects.py.

STARROCKS_GOLDEN = {
    "no policy": (
        PolicyDecision(),
        COLUMNS,
        '`id`, `region`, `ssn`, `we"ird`',
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
        '`id`, `region`, if(FALSE, `ssn`, NULL) AS `ssn`, `we"ird`',
        "TRUE",
        [],
    ),
    "mask redact": (
        PolicyDecision(masks=[("ssn", MaskMode.redact)]),
        COLUMNS,
        '`id`, `region`, \'***\' AS `ssn`, `we"ird`',
        "TRUE",
        [],
    ),
    "mask hash": (
        PolicyDecision(masks=[("ssn", MaskMode.hash)]),
        COLUMNS,
        "`id`, `region`, CASE WHEN `ssn` IS NULL THEN NULL ELSE "
        'substr(sha2(CAST(`ssn` AS STRING), 256), 1, 16) END AS `ssn`, `we"ird`',
        "TRUE",
        [],
    ),
    "row policy, three values": (
        PolicyDecision(row_column="region", allowed_values=["us", "eu", "apac"]),
        COLUMNS,
        '`id`, `region`, `ssn`, `we"ird`',
        "CAST(`region` AS STRING) IN (?, ?, ?)",
        ["us", "eu", "apac"],
    ),
    "row policy and two masks": (
        PolicyDecision(
            row_column="region",
            allowed_values=["us"],
            masks=[("ssn", MaskMode.hash), ("id", MaskMode.null)],
        ),
        COLUMNS,
        "if(FALSE, `id`, NULL) AS `id`, `region`, CASE WHEN `ssn` IS NULL THEN "
        "NULL ELSE substr(sha2(CAST(`ssn` AS STRING), 256), 1, 16) END AS `ssn`, "
        '`we"ird`',
        "CAST(`region` AS STRING) IN (?)",
        ["us"],
    ),
    "quoting a column with a quote in it": (
        PolicyDecision(masks=[('we"ird', MaskMode.redact)]),
        COLUMNS,
        '`id`, `region`, `ssn`, \'***\' AS `we"ird`',
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


@pytest.mark.parametrize("case", list(STARROCKS_GOLDEN), ids=list(STARROCKS_GOLDEN))
def test_starrocks_renders_the_measured_expressions(case):
    decision, columns, select_list, where, params = STARROCKS_GOLDEN[case]
    policy = SqlPolicy.render(
        decision, columns, dialect=STARROCKS, column_types=TYPES
    )
    assert policy.select_list == select_list
    assert policy.where == where
    assert policy.params == params
    assert policy.dialect is STARROCKS


def test_starrocks_does_not_inherit_clickhouses_hash_or_null_mask():
    """The two copy-paste hazards, called out one by one.

    ``lower(hex(...))`` around a digest is *mandatory* on ClickHouse and
    catastrophic here: ``sha2`` already returns hex, so wrapping it measures
    128 characters instead of 64 and every hash mask silently stops joining.
    ``if(0, ...)`` is ClickHouse's boolean; StarRocks wants ``FALSE``.
    """
    hashed = STARROCKS.hash_mask("`ssn`")
    assert "hex(" not in hashed and "SHA256(" not in hashed
    assert "sha2(CAST(`ssn` AS STRING), 256)" in hashed
    assert STARROCKS.null_mask("`amt`") == "if(FALSE, `amt`, NULL)"
    assert "NULLIF" not in STARROCKS.null_mask("`amt`")
    assert STARROCKS.to_text(STARROCKS.quote("region")) == "CAST(`region` AS STRING)"


def test_starrocks_binds_and_has_no_escaper_at_all():
    """The single most consequential decision in this dialect.

    Stacked statements execute on StarRocks and the client flag does not stop
    them (measured — see ``test_starrocks_stacked_statements_execute``), so an
    escaping defect here would be a remote *write*, not a wrong read. The
    defence is that there is no escaper: ``literal`` raises, and the row filter
    carries placeholders with the values in ``params``.
    """
    assert STARROCKS.binds_values is True
    assert STARROCKS.placeholder(0) == "?"
    with pytest.raises(NotImplementedError):
        STARROCKS.literal("us")

    hostile = ["us') OR 1=1 --", "a\nb", "\\", "'", "; DROP TABLE t"]
    policy = SqlPolicy.render(
        PolicyDecision(row_column="region", allowed_values=hostile),
        ["region"],
        dialect=STARROCKS,
        column_types=TYPES,
    )
    assert policy.where == "CAST(`region` AS STRING) IN (?, ?, ?, ?, ?)"
    assert policy.params == hostile
    for value in hostile:
        assert value not in policy.where


def test_starrocks_quote_refuses_a_backtick_rather_than_mangling_it():
    """Measured: doubling does not escape a backtick here, it *drops* it.

    ``CREATE TABLE t (`a``b` INT)`` produces a column literally named ``ab`` —
    proved by a subsequent real ``ab`` column failing with "Duplicate column
    name". So there is no spelling of the name that addresses the intended
    column, and a mask aimed at it would land somewhere else.
    """
    with pytest.raises(ValueError, match="backtick"):
        STARROCKS.quote("a`b")
    # And the refusal reaches the renderer rather than being a local nicety.
    with pytest.raises(ValueError, match="backtick"):
        SqlPolicy.render(
            PolicyDecision(masks=[("a`b", MaskMode.redact)]),
            ["id", "a`b"],
            dialect=STARROCKS,
        )


def test_starrocks_nests_the_filter_and_names_the_derived_table():
    sql = STARROCKS.assemble(
        "'***' AS `region`",
        "`db`.`events`",
        "CAST(`region` AS STRING) IN (?)",
        limit=5,
        settings="query_timeout=60, query_mem_limit=2147483648",
    )
    assert sql == (
        "SELECT /*+ SET_VAR(query_timeout=60, query_mem_limit=2147483648) */ "
        "'***' AS `region` FROM (SELECT * FROM `db`.`events` "
        "WHERE CAST(`region` AS STRING) IN (?)) t LIMIT 5"
    )
    # The alias is not decoration: without it StarRocks refuses the statement
    # with error 1248, "Every derived table must have its own alias".
    assert sql.rstrip().endswith("LIMIT 5") and ") t " in sql
    assert sql.index("WHERE") > sql.index("(SELECT *")
    # No hint asked for, no comment emitted.
    assert "SET_VAR" not in STARROCKS.assemble("*", "t", "TRUE")


@pytest.mark.parametrize("arrow_type, row_key, hash_text", [
    (pa.string(), True, True),
    (pa.large_string(), True, True),
    (pa.int64(), True, True),
    (pa.int8(), True, True),
    (pa.date32(), True, True),
    (pa.decimal128(12, 2), True, True),
    (pa.decimal128(20, 6), True, True),
    # Scale 7 is where Arrow's own rendering changes, not the engine's: a
    # Decimal whose adjusted exponent falls below -6 prints in scientific
    # notation, so pyarrow gives '0E-7' where both DuckDB and StarRocks give
    # '0.0000000'. Measured against a live server.
    (pa.decimal128(20, 7), False, False),
    (pa.decimal128(38, 18), False, False),
    # Boolean is row-key portable on DuckDB *and* ClickHouse. Here
    # CAST(b AS STRING) is '1' where Arrow's cast is 'true', so a tenant policy
    # on a boolean column would admit the wrong half of the table.
    (pa.bool_(), False, False),
    (pa.float64(), False, False),
    (pa.float32(), False, False),
    # A DATETIME renders without the sub-second zeros Arrow's cast keeps, so it
    # is not a row key — but it is exactly `str(datetime)`, so it *is* a
    # faithful hash input. Measured under two session time zones.
    (pa.timestamp("us"), False, True),
    (pa.timestamp("s"), False, True),
    # Nothing was measured for a tz-aware column because StarRocks has no
    # column type that produces one.
    (pa.timestamp("us", tz="UTC"), False, False),
    (pa.list_(pa.string()), False, False),
    (pa.struct([("a", pa.int64())]), False, False),
])
def test_the_starrocks_type_tables_are_what_was_measured(arrow_type, row_key, hash_text):
    assert STARROCKS.row_key_matches_arrow(arrow_type) is row_key
    assert STARROCKS.hash_text_matches_arrow(arrow_type) is hash_text


@pytest.mark.parametrize("scale, portable", [
    (0, True), (2, True), (6, True), (7, False), (9, False), (18, False),
])
def test_wide_decimals_are_refused_by_every_dialect_that_claims_decimals(scale, portable):
    """The boundary is a property of Arrow's text form, so it is the same on
    both engines that claim decimals at all — and it is checkable with no
    engine present, which is why it is asserted here as well as in
    ``test_text_agreement``.

    Both dialects claimed the whole decimal family. The corpus sampled scales
    0, 2 and 6 — one step below the boundary — which is why nothing caught it,
    and this guard exists so re-widening the claim has to be deliberate.
    """
    t = pa.decimal128(38, scale)
    assert DUCKDB.row_key_matches_arrow(t) is portable
    assert DUCKDB.hash_text_matches_arrow(t) is portable
    assert STARROCKS.row_key_matches_arrow(t) is portable
    assert STARROCKS.hash_text_matches_arrow(t) is portable
    # ClickHouse refuses decimals outright and is unaffected either way.
    assert CLICKHOUSE.row_key_matches_arrow(t) is False


def test_boolean_is_a_row_key_on_the_other_two_dialects_and_not_here():
    """Written as a comparison because that is what makes it a trap: copying
    either existing type table would have admitted it."""
    assert DUCKDB.row_key_matches_arrow(pa.bool_())
    assert CLICKHOUSE.row_key_matches_arrow(pa.bool_())
    assert not STARROCKS.row_key_matches_arrow(pa.bool_())


# ---------------------------------------------------------------------------
# Dispatch must be total
# ---------------------------------------------------------------------------
#
# The bug this closes was latent rather than live: `sql_dialect` returned
# "duckdb" for everything that was not ClickHouse, and `_source_reader`
# returned a DuckDB reader for everything that was not ClickHouse. A new
# source-scanned kind would therefore have been read with DuckDB's *flat*
# statement and DuckDB's *quoter* — and `source_table`'s dialect-mismatch guard
# could not have caught it, because it compares the policy's dialect with the
# reader's and both would have said "duckdb".

@pytest.mark.parametrize("kind", DATASET_KINDS)
def test_every_dataset_kind_maps_to_a_registered_dialect(kind):
    from laurelin.core.models import DatasetInfo

    name = DatasetInfo(name="d", kind=kind).sql_dialect
    assert name in DIALECTS, f"kind {kind!r} names a dialect that does not exist"


def test_an_unknown_kind_raises_instead_of_defaulting_to_duckdb():
    from laurelin.core.models import DatasetInfo

    info = DatasetInfo(name="d", kind="some_new_engine")
    with pytest.raises(ValueError, match="no SQL dialect"):
        info.sql_dialect


def test_the_source_reader_registry_raises_instead_of_defaulting():
    from laurelin.catalog.catalog import DatasetCatalog
    from laurelin.core.models import DatasetInfo

    catalog = DatasetCatalog.__new__(DatasetCatalog)  # no workspace needed
    info = DatasetInfo(name="d", kind="some_new_engine")
    with pytest.raises(ValueError, match="No source reader"):
        catalog._source_reader(info)


@pytest.mark.parametrize("kind", DATASET_KINDS)
def test_a_source_scanned_kind_has_a_reader_in_its_own_dialect(kind):
    """The two registries are written independently, so they can disagree —
    and a policy compiled for one engine and executed by another is a
    plausible-looking query with different semantics."""
    from laurelin.catalog.catalog import DatasetCatalog
    from laurelin.core.models import DatasetInfo

    info = DatasetInfo(name="d", kind=kind)
    if not info.scans_at_source:
        return
    reader = DatasetCatalog._SOURCE_READERS[kind]
    assert reader.dialect.name == info.sql_dialect


# ---------------------------------------------------------------------------
# StarRocks, against the live engine
# ---------------------------------------------------------------------------

SR_IDENTIFIERS = ["plain", "sp ace", "sel ect", "FALSE", "Case", "case",
                  'a"b', "a'b", "semi;colon", "dash-dash"]


@starrocks_env.needs_starrocks
@pytest.mark.parametrize("name", SR_IDENTIFIERS, ids=[repr(n) for n in SR_IDENTIFIERS])
def test_the_starrocks_quoter_addresses_the_column_it_names(name):
    """Write a one-column table, read it back through the quoter, and require
    the value written. A quoter that resolves to *some other* column is worse
    than one that errors: the mask lands on the wrong data, silently."""
    table = starrocks_env.load(
        f"`id` INT, {STARROCKS.quote(name)} VARCHAR(16)", [(1, "VALUE")]
    )
    try:
        source = starrocks_env.source(table)
        sql = (f"SELECT {STARROCKS.quote(name)} FROM "
               f"{starrocks.scan_expression(source)}")
        got = starrocks.run(sql, source=source)
    except starrocks.StarRocksError:
        return  # a refusal is acceptable; a wrong answer is not
    finally:
        starrocks_env.drop(table)
    assert got.column(0).to_pylist() == ["VALUE"]


@starrocks_env.needs_starrocks
def test_duckdbs_quoter_on_starrocks_returns_a_constant_and_fails_open():
    """The measurement that makes sharing a quoter indefensible.

    A double-quoted name is a *string literal* on StarRocks. So DuckDB's quoter
    turns ``SELECT "s"`` into a constant column, and — the part that matters —
    turns the row filter ``CAST("s" AS STRING) IN ('s')`` into ``'s' IN ('s')``,
    which is true for every row including the ones whose value is NULL. Fails
    open, in the one place that must not.
    """
    table = starrocks_env.load(
        "`id` INT, `s` VARCHAR(16)", [(1, "us"), (2, None), (3, "eu")]
    )
    try:
        source = starrocks_env.source(table)
        scan = starrocks.scan_expression(source)

        wrong = starrocks.run(
            f"SELECT `id` FROM {scan} WHERE CAST({DUCKDB.quote('s')} AS STRING) "
            "IN ('s')",
            source=source,
        )
        assert wrong.num_rows == 3, "the silent fail-open: every row, NULL included"

        right = starrocks.run(
            f"SELECT `id` FROM {scan} WHERE CAST({STARROCKS.quote('s')} AS STRING) "
            "IN ('us')",
            source=source,
        )
        assert right.column(0).to_pylist() == [1]

        # ClickHouse's quoter fails *closed* here — still wrong, but loudly.
        with pytest.raises(starrocks.StarRocksError):
            starrocks.run(
                f"SELECT {CLICKHOUSE.quote('a\\`b')} FROM {scan}", source=source
            )
    finally:
        starrocks_env.drop(table)


@starrocks_env.needs_starrocks
def test_starrocks_binds_every_hostile_value_byte_for_byte():
    """Fidelity, not "the query ran".

    This is the property that lets ``literal`` be absent. A value that arrives
    shortened or altered matches rows the policy never allowed — which is
    exactly what ClickHouse's named-parameter channel does, and why that engine
    needs an escaper instead.
    """
    values = [
        "", "plain", "us", "\\", "a\\", "\\\\", "'", "''", "o'brien",
        "a\nb", "a\r\nb", "a\tb", "a\0b", "%s", "%", "_", "us') OR 1=1 --",
        "-- comment", "/*x*/", ";DROP", "üñí", "日本語", "emoji 🙂", "?", "$1",
        "\\N", "NULL", "`", "``", "\\`", "a" * 300, "\\g", "#",
    ]
    values += [f"a{chr(c)}b" for c in range(1, 300)]
    assert len(values) >= 300

    con = starrocks_env.connect()
    try:
        for start in range(0, len(values), 50):
            batch = values[start:start + 50]
            select = ", ".join(f"length(?) AS c{i}" for i, _ in enumerate(batch))
            got = starrocks.run(f"SELECT {select}", batch, con).to_pylist()[0]
            assert [got[f"c{i}"] for i in range(len(batch))] == [
                len(v.encode()) for v in batch
            ], f"batch starting at {start} did not round-trip"
    finally:
        con.close()


@starrocks_env.needs_starrocks
def test_starrocks_stacked_statements_execute_so_we_never_build_sql_from_values():
    """Documented, not mitigated — because it cannot be mitigated client-side.

    This is the blast radius that makes ``StarRocksDialect.literal`` raise. A
    future contributor who adds an escaper "just for the scan expression"
    should have to read this test first. Note what is *not* asserted: that
    passing ``-ClientFlag.MULTI_STATEMENTS`` helps. Measured, the client
    reports the flag off and the second statement still runs.
    """
    table = starrocks_env.load("`id` INT, `s` VARCHAR(64)", [(1, "us")])
    victim = starrocks_env.load("`id` INT", [])
    try:
        source = starrocks_env.source(table)
        scan = starrocks.scan_expression(source)
        target = (f"`{starrocks_env.database()}`.`{victim}`")
        payload = f"us') OR 1=1; INSERT INTO {target} VALUES (77) -- "

        # What a naive renderer would emit if `literal` existed. On its own
        # connection, which is then thrown away: a stacked statement leaves
        # result sets the driver never reads, and every later statement on that
        # connection fails with a torn buffer.
        attacker = starrocks_env.connect()
        try:
            cur = attacker.cursor()
            cur.execute(
                f"SELECT `id` FROM {scan} WHERE CAST(`s` AS STRING) "
                f"IN ('{payload}')"
            )
            cur.fetchall()
            # Every result set must be read before the connection goes away.
            # Dropping it after only the first one leaves whether the second
            # statement ran up to a race, which is a flaky test rather than a
            # weaker engine: draining makes the INSERT land on every attempt.
            while cur.nextset():
                pass
            cur.close()
        finally:
            attacker.close()

        con = starrocks_env.connect()
        try:
            # The smuggled INSERT commits on its own schedule: measured, the
            # row is invisible at 0.00s and visible at 0.25s. Polling, because
            # a fixed sleep would either be flaky or slow.
            landed = 0
            for _ in range(40):
                landed = starrocks.run(
                    f"SELECT count(*) AS n FROM {target}", con=con
                ).column("n").to_pylist()[0]
                if landed:
                    break
                time.sleep(0.25)
            assert landed == 1, (
                "if this is 0 the engine stopped executing stacked statements; "
                "the dialect's refusal to escape is still correct, but this "
                "test's justification has changed and should be rewritten"
            )

            # The bound form: no rows, no write.
            bound = starrocks.run(
                f"SELECT `id` FROM {scan} WHERE CAST(`s` AS STRING) IN (?)",
                [payload], con,
            )
            assert bound.num_rows == 0
            still = starrocks.run(
                f"SELECT count(*) AS n FROM {target}", con=con
            ).column("n").to_pylist()[0]
            assert still == 1, "the bound value wrote nothing"
        finally:
            con.close()
    finally:
        starrocks_env.drop(table)
        starrocks_env.drop(victim)
