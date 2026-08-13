"""StarRocks-backed datasets — governed, read-only, remote.

A sibling of :mod:`laurelin.core.clickhouse` in shape and of
:mod:`laurelin.core.federation` in risk. ClickHouse here is *embedded*: chdb
runs in this process, holds no credentials and cannot be written to. StarRocks
is a **server** reached over the MySQL wire protocol, which changes two things
that matter more than the SQL does:

* There is an account, and accounts have privileges. Laurelin's account should
  hold ``SELECT`` and nothing else — measured, a SELECT-only StarRocks user is
  refused ``INSERT``/``DELETE``/``CREATE``/``DROP`` with error 5203, *including*
  when the write is smuggled in as a second statement.
* Stacked statements execute. ``SELECT 1; INSERT INTO t VALUES (99)`` runs the
  INSERT, and asking mysql-connector for ``-ClientFlag.MULTI_STATEMENTS``
  reports the flag off while the INSERT still lands. That is why
  :meth:`StarRocksDialect.literal` raises instead of escaping: on this engine a
  string-concatenation defect is a remote write, not merely a wrong read. Every
  policy value travels as a bound ``?``.

**What this slice is.** The read path: register a StarRocks table, scan it with
the row/column policy compiled to StarRocks SQL and pushed down, and hand back
Arrow.

**What this slice is not**, stated here rather than discovered:

* **No writes.** ``catalog.write``/``append``/``upload_file`` refuse a
  StarRocks dataset exactly as they refuse a ClickHouse one. Laurelin does not
  own this table.
* **No versions or time travel.** ``latest_version`` stays ``None``.
* **No ontology object types.** Refused, as for every ``scans_at_source`` kind.
* **No Stream Load, no primary-key upserts, no object store.** Those belong to
  the operational-store work, not here.
* **Scalar columns only.** ``ARRAY``/``MAP``/``STRUCT``/``JSON``/``BITMAP``/
  ``HLL``/``VARBINARY`` columns are refused at registration, by name, rather
  than being carried as some approximate Arrow type. A column whose type we
  guessed is a column whose text form we cannot claim, and the text form is
  what a row policy compares.

**Iceberg external catalogs are UNVERIFIED.** A three-part
``catalog.db.table`` scan expression is what an external catalog needs and the
form itself is measured working against ``default_catalog``, but no Iceberg
REST catalog was stood up, so the type-agreement tables in
:mod:`laurelin.core.dialects` were measured on **native** StarRocks columns
only. Whether an Iceberg-mapped DECIMAL or DATETIME renders identically is not
something this module has any right to claim yet.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import pyarrow as pa

from laurelin.core import federation
from laurelin.core.dialects import STARROCKS
from laurelin.core.failure import Failure, Phase, connect_failure

SOURCE_TYPES = ("table",)

# One to three dot-separated identifiers: `table`, `db.table`, or
# `catalog.db.table` for an external (Iceberg, Hive, JDBC) catalog. Deliberately
# narrow — the parts are rendered through `STARROCKS.quote`, which refuses a
# backtick, and nothing else may reach the statement.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")

_URL_RE = re.compile(
    r"^starrocks://"
    r"(?:(?P<user>[^:/@]+)(?::(?P<password>[^/]*))?@)?"
    r"(?P<host>[^:/@]+)"
    r"(?::(?P<port>\d+))?"
    r"(?:/(?P<database>[^/?]*))?$"
)

# Default MySQL-protocol query port of a StarRocks FE.
DEFAULT_PORT = 9030

# Re-exported unchanged, as the ClickHouse module does: one redactor, one place
# to fix. It masks any key matching password|secret|token|key and the password
# inside a URL.
redacted_source = federation.redacted_source


class StarRocksError(federation.FederationError):
    """A StarRocks source could not be reached or scanned.

    Subclasses ``FederationError`` so the API layer's existing 502 handling
    covers it without a second except clause to keep in sync.
    """


def available() -> bool:
    """Whether the MySQL-protocol client is installed.

    Mirrors ``iceberg.available()`` and ``clickhouse.available()`` so the
    feature and its tests read availability off one function.
    """
    try:
        import mysql.connector  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install is unavailable too
        return False
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def parse_url(url: str) -> dict[str, Any]:
    """``starrocks://user:password@host:port/database`` as connection kwargs."""
    match = _URL_RE.match(str(url).strip())
    if not match:
        raise ValueError(
            "starrocks source needs a url of the form "
            "starrocks://user:password@host:9030/database"
        )
    parts = match.groupdict()
    database = parts["database"] or ""
    if not database:
        raise ValueError("starrocks url needs a database: starrocks://…/<database>")
    return {
        "user": parts["user"] or "root",
        "password": parts["password"] or "",
        "host": parts["host"],
        "port": int(parts["port"] or DEFAULT_PORT),
        "database": database,
    }


