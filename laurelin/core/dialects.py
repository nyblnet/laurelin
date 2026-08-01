"""How each SQL engine spells a compiled policy.

Laurelin makes **one** policy decision (``PermissionService.decide``) and
renders it N ways. Arrow is one renderer; SQL is another — but "SQL" is not a
language, it is a family, and the differences between its members are not
cosmetic. A federator that gets a dialect wrong returns a wrong number. A
*governance* layer that gets one wrong leaks data.

So a dialect here owns **every** string it emits: its own identifier quoter,
its own value escaper, its own mask expressions, and its own statement
assembly. Nothing is shared between dialects, because the engines' rules are
different rather than merely stricter. Three measured examples, each of which
is a silent leak if you assume otherwise:

* Reusing DuckDB's doubled-quote identifier rule on ClickHouse resolves a
  column named ``a\\`b`` to the *different* column ``a`b`` and returns its
  data, with no error (see :meth:`ClickHouseDialect.quote`).
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
            or pa.types.is_decimal(t)
        )

    def hash_text_matches_arrow(self, arrow_type) -> bool:
        # DOUBLE and DECIMAL are in because DuckDB's text form is Python's
        # (shortest round-trip for doubles, scale-preserving for decimals) --
        # measured across inf/nan/denormals and scales 0, 2 and 6. BOOLEAN is
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
            or pa.types.is_decimal(t)
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


DUCKDB = DuckDbDialect()
CLICKHOUSE = ClickHouseDialect()

DIALECTS = {DUCKDB.name: DUCKDB, CLICKHOUSE.name: CLICKHOUSE}
