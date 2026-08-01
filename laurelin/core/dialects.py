"""How each SQL engine spells a compiled policy.

Laurelin makes **one** policy decision (``PermissionService.decide``) and
renders it N ways. Arrow is one renderer; SQL is another — but "SQL" is not a
language, it is a family, and the differences between its members are not
cosmetic. A federator that gets a dialect wrong returns a wrong number. A
*governance* layer that gets one wrong leaks data.

So a dialect here owns **every** string it emits: its own identifier quoter,
its own value escaper, its own mask expressions, and its own statement
assembly. Nothing is shared between dialects, because the engines' rules are
different rather than merely stricter. Four measured examples, each of which
is a silent leak if you assume otherwise:

* Reusing DuckDB's doubled-quote identifier rule on ClickHouse resolves a
  column named ``a\\`b`` to the *different* column ``a`b`` and returns its
  data, with no error (see :meth:`ClickHouseDialect.quote`).
* Reusing it on StarRocks is worse: ``"s"`` is a *string literal* there, so
  ``SELECT "s"`` returns the constant ``'s'`` for every row and a row filter
  written that way admitted every row, NULLs included (see
  :meth:`StarRocksDialect.quote`).
* ``NULLIF(c, c)`` masks NaN on DuckDB and does **not** on ClickHouse, so the
  masked column ships a real value through (see ``null_mask``).
* ClickHouse resolves ``WHERE`` against ``SELECT`` aliases and DuckDB does
  not, so a flat statement evaluates the row policy against the *mask* — a
  complete row-policy bypass that fails **open** (see ``assemble``).

A dialect also owns something that is not a string: **which column types it can
render to text the same way Arrow does**. Row policies and hash masks are both
defined on the *text* of a value, and ``toString``/``CAST AS VARCHAR`` are not
``pyarrow.compute.cast(col, string)``. Where they disagree, the policy means a
different thing on that engine, so the dialect declares the types it can carry
faithfully and the renderer refuses the rest (see ``row_key_matches_arrow`` and
``hash_text_matches_arrow``). Both tables were measured, and
``tests/test_text_agreement.py`` re-derives them from the live engines so a
version bump that changes a rendering fails CI instead of changing a policy.

This module builds strings and inspects Arrow types; it does not import the
permission service, so the ClickHouse backend can use ``quote``/``literal`` for
its scan expression without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

# Decimal scales whose Arrow text form is plain fixed point.
#
# Not a style choice and not caution: Python's Decimal — which is what
# pyarrow's cast-to-string produces — switches to scientific notation once the
# adjusted exponent drops below -6. So decimal(38,7) renders zero as '0E-7'
# where both DuckDB and StarRocks render '0.0000000'. Measured on both engines,
# scales 0..6 agree value-for-value and 7 upward do not. Negative scales are
# out for the same reason at the other end ('1E+2').
#
# Both dialects claimed the whole decimal family. The shipped corpus sampled
# scales 0, 2 and 6 — one step below the boundary — which is why nothing caught
# it. The guard exists precisely so a policy cannot mean two things on two
# engines, so a guard that answers wrongly is worse than a narrow one.
_MAX_PORTABLE_DECIMAL_SCALE = 6


def _decimal_text_is_portable(t) -> bool:
    return bool(pa.types.is_decimal(t) and 0 <= t.scale <= _MAX_PORTABLE_DECIMAL_SCALE)


def _unwrap(arrow_type):
    """Dictionary-encoding is a storage detail; both engines hand back the
    value type, and so does ``pc.cast``."""
    while pa.types.is_dictionary(arrow_type):
        arrow_type = arrow_type.value_type
    return arrow_type


@dataclass(frozen=True)
class SqlDialect:
    """Base contract. Every method is overridden; none has a safe default."""

    name: str = "sql"
    # True  -> policy values travel as bound parameters, `placeholder()` used.
    # False -> policy values are escaped into the statement by `literal()`,
    #          which is then the ONLY function in the codebase turning a
    #          policy value into SQL text.
    binds_values: bool = True

    def quote(self, ident: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def literal(self, value: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def placeholder(self, index: int) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_text(self, col_sql: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def null_mask(self, expr: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def redact_mask(self, expr: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def hash_mask(self, expr: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- type portability -----------------------------------------------------
    #
    # Default deny on both. A dialect that forgets to declare a type refuses
    # the read; a dialect that forgets to declare a type and defaulted to True
    # would silently enforce a different policy.

    def row_key_matches_arrow(self, arrow_type) -> bool:
        """Is ``to_text(col)`` byte-identical to ``pc.cast(col, string)``?

        That cast is the key the Arrow renderer compares a row policy's
        allowlist against (``PermissionService._filter_rows``), so anywhere the
        answer is no, the same allowlist selects a different set of rows here.
        """
        return False

    def hash_text_matches_arrow(self, arrow_type) -> bool:
        """Is the text this dialect feeds to SHA256 equal to ``str(value)``?

        That is what the Arrow renderer digests (``_mask_column``), and the
        whole point of a hash mask is that the same value yields the same token
        everywhere. Where the answer is no the token is still masked but no
        longer joinable, which is a mask that quietly does not do its job.
        """
        return False

    def assemble(
        self,
        select_list: str,
        scan_expr: str,
        where: str,
        limit=None,
        settings: str = "",
    ) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class DuckDbDialect(SqlDialect):
    """DuckDB, byte-for-byte what Laurelin emitted before dialects existed.

    Every string below is a verbatim copy of the pre-seam renderer. The golden
    test in ``tests/test_dialects.py`` compares against checked-in literals so
    that adding a second engine cannot quietly change the first one.
    """

    name: str = "duckdb"
    binds_values: bool = True

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def literal(self, value: str) -> str:
        raise NotImplementedError(
            "DuckDB binds policy values as parameters; there is no reason to "
            "turn one into SQL text, and doing so would be the only injection "
            "surface in the renderer."
        )

    def placeholder(self, index: int) -> str:
        return "?"

    def to_text(self, col_sql: str) -> str:
        return f"CAST({col_sql} AS VARCHAR)"

    def null_mask(self, expr: str) -> str:
        # NULLIF(x, x) is NULL with the expression's own type, so the
        # masked column keeps its type without needing the schema.
        return f"NULLIF({expr}, {expr})"

    def redact_mask(self, expr: str) -> str:
        return "'***'"

    def hash_mask(self, expr: str) -> str:
        # DuckDB's sha256 matches the table path's digest.
        return (
            f"CASE WHEN {expr} IS NULL THEN NULL ELSE "
            f"substr(sha256(CAST({expr} AS VARCHAR)), 1, 16) END"
        )

    def row_key_matches_arrow(self, arrow_type) -> bool:
        # Measured over a hostile value set per type (scratch survey reproduced
        # as tests/test_text_agreement.py). Notable exclusions, each a real
        # divergence rather than caution: DOUBLE renders 0.0 as '0.0' where the
        # cast gives '0'; TIMESTAMP drops the sub-second zeros the cast keeps
        # ('...05' vs '...05.000000'); BLOB escapes non-printables.
        t = _unwrap(arrow_type)
        return bool(
            pa.types.is_string(t)
            or pa.types.is_large_string(t)
            or pa.types.is_boolean(t)
            or pa.types.is_integer(t)
            or pa.types.is_date(t)
            or _decimal_text_is_portable(t)
        )

    def hash_text_matches_arrow(self, arrow_type) -> bool:
        # DOUBLE is in because DuckDB's text form is Python's shortest
        # round-trip -- measured across inf/nan/denormals. DECIMAL is in only
        # up to scale 6; see _MAX_PORTABLE_DECIMAL_SCALE. BOOLEAN is
        # out: DuckDB says 'true' and `str(True)` is 'True', which has been a
        # silent digest divergence since before dialects existed. FLOAT is out
        # too: float32 1e-07 widens to '1.0000000116860974e-07' in Python.
        t = _unwrap(arrow_type)
        return bool(
            pa.types.is_string(t)
            or pa.types.is_large_string(t)
            or pa.types.is_integer(t)
            or pa.types.is_date(t)
            or pa.types.is_float64(t)
            or _decimal_text_is_portable(t)
        )

    def assemble(
        self,
        select_list: str,
        scan_expr: str,
        where: str,
        limit=None,
        settings: str = "",
    ) -> str:
        # DuckDB resolves WHERE against the *input* columns, never the SELECT
        # aliases, so the flat form cannot see a mask. `settings` has no
        # DuckDB equivalent (budgets go on the connection) and is ignored.
        sql = f"SELECT {select_list} FROM {scan_expr} WHERE {where}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return sql


@dataclass(frozen=True)
class ClickHouseDialect(SqlDialect):
    """ClickHouse (embedded, via chdb).

    ``binds_values`` is **False**, which is the uncomfortable part and is
    stated here rather than discovered later: ClickHouse has no positional
    placeholder (``SELECT ?`` is a syntax error, code 62) and its named
    ``{p:String}`` channel is *not* byte-preserving — ``length({p:String})``
    for ``a\\nb`` measured 3 where the value is 4 bytes, ``\\\\`` measured 1
    where it is 2, and a trailing backslash is a hard error. A value that
    arrives shortened matches rows the policy never allowed.

    So the invariant "policy values never appear verbatim in the rendered SQL"
    — true for DuckDB — **cannot hold here**. The replacement invariant, which
    the tests enforce, is: :meth:`literal` is the only function in the codebase
    that may turn a policy value into SQL text, and it is fuzz-tested on
    round-trip *fidelity* (``length(<literal>) == len(value.encode())``), not
    on "the query ran". Every dangerous value runs fine; that is exactly why
    fidelity is the acceptance test.
    """

    name: str = "clickhouse"
    binds_values: bool = False

    def quote(self, ident: str) -> str:
        # ClickHouse treats backslash as an escape INSIDE quoted identifiers
        # and DuckDB does not. Measured on a two-column Parquet file: a column
        # named  a\`b  rendered with DuckDB's doubled-quote rule resolves to
        # the DIFFERENT column  a`b  and returns its data -- a mask aimed at
        # one column lands on another, and nothing errors.
        # Escape the backslash FIRST; the other order re-escapes what you add.
        return "`" + ident.replace("\\", "\\\\").replace("`", "\\`") + "`"

    def literal(self, value: str) -> str:
        # The ONLY place a policy value becomes SQL text. ClickHouse honours
        # backslash escapes inside string literals, so doubling the quote
        # alone is not enough: 'a\nb' would arrive 3 bytes long instead of 4
        # and match rows the policy does not allow. Backslash first, then the
        # quote -- reversing the order would escape the backslashes this step
        # just introduced.
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def placeholder(self, index: int) -> str:
        raise NotImplementedError(
            "ClickHouse has no positional placeholder (SELECT ? is error 62) "
            "and its named parameters are not byte-preserving. Use literal()."
        )

    def to_text(self, col_sql: str) -> str:
        # NOT CAST(x AS String): that raises code 349 ("Cannot convert NULL
        # value to non-Nullable type") on any NULL row, and Parquet columns
        # infer Nullable by default -- so the whole governed read would fail
        # on data that is merely incomplete. toString() keeps NULL as NULL,
        # which the IN predicate then excludes (fail closed).
        return f"toString({col_sql})"

    def null_mask(self, expr: str) -> str:
        # NULLIF(c, c) is WRONG here. NaN <> NaN in ClickHouse, so NULLIF
        # returns the NaN unchanged: measured [None, nan, None, None] over a
        # masked Float64 column -- one row ships its real value through a
        # mask. if(0, c, NULL) masks every value and keeps the column's type
        # (toTypeName -> Nullable(Float64)). DuckDB's NULLIF *does* mask NaN,
        # so this is a genuine dialect divergence, not a shared bug.
        return f"if(0, {expr}, NULL)"

    def redact_mask(self, expr: str) -> str:
        return "'***'"

    def hash_mask(self, expr: str) -> str:
        # Three ClickHouse facts, each measured: lowercase `sha256` does not
        # exist (code 46); `SHA256` returns a raw FixedString(32), so hex() is
        # mandatory; hex() uppercases, so lower() is mandatory. The result for
        # 'us' is 79adb2a2fce5c6ba -- identical to the Arrow path's
        # hashlib.sha256(b"us").hexdigest()[:16].
        return (
            f"CASE WHEN {expr} IS NULL THEN NULL ELSE "
            f"substring(lower(hex(SHA256(toString({expr})))), 1, 16) END"
        )

    def row_key_matches_arrow(self, arrow_type) -> bool:
        # Strictly narrower than DuckDB's, and every exclusion was demonstrated
        # to change which rows a policy admits:
        #   Decimal(12,2)  0.00 -> '0'          where the cast gives '0.00'
        #   Float64        1e10 -> '10000000000' where the cast gives '1e+10'
        #   timestamp/time  -> DateTime64(n,'UTC'); the scale and the epoch
        #                      date both appear, and a tz-aware column loses
        #                      its offset entirely.
        # Timestamps look tempting -- timestamp[us] happens to agree -- but the
        # agreement is an accident of the parquet unit, and timestamp[s] and
        # timestamp[ms] both disagree on the same column definition.
        t = _unwrap(arrow_type)
        return bool(
            pa.types.is_string(t)
            or pa.types.is_large_string(t)
            or pa.types.is_boolean(t)
            or pa.types.is_integer(t)
            or pa.types.is_date(t)
        )

    def hash_text_matches_arrow(self, arrow_type) -> bool:
        # Same set minus Bool, for the same reason DuckDB excludes it:
        # toString(true) is 'true' and `str(True)` is 'True'.
        t = _unwrap(arrow_type)
        return bool(
            pa.types.is_string(t)
            or pa.types.is_large_string(t)
            or pa.types.is_integer(t)
            or pa.types.is_date(t)
        )

    def assemble(
        self,
        select_list: str,
        scan_expr: str,
        where: str,
        limit=None,
        settings: str = "",
    ) -> str:
        # The filter MUST sit strictly below the projection. ClickHouse
        # resolves WHERE against SELECT aliases and DuckDB does not, so a flat
        # statement evaluates the row policy against the MASK. Measured on a
        # 4-row file:
        #     SELECT '***' AS region FROM f WHERE toString(region) IN ('***')
        # returns all 4 rows flat and 0 nested; with IN ('us') the flat form
        # returns 0 where 2 are correct. Both directions are wrong and one of
        # them fails OPEN. Wrapping only the scan does not help -- the alias
        # is still in the same SELECT scope.
        sql = f"SELECT {select_list} FROM (SELECT * FROM {scan_expr} WHERE {where})"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if settings:
            sql += " SETTINGS " + settings
        return sql


@dataclass(frozen=True)
class StarRocksDialect(SqlDialect):
    """StarRocks (a real server, reached over the MySQL wire protocol).

    ``binds_values`` is **True**, and unlike DuckDB — where binding is merely
    the natural thing — here it is load-bearing, so :meth:`literal` raises and
    no escaper exists to fall back to. The reason was measured against a live
    StarRocks 3.x rather than reasoned about:

    * ``SELECT 1; INSERT INTO t VALUES (99)`` on one ``execute()`` **runs the
      INSERT**. Asking the client for ``-ClientFlag.MULTI_STATEMENTS`` reports
      the flag off and the INSERT still lands.
    * Feeding the policy value ``us') OR 1=1; INSERT INTO t VALUES (77) --``
      through naive concatenation returned every row *and* wrote a row. The
      same value bound through ``?`` returned nothing and wrote nothing.

    ClickHouse's escaper exists because that engine offers no byte-preserving
    binding channel; the cost of a defect there is a read. This engine does
    bind — ``length(?)`` equalled ``len(value.encode())`` for all 334 hostile
    values fuzzed, including NUL, newlines, lone backslashes and 300 control
    characters — and the cost of a defect here would be a remote *write*. So
    the door is not merely unused, it is absent.
    """

    name: str = "starrocks"
    binds_values: bool = True

    def quote(self, ident: str) -> str:
        # A backtick is unspellable in a StarRocks identifier, so this refuses
        # rather than mangles. Measured: MySQL's doubling rule does not apply
        # here -- CREATE TABLE with a column `a``b` produces a column literally
        # named `ab`, proved by a later real `ab` column failing with
        # "Duplicate column name". Every other escape is worse:
        #   DuckDB's  "x"  is a *string literal* on StarRocks, so SELECT "s"
        #             returns the constant 's' for every row and a row policy
        #             whose allowlist happens to contain the column's own name
        #             admitted 3 of 3 rows, the NULL row included. Fails OPEN.
        #   ClickHouse's backslash escape does not resolve (error 1064). Fails
        #             closed, but still wrong.
        if "`" in ident:
            raise ValueError(
                f"Cannot address the column {ident!r} on StarRocks: a backtick "
                "has no escape inside a quoted identifier (doubling it drops "
                "the character rather than escaping it), so the name would "
                "resolve to a different column. Refusing."
            )
        return "`" + ident + "`"

    def literal(self, value: str) -> str:
        raise NotImplementedError(
            "StarRocks binds policy values as parameters. Turning one into SQL "
            "text would be the only injection surface in the renderer, and on "
            "this engine it is a write primitive: stacked statements execute "
            "and the client's MULTI_STATEMENTS flag does not stop them."
        )

    def placeholder(self, index: int) -> str:
        return "?"

    def to_text(self, col_sql: str) -> str:
        # Unlike ClickHouse -- where CAST(x AS String) raises on a NULL row --
        # StarRocks returns NULL and the query succeeds, and `IN ('us')` then
        # excludes that row (fail closed). Measured on a table holding NULL,
        # '' and 'us': IN ('us') matched only 'us', and IN ('') matched only
        # the empty string, never the NULL.
        return f"CAST({col_sql} AS STRING)"

    def null_mask(self, expr: str) -> str:
        # `NULLIF(c, c)` also measured NULL for every row here, including the
        # NaN case ClickHouse gets wrong -- but only because StarRocks has no
        # NaN to test with: CAST('nan' AS DOUBLE) is itself NULL. The hazard is
        # therefore unreproducible rather than proven absent, so this uses the
        # form that masks unconditionally and needs no such argument.
        # Type-preserving: the result column's protocol type is DOUBLE for a
        # DOUBLE input, DECIMAL for a DECIMAL, DATETIME for a DATETIME.
        return f"if(FALSE, {expr}, NULL)"

    def redact_mask(self, expr: str) -> str:
        return "'***'"

    def hash_mask(self, expr: str) -> str:
        # `sha2(x, 256)` already returns lowercase hex: measured
        # sha2('us',256) = '79adb2a2...' , 64 characters, equal value-for-value
        # to hashlib.sha256(b'us').hexdigest(). Porting ClickHouse's mandatory
        # lower(hex(...)) wrapper would hex the hex -- 128 characters, measured
        # -- and every hash mask would silently stop joining. Lowercase
        # `sha256()` does not exist here at all (error 1064).
        return (
            f"CASE WHEN {expr} IS NULL THEN NULL ELSE "
            f"substr(sha2(CAST({expr} AS STRING), 256), 1, 16) END"
        )

    def row_key_matches_arrow(self, arrow_type) -> bool:
        # Measured by writing each type into a real StarRocks table and
        # comparing CAST(c AS STRING) against pyarrow's cast, per value.
        # Narrower than DuckDB's, and **boolean is the StarRocks-specific
        # trap**: it is row-key portable on DuckDB *and* ClickHouse, and here
        # CAST(b AS STRING) is '1'/'0' where Arrow says 'true'/'false'. A
        # tenant policy on a boolean column would admit the wrong half of the
        # table. Also out:
        #   float32/float64  1e-7 -> '1e-07' where Arrow gives '1e-7', and
        #                    DOUBLE 1e10 -> '10000000000' vs Arrow's '1e+10'.
        #   timestamp        DATETIME '...03:04:05' drops the sub-second zeros
        #                    Arrow keeps ('...05.000000').
        # Decimal is IN up to scale 6 (matching DuckDB, unlike ClickHouse):
        # trailing zeros stay intact, and the engine never uses an exponent
        # where Arrow does. Above scale 6 Arrow does; see
        # _MAX_PORTABLE_DECIMAL_SCALE, re-measured on a live server.
        t = _unwrap(arrow_type)
        return bool(
            pa.types.is_string(t)
            or pa.types.is_large_string(t)
            or pa.types.is_integer(t)
            or pa.types.is_date(t)
            or _decimal_text_is_portable(t)
        )

    def hash_text_matches_arrow(self, arrow_type) -> bool:
        # Same set, plus naive timestamps: DATETIME renders exactly as
        # `str(datetime)` does at second, millisecond and microsecond
        # precision, and the whole corpus was re-measured under two session
        # time zones (UTC and America/New_York) with identical results --
        # StarRocks DATETIME is timezone-naive, so the session variable does
        # not reach the rendering. A *tz-aware* Arrow timestamp is excluded
        # because StarRocks has no column type that could produce one, so
        # there is nothing to have measured.
        # Floats stay out for the same reason as above; boolean stays out
        # because '1' is not `str(True)`, which is also why DuckDB excludes it.
        t = _unwrap(arrow_type)
        return bool(
            pa.types.is_string(t)
            or pa.types.is_large_string(t)
            or pa.types.is_integer(t)
            or pa.types.is_date(t)
            or _decimal_text_is_portable(t)
            or (pa.types.is_timestamp(t) and t.tz is None)
        )

    def assemble(
        self,
        select_list: str,
        scan_expr: str,
        where: str,
        limit=None,
        settings: str = "",
    ) -> str:
        # Nested, and the derived table **must** carry an alias: without one
        # StarRocks refuses with error 1248, "Every derived table must have its
        # own alias".
        #
        # The flat form is correct *today* -- measured, WHERE resolves against
        # the base columns, so `SELECT '***' AS s ... WHERE CAST(s AS STRING)
        # IN ('***')` returns 0 rows and IN ('us') returns the right ones. But
        # ORDER BY does see SELECT aliases (measured: ordering by a column
        # aliased to a constant leaves the input order untouched, where
        # ordering by the base column reverses it), so the flat form's
        # correctness depends on which clauses the renderer happens to emit.
        # Nesting costs nothing and removes the dependency.
        #
        # `settings` is re-rendered as a hint rather than appended: there is no
        # trailing SETTINGS clause here. StarRocks validates the variable names
        # inside SET_VAR (error 1193, with a did-you-mean), so a typo in a
        # resource pin fails loudly instead of being ignored.
        hint = f"/*+ SET_VAR({settings}) */ " if settings else ""
        sql = (
            f"SELECT {hint}{select_list} "
            f"FROM (SELECT * FROM {scan_expr} WHERE {where}) t"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return sql


DUCKDB = DuckDbDialect()
CLICKHOUSE = ClickHouseDialect()
STARROCKS = StarRocksDialect()

DIALECTS = {d.name: d for d in (DUCKDB, CLICKHOUSE, STARROCKS)}