def validate_source(source: dict[str, Any]) -> dict[str, Any]:
    """Reject a malformed StarRocks source at registration time."""
    type_ = source.get("type")
    if type_ not in SOURCE_TYPES:
        raise ValueError(
            f"Unknown StarRocks source type {type_!r}: expected one of "
            + ", ".join(SOURCE_TYPES)
            + " (a StarRocks dataset names a table; there is no file source, "
            "and writes are not implemented — see laurelin/core/starrocks.py)"
        )
    url = str(source.get("url", "")).strip()
    parse_url(url)  # raises with the shape it wanted
    table = str(source.get("table", "")).strip()
    if not _TABLE_RE.match(table):
        raise ValueError(
            f"Invalid StarRocks table {table!r}: expected table, db.table or "
            "catalog.db.table, made of letters, digits and underscores"
        )
    return {"type": type_, "url": url, "table": table}


# ---------------------------------------------------------------------------
# Scan rendering
# ---------------------------------------------------------------------------

def scan_expression(source: dict[str, Any]) -> str:
    """A table reference usable as ``FROM <expr>``.

    Every part goes through the dialect's quoter, which refuses a backtick —
    the one character that cannot be escaped inside a StarRocks identifier. The
    regex in ``validate_source`` already excludes it; this is the second lock,
    on the function that actually builds the string.

    Returns a plain string, not federation's ``(expr, params, extensions)``
    triple: there is nothing to bind in a table name and no DuckDB extension to
    load.
    """
    type_ = source["type"]
    if type_ != "table":
        raise ValueError(f"Unknown StarRocks source type {type_!r}")
    return ".".join(STARROCKS.quote(part) for part in str(source["table"]).split("."))


# ---------------------------------------------------------------------------
# Connection and execution
# ---------------------------------------------------------------------------

def _table_subject(source: dict[str, Any]) -> str:
    """A `Failure.subject` naming the table, from OUR validated config.

    `validate_source` has already forced the table through `_TABLE_RE`
    (letters, digits, underscores and dots), so this is a Laurelin identifier
    by the time it gets here — and `Failure` re-gates it anyway.
    """
    return str(source.get("table", "")) or "table"


def connect(source: dict[str, Any], connect_timeout: int = 10):
    """A connection to the StarRocks FE named by the source's url.

    ``use_pure=True`` on purpose: the C extension is an optional manylinux
    wheel, and a governed read path must not depend on which build of a client
    happened to install. Measured working for every case in this module.
    """
    try:
        import mysql.connector
    except Exception as exc:  # noqa: BLE001
        raise StarRocksError(
            "StarRocks support needs the MySQL-protocol client: "
            "pip install 'laurelin[starrocks]'"
        ) from exc

    params = parse_url(source["url"])
    try:
        return mysql.connector.connect(
            use_pure=True,
            charset="utf8mb4",
            connection_timeout=connect_timeout,
            autocommit=True,
            **params,
        )
    except Exception as exc:  # noqa: BLE001 - the driver raises several classes
        # R1. `_redact` lived here: `str(exc).replace(password, "*****")`. It
        # only ever worked when the driver quoted the password back *verbatim*,
        # and mysql-connector does not always — which is the same defeat round 3
        # found on the psycopg path. mysql-connector *does* give a usable errno
        # at connect (measured: 1045 wrong password, 2003 refused, 2005 bad
        # host), so classification here is mostly the driver's own code, with
        # Laurelin's TCP pre-flight as the fallback.
        raise StarRocksError(failure=connect_failure(
            exc, subject=f"starrocks:{params['host']}", driver="mysql.connector",
            dsn=str(source.get("url", "")), config=source,
        )) from exc


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
#
# The schema comes from `DESC`, and both of the alternatives were measured and
# rejected for the same reason: they lose the distinction between BOOLEAN and
# TINYINT. Integers are row-key portable on this dialect and booleans are
# emphatically not — CAST(b AS STRING) is '1' where Arrow says 'true' — so a
# boolean that arrives looking like an integer makes the dialect claim an
# agreement it does not have, and a tenant policy then admits the wrong rows.
#
#   * The MySQL result-set metadata reports a BOOLEAN as field type 1 (TINY),
#     the *same* code as TINYINT, and carries no scale for a DECIMAL.
#   * `information_schema.columns` is MySQL-compat-shaped and worse: it reports
#     BOOLEAN as 'tinyint(1)' and — measured — a 128-bit LARGEINT as
#     'bigint(20) unsigned'.
#
# `DESC` reports 'boolean', 'largeint' and 'decimal(12,2)'. It is also the only
# one of the three that answers for an external (Iceberg/Hive) catalog.

