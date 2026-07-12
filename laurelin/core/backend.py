"""Database backend abstraction so the metadata / control stores run on either
SQLite (embedded, default) or PostgreSQL (server / multi-tenant deployments).

The two dialects are close enough that the store code shares almost all its SQL.
This module papers over the differences the stores actually use:

- placeholders: SQLite ``?`` vs psycopg ``%s`` (we translate ``?`` -> ``%s``);
- rows: both are exposed as plain dicts (``row["col"]`` and ``"col" in row``);
- multi-statement DDL: psycopg runs one statement per ``execute``;
- ``INSERT OR IGNORE`` -> ``INSERT ... ON CONFLICT DO NOTHING``;
- schema tokens (autoincrement PK, case-insensitive text) substituted per dialect.

All SQL in this codebase is authored here, so the naive ``?`` -> ``%s`` swap is
safe (no ``?`` inside string literals, no bare ``%``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional


def is_postgres_url(s: str) -> bool:
    return isinstance(s, str) and s.startswith(("postgres://", "postgresql://"))


# --------------------------------------------------------------------------- rows

class _Cursor:
    """Uniform cursor: fetchone -> dict|None, fetchall -> list[dict]."""

    def __init__(self, cur, dialect: str):
        self._cur = cur
        self._dialect = dialect

    def _to_dict(self, row) -> Optional[dict]:
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        return {d[0]: row[i] for i, d in enumerate(self._cur.description)}

    def fetchone(self) -> Optional[dict]:
        return self._to_dict(self._cur.fetchone())

    def fetchall(self) -> list[dict]:
        return [self._to_dict(r) for r in self._cur.fetchall()]

    def fetchmany(self, n: int) -> list:
        return list(self._cur.fetchmany(n))

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def __iter__(self):
        for r in self._cur.fetchall():
            yield self._to_dict(r)


class Connection:
    """A thin, dialect-aware connection wrapper used by the stores as ``c``."""

    def __init__(self, raw, dialect: str):
        self._raw = raw
        self.dialect = dialect

    def _translate(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _Cursor:
        cur = self._raw.cursor()
        cur.execute(self._translate(sql), tuple(params))
        return _Cursor(cur, self.dialect)

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
        cur = self._raw.cursor()
        cur.executemany(self._translate(sql), [tuple(p) for p in seq_of_params])

    def executescript(self, script: str) -> None:
        if self.dialect == "sqlite":
            self._raw.executescript(script)
        else:
            cur = self._raw.cursor()
            for stmt in _split_statements(script):
                cur.execute(stmt)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def _split_statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


# --------------------------------------------------------------------------- backends

class Backend:
    dialect: str
    # Column giving stable insertion order. SQLite has the implicit ``rowid``;
    # Postgres has no rowid, so tables that need insertion order carry an
    # explicit ``seq`` identity column (see the ``{{SEQ_COL}}`` schema token).
    order_col: str = "rowid"

    def connect(self) -> Connection:  # pragma: no cover - interface
        raise NotImplementedError

    def render_schema(self, schema: str) -> str:
        """Substitute schema tokens for this dialect."""
        raise NotImplementedError

    def insert_or_ignore(self, table: str, columns: str, placeholders: str) -> str:
        raise NotImplementedError


class SQLiteBackend(Backend):
    dialect = "sqlite"

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return Connection(conn, self.dialect)

    order_col = "rowid"

    def render_schema(self, schema: str) -> str:
        return (
            schema.replace("{{AUTOINC_PK}}", "INTEGER PRIMARY KEY AUTOINCREMENT")
            .replace("{{NOCASE}}", "COLLATE NOCASE")
            .replace("{{SEQ_COL}}", "")  # SQLite uses the implicit rowid
            .replace("{{EXTRA_DDL}}", "")
        )

    def insert_or_ignore(self, table: str, columns: str, placeholders: str) -> str:
        return f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})"


class PostgresBackend(Backend):
    dialect = "postgres"

    # Case-insensitive uniqueness that SQLite gets from COLLATE NOCASE.
    _EXTRA_DDL = (
        "CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower "
        "ON users (lower(username));"
        "CREATE UNIQUE INDEX IF NOT EXISTS groups_name_lower "
        "ON groups (lower(name));"
    )

    order_col = "seq"

    def __init__(self, url: str):
        self.url = url

    def connect(self) -> Connection:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(self.url, row_factory=dict_row, autocommit=False)
        return Connection(conn, self.dialect)

    def render_schema(self, schema: str) -> str:
        return (
            schema.replace("{{AUTOINC_PK}}", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
            .replace("{{NOCASE}}", "")
            .replace("{{SEQ_COL}}", "seq BIGINT GENERATED BY DEFAULT AS IDENTITY,")
            .replace("{{EXTRA_DDL}}", self._EXTRA_DDL)
        )

    def insert_or_ignore(self, table: str, columns: str, placeholders: str) -> str:
        return (
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT DO NOTHING"
        )


def make_backend(path_or_url: Path | str) -> Backend:
    if is_postgres_url(str(path_or_url)):
        return PostgresBackend(str(path_or_url))
    return SQLiteBackend(path_or_url)
