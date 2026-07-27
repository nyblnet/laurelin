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

    def ensure_namespace(self, conn: Connection) -> None:
        """Create the namespace this backend's tables live in, if any.

        A no-op for SQLite (one file *is* the namespace); creates the schema
        for a namespaced Postgres backend.
        """
        return None

    # -- accelerated substring search ------------------------------------------
    #
    # Object search is a case-insensitive substring match, and that definition
    # is shared with the DuckDB scan path — the index must never return a
    # different answer, only a faster one. So this is *trigram* indexing, which
    # accelerates `LIKE '%needle%'` while preserving its exact semantics, not
    # full-text search, which would silently redefine what a search means
    # (token matching finds "minas" in "Minas Tirith" but never "inas Ti").
    #
    # Both dialects have it, and on both it is best-effort: an old SQLite built
    # without FTS5, or a managed Postgres that won't grant CREATE EXTENSION,
    # simply falls back to an unindexed LIKE. Same results, less speed.

    def ensure_search_index(self, conn: Connection) -> bool:
        """Create the trigram index if this backend can. Returns availability."""
        return False

    def sync_search_index(self, conn: Connection, object_type: str,
                          rows: list[tuple[str, str]]) -> None:
        """Mirror ``(pk, search_text)`` for one object type. No-op when the
        acceleration needs no separate storage."""
        return None

    def clear_search_index(self, conn: Connection, object_type: str) -> None:
        return None

    def append_search_filter(self, where: list[str], params: list,
                             object_type: str, needle: str) -> None:
        """Add the substring predicate, using the trigram index if present."""
        where.append("search_text LIKE ?")
        params.append(f"%{needle.lower()}%")

    def strpos(self, haystack: str, needle_placeholder: str = "?") -> str:
        """1-based position of a substring, 0 when absent.

        Ranking needs this in three engines: SQLite spells it ``instr``,
        DuckDB and PostgreSQL ``strpos``.
        """
        return f"instr({haystack}, {needle_placeholder})"


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

    # SQLite has no way to index a leading-wildcard LIKE on an ordinary column,
    # so the trigram tokenizer is exposed through an FTS5 virtual table that
    # mirrors (object_type, pk, search_text). It is written in the same
    # transaction as the index itself, so the two cannot drift.
    _has_fts: Optional[bool] = None

    def ensure_search_index(self, conn: Connection) -> bool:
        if self._has_fts is None:
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS object_search USING fts5("
                    "  object_type UNINDEXED, pk UNINDEXED, search_text,"
                    "  tokenize='trigram')"
                )
                self._has_fts = True
            except Exception:
                # FTS5 absent, or built without the trigram tokenizer (3.34+).
                self._has_fts = False
        return self._has_fts

    def sync_search_index(self, conn: Connection, object_type: str,
                          rows: list[tuple[str, str]]) -> None:
        if not self.ensure_search_index(conn):
            return
        conn.execute("DELETE FROM object_search WHERE object_type = ?", (object_type,))
        conn.executemany(
            "INSERT INTO object_search (object_type, pk, search_text) VALUES (?, ?, ?)",
            [(object_type, pk, text) for pk, text in rows],
        )

    def clear_search_index(self, conn: Connection, object_type: str) -> None:
        if self._has_fts:
            conn.execute("DELETE FROM object_search WHERE object_type = ?", (object_type,))

    def append_search_filter(self, where: list[str], params: list,
                             object_type: str, needle: str) -> None:
        if self._has_fts:
            where.append(
                "pk IN (SELECT pk FROM object_search"
                "        WHERE object_type = ? AND search_text LIKE ?)"
            )
            params.extend([object_type, f"%{needle.lower()}%"])
        else:
            super().append_search_filter(where, params, object_type, needle)


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

    def __init__(self, url: str, schema: Optional[str] = None):
        self.url = url
        # When set, every connection is scoped to this Postgres schema, so the
        # identical table definitions can be instantiated once per workspace
        # inside one database. This is what lets many workspaces share an HA
        # Postgres instead of each owning a SQLite file on a shared volume.
        self.schema = schema

    @staticmethod
    def quote_ident(name: str) -> str:
        """Quote an identifier. Workspace slugs may contain '-', which is not
        valid unquoted — and quoting avoids the collisions that sanitizing
        (mapping '-' to '_') would introduce."""
        return '"' + name.replace('"', '""') + '"'

    def connect(self) -> Connection:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(self.url, row_factory=dict_row, autocommit=False)
        if self.schema:
            # Setting the path before the schema exists is fine: it only
            # affects name resolution, and ensure_namespace() creates it on
            # this same connection before any DDL runs.
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {self.quote_ident(self.schema)}, public")
            conn.commit()
        return Connection(conn, self.dialect)

    def ensure_namespace(self, conn: Connection) -> None:
        if self.schema:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.quote_ident(self.schema)}")

    # pg_trgm indexes the existing column, so unlike SQLite there is no mirror
    # table and no query change at all — the same `search_text LIKE ?` simply
    # starts using a GIN index. Creating an extension needs privileges a
    # managed Postgres may withhold, hence best-effort.
    _has_trgm: Optional[bool] = None

    def ensure_search_index(self, conn: Connection) -> bool:
        if self._has_trgm is None:
            try:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_object_index_search "
                    "ON object_index USING gin (search_text gin_trgm_ops)"
                )
                self._has_trgm = True
            except Exception:
                conn.rollback()  # the failed DDL poisons the transaction
                self._has_trgm = False
        return self._has_trgm

    def strpos(self, haystack: str, needle_placeholder: str = "?") -> str:
        return f"strpos({haystack}, {needle_placeholder})"

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


def make_backend(path_or_url: Path | str, schema: Optional[str] = None) -> Backend:
    if is_postgres_url(str(path_or_url)):
        return PostgresBackend(str(path_or_url), schema=schema)
    if schema:
        raise ValueError(
            "A schema-scoped store requires a postgresql:// URL; SQLite has no schemas"
        )
    return SQLiteBackend(path_or_url)