_SIMPLE_TYPES = {
    "boolean": pa.bool_(),
    "tinyint": pa.int8(),
    "smallint": pa.int16(),
    "int": pa.int32(),
    "integer": pa.int32(),
    "bigint": pa.int64(),
    "float": pa.float32(),
    "double": pa.float64(),
    "date": pa.date32(),
    "datetime": pa.timestamp("us"),
    "timestamp": pa.timestamp("us"),
    # StarRocks LARGEINT is a 128-bit integer; the driver hands it back as
    # text and Arrow has no 128-bit integer type. Carrying it as a string keeps
    # every digit, and it keeps the two text forms in agreement — the SQL side
    # renders the same digits `str()` does — where decimal128 would silently
    # bound the range.
    "largeint": pa.string(),
    "char": pa.string(),
    "varchar": pa.string(),
    "string": pa.string(),
    "text": pa.string(),
}

_DECIMAL_RE = re.compile(r"^decimal(?:v[23]|32|64|128)?\((\d+),\s*(\d+)\)$")


def arrow_type_of(sr_type: str) -> pa.DataType:
    """The Arrow type for a StarRocks type as ``DESC`` spells it.

    Raises for anything not scalar. That is the fail-closed half of the
    contract: an unmapped type would have to be guessed, and a guessed type is
    a guessed *text form*, which is exactly what a row policy compares.
    """
    text = str(sr_type).strip().lower()
    base = text.split("(", 1)[0].strip()
    if base in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[base]
    decimal = _DECIMAL_RE.match(text)
    if decimal:
        return pa.decimal128(int(decimal.group(1)), int(decimal.group(2)))
    raise StarRocksError(
        f"StarRocks column type {sr_type!r} is not supported. Laurelin reads "
        "scalar columns only: a type it had to guess at would also be a text "
        "form it had to guess at, and a row policy is a comparison of text. "
        "Project the column away in a StarRocks view, or land it in a managed "
        "dataset with a transform."
    )


def schema_of(source: dict[str, Any], con=None) -> pa.Schema:
    """Arrow schema of the table, read without fetching rows.

    ``DESC`` rather than ``SELECT * LIMIT 0`` — see the note above on why the
    result-set metadata is not trustworthy enough for this. Raises rather than
    returning an empty schema: an unknown column set cannot be masked, so
    discovery failing has to be a refusal (see ``SqlPolicy.render``).
    """
    owned = con is None
    con = con or connect(source)
    sql = f"DESC {scan_expression(source)}"
    try:
        rows = _command(con, sql)
    except StarRocksError:
        raise
    except Exception as exc:  # noqa: BLE001 - the driver raises several classes
        # Was `f"Could not read the StarRocks table: {exc}"` — no redaction at
        # all on this line, and it reached a 502 body.
        raise StarRocksError(failure=Failure.from_exception(
            exc, phase=Phase.describe, driver="mysql.connector",
            subject=f"starrocks:{_table_subject(source)}",
            dsn=str(source.get("url", "")), config=source,
        )) from exc
    finally:
        if owned:
            con.close()
    fields = []
    for row in rows:
        name = str(row[0])
        if not name:
            # Not addressable in any dialect, so it cannot be masked and must
            # not be projected.
            continue
        if "`" in name:
            raise StarRocksError(
                f"Column {name!r} contains a backtick, which has no escape "
                "inside a StarRocks identifier. Refusing to read the table: "
                "the quoted name would resolve to a different column."
            )
        fields.append(pa.field(name, arrow_type_of(row[1])))
    if not fields:
        raise StarRocksError(
            f"StarRocks table {source.get('table')!r} reported no columns. "
            "Refusing to register or read it: an unknown column set cannot be "
            "masked."
        )
    return pa.schema(fields)


def columns_of(source: dict[str, Any], con=None) -> list[str]:
    """Column names of the table, read without fetching rows."""
    return list(schema_of(source, con).names)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _fetch(con, sql: str, params: Optional[list] = None):
    """``(description, rows)`` for one governed statement.

    A *prepared* cursor always, even with no parameters, because that is the
    channel whose fidelity was fuzzed: ``length(?)`` equalled
    ``len(value.encode())`` for all 334 hostile values tried, NUL bytes,
    newlines and lone backslashes included. mysql-connector's ordinary cursor
    interpolates ``%s`` client-side, which would put the codebase's only
    injection surface inside a third-party library where no test of ours looks.
    """
    cur = con.cursor(prepared=True)
    try:
        cur.execute(sql, tuple(params or ()))
        rows = cur.fetchall()
        return list(cur.description or []), rows
    finally:
        cur.close()


def _command(con, sql: str) -> list:
    """Rows of a statement the prepared protocol refuses to carry.

    ``DESC`` is one: measured, it fails with error 1295, "This command is not
    supported in the prepared statement protocol yet" (so does ``SHOW``). It
    therefore runs on an ordinary cursor — and takes **no parameters at all**,
    by signature rather than by convention, because mysql-connector's ordinary
    cursor interpolates them client-side. The only value in the statement is a
    table identifier that ``validate_source`` has already constrained to
    ``[A-Za-z_][A-Za-z0-9_]*`` parts and ``scan_expression`` has quoted.
    """
    cur = con.cursor()
    try:
        cur.execute(sql)
        return list(cur.fetchall())
    finally:
        cur.close()


def run(
    sql: str,
    params: Optional[list] = None,
    con=None,
    source: Optional[dict[str, Any]] = None,
    schema: Optional[pa.Schema] = None,
) -> pa.Table:
    """Execute one statement and return an Arrow table.

    ``schema`` is the table's *declared* schema (from :func:`schema_of`) and is
    how a result column gets its type: see :func:`_result_schema`.
    """
    owned = con is None
    if con is None:
        if source is None:
            raise ValueError("run() needs either a connection or a source")
        con = connect(source)
    try:
        description, rows = _fetch(con, sql, params)
    except StarRocksError:
        raise
    except Exception as exc:  # noqa: BLE001 - the driver raises several classes
        # Also previously unredacted.
        raise StarRocksError(failure=Failure.from_exception(
            exc, phase=Phase.execute, driver="mysql.connector",
            subject=f"starrocks:{_table_subject(source or {})}",
            dsn=str((source or {}).get("url", "")), config=source,
        )) from exc
    finally:
        if owned:
            con.close()
    return _to_arrow(description, rows, schema)


# Field type codes from the MySQL protocol, as mysql-connector reports them.
# Only the string ones are needed: they are how a *masked* column announces
# that it is no longer the type the table declares.
_PROTO_STRING = {15, 249, 250, 251, 252, 253, 254}  # VARCHAR..STRING incl. BLOBs
_PROTO_ARROW = {
    1: pa.int8(), 2: pa.int16(), 3: pa.int32(), 8: pa.int64(), 9: pa.int32(),
    4: pa.float32(), 5: pa.float64(), 10: pa.date32(),
    7: pa.timestamp("us"), 12: pa.timestamp("us"),
}


def _result_schema(description, declared: Optional[pa.Schema]) -> pa.Schema:
    """The Arrow schema of a *result set*, which is not the table's.

    A policy rewrites the projection, and two of the three masks change the
    column's type: ``redact`` and ``hash`` both yield text, while ``null``
    keeps it (measured: ``if(FALSE, d, NULL)`` still reports DOUBLE, a masked
    DECIMAL still reports DECIMAL, a masked DATETIME still reports DATETIME).

    So the rule is: trust the declared type unless the engine says the column
    came back as text and the declaration says it should not have. That case
    is a mask, and text is the truth. Anything not in the declared schema falls
    back to the protocol's own type — which is why the fallback exists at all,
    and why it is not used for the columns that matter.
    """
    names = set() if declared is None else set(declared.names)
    fields = []
    for column in description:
        name, proto = column[0], column[1]
        field = declared.field(name) if name in names else None
        if field is not None:
            is_text = pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
            if proto in _PROTO_STRING and not is_text:
                fields.append(pa.field(name, pa.string()))
            else:
                fields.append(field.with_nullable(True))
            continue
        fields.append(pa.field(name, _PROTO_ARROW.get(proto, pa.string())))
    return pa.schema(fields)


def _to_arrow(description, rows, declared: Optional[pa.Schema]) -> pa.Table:
    """Driver rows as an Arrow table with the result's own schema."""
    if not description:
        return pa.table({})
    schema = _result_schema(description, declared)
    columns = []
    for index, field in enumerate(schema):
        values = [row[index] for row in rows]
        if pa.types.is_boolean(field.type):
            # StarRocks BOOLEAN arrives as 0/1 over the protocol.
            values = [None if v is None else bool(v) for v in values]
        elif pa.types.is_string(field.type):
            # LARGEINT arrives as text already; a masked numeric column can
            # arrive as bytes on some charsets.
            values = [
                None if v is None
                else v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
                for v in values
            ]
        columns.append(pa.array(values, type=field.type))
    return pa.Table.from_arrays(columns, schema=schema)
