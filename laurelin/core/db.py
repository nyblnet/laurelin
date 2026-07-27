"""MetadataStore: the catalog of versions, builds, lineage, edits, audit, users,
grants and policies.

Runs on either SQLite (embedded, a single inspectable file — the default) or
PostgreSQL (server / multi-tenant), chosen by the constructor argument: a
filesystem path selects SQLite, a ``postgresql://`` URL selects Postgres. The
dialect differences are handled by ``laurelin/core/backend.py`` so the SQL here
is shared. Each operation opens a short-lived connection.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from laurelin.core.backend import Connection, make_backend
from laurelin.core.models import (
    AuditEvent,
    BuildInfo,
    BuildStatus,
    BuildTaskInfo,
    ColumnSchema,
    DashboardInfo,
    DashboardPanel,
    DatasetInfo,
    DatasetVersionInfo,
    EditKind,
    LineageEdge,
    ObjectAppInfo,
    ObjectEdit,
    Role,
    ScheduleInfo,
    SourceInfo,
    User,
    utcnow_iso,
)

# How many matches a *search* counts before it stops counting and reports the
# cap. Browsing is unaffected: an unfiltered count is a cheap indexed count and
# stays exact.
#
# Searching is different. Substring matching is the one predicate no index can
# make cheap for a broad term — measured, an exact count over 28,000 matches
# was *slower* through the trigram index (130 ms) than through a plain scan
# (63 ms), because the index has to visit every match to count it. Saturating
# the count makes both paths fast and costs nothing real: nobody pages to
# result 10,000, and "10,000+" is the honest answer anyway.
#
# Applied identically by the index and the DuckDB scan, because those two must
# never disagree.
SEARCH_TOTAL_CAP = 10_000


def count_sql(inner_select: str, capped: bool, alias: str = "n") -> str:
    """Wrap a row-producing SELECT in a count, saturating when capped."""
    limited = f"{inner_select} LIMIT {SEARCH_TOTAL_CAP}" if capped else inner_select
    return f"SELECT count(*) AS {alias} FROM ({limited}) t"


def _iso_in(seconds: int) -> str:
    """An ISO timestamp `seconds` from now — lease expiry math in one place."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'managed',
    source_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    schema_json TEXT NOT NULL DEFAULT '[]',
    path TEXT NOT NULL DEFAULT '',
    files_json TEXT NOT NULL DEFAULT '[]',
    build_id TEXT,
    source TEXT NOT NULL DEFAULT 'upload',
    PRIMARY KEY (dataset, version)
);
CREATE TABLE IF NOT EXISTS object_index (
    object_type TEXT NOT NULL,
    pk TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    -- Lower-cased concatenation of the string properties, for substring search
    -- without scanning Parquet.
    search_text TEXT NOT NULL DEFAULT '',
    props_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (object_type, pk)
);
CREATE INDEX IF NOT EXISTS idx_object_index_type ON object_index (object_type);
CREATE TABLE IF NOT EXISTS object_index_state (
    object_type TEXT PRIMARY KEY,
    -- The dataset version this index was built from, plus the edit-log length.
    -- Both must still match for the index to be trusted.
    dataset_version INTEGER NOT NULL DEFAULT 0,
    edit_count INTEGER NOT NULL DEFAULT 0,
    object_count INTEGER NOT NULL DEFAULT 0,
    built_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transform_state (
    transform_name TEXT NOT NULL,
    input_dataset TEXT NOT NULL,
    -- Highest input version already folded into the output, so the next build
    -- knows what "new" means.
    last_version INTEGER NOT NULL,
    last_rows INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (transform_name, input_dataset)
);
CREATE TABLE IF NOT EXISTS object_apps (
    name TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    object_type TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    trigger_type TEXT NOT NULL DEFAULT 'cron',
    cron TEXT NOT NULL DEFAULT '',
    upstream_dataset TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT 'build',
    targets_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '',
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    last_build_id TEXT,
    watermark INTEGER,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    -- Same coordination primitive as build leases: exactly one replica fires a
    -- due schedule, and a dead replica's claim expires rather than wedging it.
    claimed_by TEXT,
    lease_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS engines (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'flightsql',
    uri TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    dataset TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    last_sync_at TEXT,
    last_sync_status TEXT,
    last_sync_error TEXT,
    last_sync_version INTEGER,
    last_sync_rows INTEGER,
    cursor_value TEXT
);
CREATE TABLE IF NOT EXISTS dashboards (
    name TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    panels_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS builds (
    {{SEQ_COL}}
    id TEXT PRIMARY KEY,
    targets_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    -- Cross-replica coordination: exactly one worker may own a build, and the
    -- lease expires so a dead replica's build can be reclaimed.
    claimed_by TEXT,
    lease_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS build_tasks (
    build_id TEXT NOT NULL,
    transform_name TEXT NOT NULL,
    output_dataset TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    rows_written INTEGER,
    output_version INTEGER,
    -- One JSON entry per declared expectation, pass or fail. A check that
    -- passed is evidence worth keeping, not just noise before a failure.
    expectations_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (build_id, transform_name)
);
CREATE TABLE IF NOT EXISTS lineage_edges (
    upstream_dataset TEXT NOT NULL,
    downstream_dataset TEXT NOT NULL,
    transform_name TEXT NOT NULL,
    PRIMARY KEY (upstream_dataset, downstream_dataset, transform_name)
);
CREATE TABLE IF NOT EXISTS object_edits (
    {{SEQ_COL}}
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    pk_value TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT 'anonymous',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_object_edits_type ON object_edits (object_type, created_at);
CREATE TABLE IF NOT EXISTS audit_log (
    id {{AUTOINC_PK}},
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'anonymous',
    action TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE {{NOCASE}},
    password_hash TEXT,
    role TEXT CHECK(role IN ('viewer','editor','admin')),
    created_at TEXT,
    disabled INTEGER DEFAULT 0,
    superadmin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT,
    created_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    name TEXT,
    token_hash TEXT UNIQUE,
    user_id TEXT,
    created_at TEXT,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS groups (
    name TEXT PRIMARY KEY {{NOCASE}},
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_name TEXT NOT NULL {{NOCASE}},
    username TEXT NOT NULL {{NOCASE}},
    PRIMARY KEY (group_name, username)
);
CREATE TABLE IF NOT EXISTS ontology_grants (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    can_view INTEGER NOT NULL DEFAULT 0,
    can_edit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_type ON ontology_grants (object_type);
CREATE TABLE IF NOT EXISTS dataset_grants (
    id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    can_view INTEGER NOT NULL DEFAULT 0,
    can_edit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dataset_grants ON dataset_grants (dataset);
CREATE TABLE IF NOT EXISTS dataset_policies (
    dataset TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oidc_flows (
    state TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS markings (
    name TEXT PRIMARY KEY {{NOCASE}},
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_markings (
    dataset TEXT NOT NULL,
    marking TEXT NOT NULL,
    inherited INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset, marking, inherited)
);
CREATE INDEX IF NOT EXISTS idx_dataset_markings ON dataset_markings (dataset);
CREATE TABLE IF NOT EXISTS clearances (
    username TEXT NOT NULL {{NOCASE}},
    marking TEXT NOT NULL,
    PRIMARY KEY (username, marking)
);
CREATE INDEX IF NOT EXISTS idx_clearances_user ON clearances (username);
{{EXTRA_DDL}}
"""


class MetadataStore:
    def __init__(self, path: Path | str, schema: Optional[str] = None):
        # ``path`` is a filesystem path (SQLite, embedded) or a postgres:// URL.
        # ``schema`` scopes the tables to a Postgres schema, which is how many
        # workspaces share one HA database instead of one SQLite file each.
        self.path = path
        self.schema = schema
        self.backend = make_backend(path, schema=schema)
        self.dialect = self.backend.dialect
        self._ensure_schema()

    @contextmanager
    def _conn(self) -> Iterator[Connection]:
        conn = self.backend.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as c:
            self.backend.ensure_namespace(c)
            c.executescript(self.backend.render_schema(_SCHEMA))
            self._migrate(c)

    def _has_column(self, c: Connection, table: str, column: str) -> bool:
        if self.dialect == "postgres":
            row = c.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = ? AND column_name = ?",
                (table, column),
            ).fetchone()
            return row is not None
        return column in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}

    def _migrate(self, c: Connection) -> None:
        """Additive migrations for databases created by older versions."""
        if not self._has_column(c, "users", "superadmin"):
            c.execute("ALTER TABLE users ADD COLUMN superadmin INTEGER NOT NULL DEFAULT 0")
        # Manifest of Parquet parts per version. Versions predating this column
        # keep an empty manifest and are read by globbing their version dir.
        if not self._has_column(c, "dataset_versions", "files_json"):
            c.execute(
                "ALTER TABLE dataset_versions ADD COLUMN files_json TEXT NOT NULL DEFAULT '[]'"
            )
        if not self._has_column(c, "sources", "cursor_value"):
            c.execute("ALTER TABLE sources ADD COLUMN cursor_value TEXT")
        for col in ("claimed_by", "lease_expires_at"):
            if not self._has_column(c, "builds", col):
                c.execute(f"ALTER TABLE builds ADD COLUMN {col} TEXT")
        if not self._has_column(c, "datasets", "kind"):
            c.execute("ALTER TABLE datasets ADD COLUMN kind TEXT NOT NULL DEFAULT 'managed'")
        if not self._has_column(c, "datasets", "source_json"):
            c.execute("ALTER TABLE datasets ADD COLUMN source_json TEXT NOT NULL DEFAULT '{}'")
        if not self._has_column(c, "build_tasks", "expectations_json"):
            c.execute(
                "ALTER TABLE build_tasks ADD COLUMN expectations_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )

    # -- datasets -------------------------------------------------------------

    def upsert_dataset(self, name: str, description: str = "") -> DatasetInfo:
        with self._conn() as c:
            # Single atomic statement: concurrent writers must not race a
            # check-then-insert. Description only overwrites when non-empty.
            c.execute(
                """INSERT INTO datasets (name, description, created_at) VALUES (?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET description = excluded.description
                   WHERE excluded.description != ''
                     AND excluded.description != datasets.description""",
                (name, description, utcnow_iso()),
            )
        return self.get_dataset(name)  # type: ignore[return-value]

    def get_dataset(self, name: str) -> Optional[DatasetInfo]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM datasets WHERE name = ?", (name,)).fetchone()
            if row is None:
                return None
            latest = c.execute(
                "SELECT MAX(version) AS v FROM dataset_versions WHERE dataset = ?", (name,)
            ).fetchone()["v"]
        return DatasetInfo(
            name=row["name"],
            description=row["description"],
            created_at=row["created_at"],
            latest_version=latest,
            kind=row["kind"],
            source=json.loads(row["source_json"] or "{}"),
        )

    def set_dataset_source(self, name: str, kind: str, source: dict) -> None:
        """Mark a dataset as managed or federated, with its source config."""
        with self._conn() as c:
            c.execute(
                "UPDATE datasets SET kind = ?, source_json = ? WHERE name = ?",
                (kind, json.dumps(source), name),
            )

    def list_datasets(self) -> list[DatasetInfo]:
        with self._conn() as c:
            names = [r["name"] for r in c.execute("SELECT name FROM datasets ORDER BY name")]
        return [d for n in names if (d := self.get_dataset(n)) is not None]

    # -- versions ---------------------------------------------------------------

    def next_version(self, dataset: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(version) AS v FROM dataset_versions WHERE dataset = ?", (dataset,)
            ).fetchone()
        return (row["v"] or 0) + 1

    def add_version(self, info: DatasetVersionInfo) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO dataset_versions
                   (dataset, version, created_at, row_count, schema_json, path,
                    files_json, build_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    info.dataset,
                    info.version,
                    info.created_at,
                    info.row_count,
                    json.dumps([s.model_dump() for s in info.schema_]),
                    info.path,
                    json.dumps(info.files),
                    info.build_id,
                    info.source,
                ),
            )

    def _row_to_version(self, row: dict) -> DatasetVersionInfo:
        return DatasetVersionInfo(
            dataset=row["dataset"],
            version=row["version"],
            created_at=row["created_at"],
            row_count=row["row_count"],
            schema=[ColumnSchema(**s) for s in json.loads(row["schema_json"])],
            path=row["path"],
            files=json.loads(row["files_json"] or "[]"),
            build_id=row["build_id"],
            source=row["source"],
        )

    def get_version(self, dataset: str, version: Optional[int] = None) -> Optional[DatasetVersionInfo]:
        with self._conn() as c:
            if version is None:
                row = c.execute(
                    "SELECT * FROM dataset_versions WHERE dataset = ? ORDER BY version DESC LIMIT 1",
                    (dataset,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM dataset_versions WHERE dataset = ? AND version = ?",
                    (dataset, version),
                ).fetchone()
        return self._row_to_version(row) if row else None

    def list_versions(self, dataset: str) -> list[DatasetVersionInfo]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM dataset_versions WHERE dataset = ? ORDER BY version", (dataset,)
            ).fetchall()
        return [self._row_to_version(r) for r in rows]

    # -- object index ---------------------------------------------------------------
    #
    # See SEARCH_TOTAL_CAP below for why a search reports a saturating total.

    def replace_object_index(
        self, object_type: str, rows: list[dict], dataset_version: int, edit_count: int
    ) -> None:
        """Swap in a freshly built index for one object type, atomically."""
        with self._conn() as c:
            self.backend.ensure_search_index(c)
            c.execute("DELETE FROM object_index WHERE object_type = ?", (object_type,))
            for row in rows:
                c.execute(
                    """INSERT INTO object_index
                         (object_type, pk, title, search_text, props_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (object_type, row["pk"], row["title"], row["search_text"],
                     json.dumps(row["props"])),
                )
            # Same transaction as the rows above, so the search mirror can
            # never be left describing an index that no longer exists.
            self.backend.sync_search_index(
                c, object_type, [(r["pk"], r["search_text"]) for r in rows]
            )
            c.execute(
                """INSERT INTO object_index_state
                     (object_type, dataset_version, edit_count, object_count, built_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (object_type) DO UPDATE SET
                     dataset_version = excluded.dataset_version,
                     edit_count = excluded.edit_count,
                     object_count = excluded.object_count,
                     built_at = excluded.built_at""",
                (object_type, dataset_version, edit_count, len(rows), utcnow_iso()),
            )

    def object_index_state(self, object_type: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM object_index_state WHERE object_type = ?", (object_type,)
            ).fetchone()
        if row is None:
            return None
        return {"dataset_version": row["dataset_version"],
                "edit_count": row["edit_count"],
                "object_count": row["object_count"],
                "built_at": row["built_at"]}

    def drop_object_index(self, object_type: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM object_index WHERE object_type = ?", (object_type,))
            c.execute("DELETE FROM object_index_state WHERE object_type = ?", (object_type,))
            self.backend.clear_search_index(c, object_type)

    def count_object_edits(self, object_type: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT count(*) AS n FROM object_edits WHERE object_type = ?",
                (object_type,),
            ).fetchone()
        return int(row["n"])

    def search_object_index(
        self,
        object_type: str,
        search: Optional[str] = None,
        pk: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Page the index. Returns ``(rows, total)``.

        Only the primary key is filterable here, and deliberately: it is a real
        indexed column, so a lookup is a b-tree probe. Filtering on an
        arbitrary property would mean extracting JSON on every row — measured
        *slower* than the DuckDB scan it was meant to beat — so those queries
        are left to the scan path, which prunes Parquet row groups instead.
        """
        where = ["object_type = ?"]
        params: list = [object_type]
        if search:
            self.backend.append_search_filter(where, params, object_type, search)
        if pk is not None:
            where.append("pk = ?")
            params.append(str(pk))
        clause = " AND ".join(where)
        with self._conn() as c:
            total = c.execute(
                count_sql(f"SELECT 1 FROM object_index WHERE {clause}", bool(search)),
                tuple(params),
            ).fetchone()["n"]
            rows = c.execute(
                f"""SELECT pk, title, props_json FROM object_index WHERE {clause}
                    ORDER BY pk LIMIT ? OFFSET ?""",
                tuple([*params, max(0, limit), max(0, offset)]),
            ).fetchall()
        return (
            [{"pk": r["pk"], "title": r["title"], "props": json.loads(r["props_json"])}
             for r in rows],
            int(total),
        )

    # -- incremental transform state -----------------------------------------------

    def get_transform_state(self, transform: str, dataset: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM transform_state WHERE transform_name = ? AND input_dataset = ?",
                (transform, dataset),
            ).fetchone()
        if row is None:
            return None
        return {"last_version": row["last_version"], "last_rows": row["last_rows"]}

    def set_transform_state(
        self, transform: str, dataset: str, last_version: int, last_rows: int
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO transform_state
                     (transform_name, input_dataset, last_version, last_rows, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (transform_name, input_dataset) DO UPDATE SET
                     last_version = excluded.last_version,
                     last_rows = excluded.last_rows,
                     updated_at = excluded.updated_at""",
                (transform, dataset, last_version, last_rows, utcnow_iso()),
            )

    def clear_transform_state(self, transform: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM transform_state WHERE transform_name = ?", (transform,))

    # -- object apps ---------------------------------------------------------------

    def upsert_object_app(self, info: "ObjectAppInfo") -> None:
        config = info.model_dump(
            mode="json",
            include={"columns", "filters", "search_placeholder", "actions", "links"},
        )
        with self._conn() as c:
            c.execute(
                """INSERT INTO object_apps
                     (name, title, description, object_type, config_json,
                      created_at, created_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET
                     title = excluded.title,
                     description = excluded.description,
                     object_type = excluded.object_type,
                     config_json = excluded.config_json,
                     updated_at = excluded.updated_at""",
                (info.name, info.title, info.description, info.object_type,
                 json.dumps(config), info.created_at, info.created_by,
                 info.updated_at),
            )

    def _row_to_object_app(self, row) -> "ObjectAppInfo":
        config = json.loads(row["config_json"] or "{}")
        return ObjectAppInfo(
            name=row["name"], title=row["title"], description=row["description"],
            object_type=row["object_type"], created_at=row["created_at"],
            created_by=row["created_by"], updated_at=row["updated_at"], **config,
        )

    def get_object_app(self, name: str) -> Optional["ObjectAppInfo"]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM object_apps WHERE name = ?", (name,)).fetchone()
        return self._row_to_object_app(row) if row else None

    def list_object_apps(self) -> list["ObjectAppInfo"]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM object_apps ORDER BY name").fetchall()
        return [self._row_to_object_app(r) for r in rows]

    def delete_object_app(self, name: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM object_apps WHERE name = ?", (name,)).rowcount > 0

    # -- schedules -----------------------------------------------------------------

    def upsert_schedule(self, info: "ScheduleInfo") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO schedules
                     (name, enabled, trigger_type, cron, upstream_dataset, action,
                      targets_json, source, next_run_at, watermark, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET
                     enabled = excluded.enabled,
                     trigger_type = excluded.trigger_type,
                     cron = excluded.cron,
                     upstream_dataset = excluded.upstream_dataset,
                     action = excluded.action,
                     targets_json = excluded.targets_json,
                     source = excluded.source,
                     next_run_at = excluded.next_run_at""",
                (
                    info.name, 1 if info.enabled else 0, info.trigger, info.cron,
                    info.upstream_dataset, info.action, json.dumps(info.targets),
                    info.source, info.next_run_at, info.watermark,
                    info.created_at, info.created_by,
                ),
            )

    def _row_to_schedule(self, row) -> "ScheduleInfo":
        return ScheduleInfo(
            name=row["name"],
            enabled=bool(row["enabled"]),
            trigger=row["trigger_type"],
            cron=row["cron"],
            upstream_dataset=row["upstream_dataset"],
            action=row["action"],
            targets=json.loads(row["targets_json"] or "[]"),
            source=row["source"],
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            last_build_id=row["last_build_id"],
            watermark=row["watermark"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )

    def get_schedule(self, name: str) -> Optional["ScheduleInfo"]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM schedules WHERE name = ?", (name,)).fetchone()
        return self._row_to_schedule(row) if row else None

    def list_schedules(self) -> list["ScheduleInfo"]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM schedules ORDER BY name").fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def delete_schedule(self, name: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM schedules WHERE name = ?", (name,)).rowcount > 0

    def due_schedules(self, now_iso: str) -> list["ScheduleInfo"]:
        """Enabled schedules that want to fire: cron ones past their next run,
        and every upstream one (whose watermark is compared by the caller)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM schedules
                   WHERE enabled = 1
                     AND (trigger_type = 'upstream'
                          OR (next_run_at IS NOT NULL AND next_run_at <= ?))
                   ORDER BY name""",
                (now_iso,),
            ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def claim_schedule(self, name: str, worker: str, lease_seconds: int = 300) -> bool:
        """Take ownership of a due schedule. One conditional UPDATE, so with
        several replicas polling, exactly one fires it."""
        now = utcnow_iso()
        with self._conn() as c:
            cur = c.execute(
                """UPDATE schedules SET claimed_by = ?, lease_expires_at = ?
                   WHERE name = ? AND enabled = 1
                     AND (claimed_by IS NULL OR lease_expires_at IS NULL
                          OR lease_expires_at < ?)""",
                (worker, _iso_in(lease_seconds), name, now),
            )
            return cur.rowcount > 0

    def record_schedule_run(
        self,
        name: str,
        status: str,
        next_run_at: Optional[str] = None,
        error: Optional[str] = None,
        build_id: Optional[str] = None,
        watermark: Optional[int] = None,
    ) -> None:
        """Record an attempt and release the claim, so the next window is
        free regardless of how this one went."""
        with self._conn() as c:
            c.execute(
                """UPDATE schedules SET last_run_at = ?, last_status = ?,
                     last_error = ?, last_build_id = ?, next_run_at = ?,
                     watermark = COALESCE(?, watermark),
                     claimed_by = NULL, lease_expires_at = NULL
                   WHERE name = ?""",
                (utcnow_iso(), status, error, build_id, next_run_at,
                 watermark, name),
            )

    # -- delegated engines ---------------------------------------------------------

    def upsert_engine(self, name: str, type_: str, uri: str, options: dict,
                      created_by: str = "") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO engines (name, type, uri, options_json, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET
                     type = excluded.type,
                     uri = excluded.uri,
                     options_json = excluded.options_json""",
                (name, type_, uri, json.dumps(options), utcnow_iso(), created_by),
            )

    def get_engine(self, name: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM engines WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return {"name": row["name"], "type": row["type"], "uri": row["uri"],
                "options": json.loads(row["options_json"] or "{}"),
                "created_at": row["created_at"], "created_by": row["created_by"]}

    def list_engines(self) -> list[dict]:
        with self._conn() as c:
            names = [r["name"] for r in c.execute("SELECT name FROM engines ORDER BY name")]
        return [e for n in names if (e := self.get_engine(n)) is not None]

    def delete_engine(self, name: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM engines WHERE name = ?", (name,)).rowcount > 0

    # -- sources ------------------------------------------------------------------

    def upsert_source(self, info: "SourceInfo") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO sources (name, type, dataset, config_json, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET
                     type = excluded.type,
                     dataset = excluded.dataset,
                     config_json = excluded.config_json""",
                (
                    info.name,
                    info.type,
                    info.dataset,
                    json.dumps(info.config),
                    info.created_at,
                    info.created_by,
                ),
            )

    def _row_to_source(self, row) -> "SourceInfo":
        return SourceInfo(
            name=row["name"],
            type=row["type"],
            dataset=row["dataset"],
            config=json.loads(row["config_json"]),
            created_at=row["created_at"],
            created_by=row["created_by"],
            last_sync_at=row["last_sync_at"],
            last_sync_status=row["last_sync_status"],
            last_sync_error=row["last_sync_error"],
            last_sync_version=row["last_sync_version"],
            last_sync_rows=row["last_sync_rows"],
            cursor_value=row["cursor_value"],
        )

    def get_source(self, name: str) -> Optional["SourceInfo"]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
        return self._row_to_source(row) if row else None

    def list_sources(self) -> list["SourceInfo"]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [self._row_to_source(r) for r in rows]

    def delete_source(self, name: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM sources WHERE name = ?", (name,))
            return cur.rowcount > 0

    def record_source_sync(
        self,
        name: str,
        status: str,
        error: Optional[str] = None,
        version: Optional[int] = None,
        rows: Optional[int] = None,
        cursor_value: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            if cursor_value is None:
                # Leave the high-water mark untouched (failed or full-refresh sync).
                c.execute(
                    """UPDATE sources SET last_sync_at = ?, last_sync_status = ?,
                       last_sync_error = ?, last_sync_version = ?, last_sync_rows = ?
                       WHERE name = ?""",
                    (utcnow_iso(), status, error, version, rows, name),
                )
            else:
                c.execute(
                    """UPDATE sources SET last_sync_at = ?, last_sync_status = ?,
                       last_sync_error = ?, last_sync_version = ?, last_sync_rows = ?,
                       cursor_value = ? WHERE name = ?""",
                    (utcnow_iso(), status, error, version, rows, cursor_value, name),
                )

    # -- dashboards --------------------------------------------------------------

    def upsert_dashboard(self, info: "DashboardInfo") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO dashboards
                     (name, title, description, panels_json, created_at, created_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET
                     title = excluded.title,
                     description = excluded.description,
                     panels_json = excluded.panels_json,
                     updated_at = excluded.updated_at""",
                (
                    info.name,
                    info.title,
                    info.description,
                    json.dumps([p.model_dump() for p in info.panels]),
                    info.created_at,
                    info.created_by,
                    info.updated_at,
                ),
            )

    def _row_to_dashboard(self, row) -> "DashboardInfo":
        return DashboardInfo(
            name=row["name"],
            title=row["title"],
            description=row["description"],
            panels=[DashboardPanel(**p) for p in json.loads(row["panels_json"])],
            created_at=row["created_at"],
            created_by=row["created_by"],
            updated_at=row["updated_at"],
        )

    def get_dashboard(self, name: str) -> Optional["DashboardInfo"]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM dashboards WHERE name = ?", (name,)).fetchone()
        return self._row_to_dashboard(row) if row else None

    def list_dashboards(self) -> list["DashboardInfo"]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM dashboards ORDER BY name").fetchall()
        return [self._row_to_dashboard(r) for r in rows]

    def delete_dashboard(self, name: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM dashboards WHERE name = ?", (name,))
            return cur.rowcount > 0

    # -- build leases -------------------------------------------------------------
    #
    # With several replicas serving one workspace, any of them may accept
    # "run a build". Exactly one must execute it. A single conditional UPDATE
    # is the whole mechanism: the row is the lock, and the database decides
    # the winner. Leases expire so a replica that dies mid-build doesn't strand
    # its work forever.

    def claim_build(self, build_id: str, worker: str, lease_seconds: int = 120) -> bool:
        """Try to take ownership of a build. True if this caller won.

        Succeeds when the build is unclaimed, already owned by this worker, or
        held by a lease that has expired.
        """
        now = utcnow_iso()
        expires = _iso_in(lease_seconds)
        with self._conn() as c:
            cur = c.execute(
                """UPDATE builds SET claimed_by = ?, lease_expires_at = ?
                   WHERE id = ?
                     AND status IN ('pending', 'running')
                     AND (claimed_by IS NULL
                          OR claimed_by = ?
                          OR lease_expires_at IS NULL
                          OR lease_expires_at < ?)""",
                (worker, expires, build_id, worker, now),
            )
            return cur.rowcount > 0

    def renew_build_lease(self, build_id: str, worker: str, lease_seconds: int = 120) -> bool:
        """Extend a lease this worker still holds (heartbeat). False means the
        lease was lost — the worker should stop touching the build."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE builds SET lease_expires_at = ? WHERE id = ? AND claimed_by = ?",
                (_iso_in(lease_seconds), build_id, worker),
            )
            return cur.rowcount > 0

    def release_build(self, build_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE builds SET lease_expires_at = NULL WHERE id = ?", (build_id,)
            )

    def reap_expired_builds(self) -> list[str]:
        """Fail builds whose owner stopped renewing (a replica died mid-build).

        Returns the ids reaped. Without this, a crashed worker leaves a build
        stuck in ``running`` forever.
        """
        now = utcnow_iso()
        with self._conn() as c:
            rows = c.execute(
                """SELECT id FROM builds
                   WHERE status IN ('pending', 'running')
                     AND lease_expires_at IS NOT NULL
                     AND lease_expires_at < ?""",
                (now,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            for build_id in ids:
                c.execute(
                    """UPDATE builds SET status = ?, finished_at = ?, error = ?,
                       claimed_by = NULL, lease_expires_at = NULL WHERE id = ?""",
                    (
                        BuildStatus.failed.value,
                        now,
                        "Abandoned: the worker holding this build stopped responding",
                        build_id,
                    ),
                )
        return ids

    # -- builds -----------------------------------------------------------------

    def create_build(self, targets: list[str]) -> BuildInfo:
        build = BuildInfo(id=uuid.uuid4().hex[:12], targets=targets, status=BuildStatus.pending)
        with self._conn() as c:
            c.execute(
                "INSERT INTO builds (id, targets_json, status) VALUES (?, ?, ?)",
                (build.id, json.dumps(targets), build.status.value),
            )
        return build

    def update_build(
        self,
        build_id: str,
        status: Optional[BuildStatus] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        sets, vals = [], []
        for col, val in (
            ("status", status.value if status else None),
            ("started_at", started_at),
            ("finished_at", finished_at),
            ("error", error),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                vals.append(val)
        if not sets:
            return
        with self._conn() as c:
            c.execute(f"UPDATE builds SET {', '.join(sets)} WHERE id = ?", (*vals, build_id))

    def upsert_build_task(self, build_id: str, task: BuildTaskInfo) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO build_tasks
                   (build_id, transform_name, output_dataset, status, started_at,
                    finished_at, error, rows_written, output_version,
                    expectations_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (build_id, transform_name) DO UPDATE SET
                     output_dataset = excluded.output_dataset,
                     status = excluded.status,
                     started_at = excluded.started_at,
                     finished_at = excluded.finished_at,
                     error = excluded.error,
                     rows_written = excluded.rows_written,
                     output_version = excluded.output_version,
                     expectations_json = excluded.expectations_json""",
                (
                    build_id,
                    task.transform_name,
                    task.output_dataset,
                    task.status.value,
                    task.started_at,
                    task.finished_at,
                    task.error,
                    task.rows_written,
                    task.output_version,
                    json.dumps(task.expectations),
                ),
            )

    def get_build(self, build_id: str) -> Optional[BuildInfo]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if row is None:
                return None
            task_rows = c.execute(
                "SELECT * FROM build_tasks WHERE build_id = ? ORDER BY started_at", (build_id,)
            ).fetchall()
        return BuildInfo(
            id=row["id"],
            targets=json.loads(row["targets_json"]),
            status=BuildStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
            tasks=[
                BuildTaskInfo(
                    transform_name=t["transform_name"],
                    output_dataset=t["output_dataset"],
                    status=BuildStatus(t["status"]),
                    started_at=t["started_at"],
                    finished_at=t["finished_at"],
                    error=t["error"],
                    rows_written=t["rows_written"],
                    output_version=t["output_version"],
                    expectations=json.loads(t["expectations_json"] or "[]"),
                )
                for t in task_rows
            ],
        )

    def list_builds(self, limit: int = 50) -> list[BuildInfo]:
        with self._conn() as c:
            ids = [
                r["id"]
                for r in c.execute(
                    f"SELECT id FROM builds ORDER BY {self.backend.order_col} DESC LIMIT ?",
                    (limit,),
                )
            ]
        return [b for i in ids if (b := self.get_build(i)) is not None]

    # -- lineage ------------------------------------------------------------------

    def replace_lineage_for_transform(self, transform_name: str, edges: list[LineageEdge]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM lineage_edges WHERE transform_name = ?", (transform_name,))
            c.executemany(
                self.backend.insert_or_ignore(
                    "lineage_edges",
                    "upstream_dataset, downstream_dataset, transform_name",
                    "?, ?, ?",
                ),
                [(e.upstream_dataset, e.downstream_dataset, e.transform_name) for e in edges],
            )

    def list_lineage(self) -> list[LineageEdge]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM lineage_edges").fetchall()
        return [
            LineageEdge(
                upstream_dataset=r["upstream_dataset"],
                downstream_dataset=r["downstream_dataset"],
                transform_name=r["transform_name"],
            )
            for r in rows
        ]

    # -- object edits (write-back overlay) ----------------------------------------

    def add_object_edit(self, edit: ObjectEdit) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO object_edits
                   (id, object_type, pk_value, kind, payload_json, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    edit.id,
                    edit.object_type,
                    edit.pk_value,
                    edit.kind.value,
                    json.dumps(edit.payload),
                    edit.actor,
                    edit.created_at,
                ),
            )

    def list_object_edits(self, object_type: str) -> list[ObjectEdit]:
        with self._conn() as c:
            rows = c.execute(
                # Insertion order (SQLite rowid / Postgres seq) — created_at has
                # sub-second collisions and id is a random uuid, so neither gives
                # a stable replay order.
                f"SELECT * FROM object_edits WHERE object_type = ? ORDER BY {self.backend.order_col}",
                (object_type,),
            ).fetchall()
        return [
            ObjectEdit(
                id=r["id"],
                object_type=r["object_type"],
                pk_value=r["pk_value"],
                kind=EditKind(r["kind"]),
                payload=json.loads(r["payload_json"]),
                actor=r["actor"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- audit ---------------------------------------------------------------------

    def log_audit(self, action: str, details: dict[str, Any] | None = None, actor: str = "anonymous") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO audit_log (timestamp, actor, action, details_json) VALUES (?, ?, ?, ?)",
                (utcnow_iso(), actor, action, json.dumps(details or {})),
            )

    def prune_audit(self, keep: int) -> int:
        """Trim the audit log to its most recent ``keep`` events.

        The log grows with every mutation and nothing else bounds it, so a
        long-lived busy workspace would otherwise accumulate rows forever.
        Returns how many were removed; ``keep <= 0`` disables pruning.
        """
        if keep <= 0:
            return 0
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?",
                (keep,),
            ).fetchone()
            if row is None:
                return 0  # fewer than `keep` events; nothing to do
            cur = c.execute("DELETE FROM audit_log WHERE id <= ?", (row["id"],))
            return cur.rowcount or 0

    def list_audit(self, limit: int = 100) -> list[AuditEvent]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            AuditEvent(
                id=r["id"],
                timestamp=r["timestamp"],
                actor=r["actor"],
                action=r["action"],
                details=json.loads(r["details_json"]),
            )
            for r in rows
        ]

    # -- users -----------------------------------------------------------------

    @staticmethod
    def _row_to_user(row: dict) -> User:
        keys = row.keys()
        return User(
            id=row["id"],
            username=row["username"],
            role=Role(row["role"]),
            created_at=row["created_at"],
            disabled=bool(row["disabled"]),
            superadmin=bool(row["superadmin"]) if "superadmin" in keys else False,
        )

    _USER_INSERT = (
        "INSERT INTO users (id, username, password_hash, role, created_at, "
        "disabled, superadmin) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )

    @staticmethod
    def _user_insert_params(user: User, password_hash: str) -> tuple:
        return (
            user.id,
            user.username,
            password_hash,
            user.role.value,
            user.created_at,
            int(user.disabled),
            int(user.superadmin),
        )

    def create_user(self, user: User, password_hash: str) -> None:
        with self._conn() as c:
            c.execute(self._USER_INSERT, self._user_insert_params(user, password_hash))

    def create_user_if_none_exist(self, user: User, password_hash: str) -> bool:
        """Atomically create the first user. Returns False (without inserting) if
        any user already exists, so concurrent first-run setups can't both win.
        An exclusive lock up front serializes racers on both dialects."""
        with self._conn() as c:
            if self.dialect == "postgres":
                c.execute("LOCK TABLE users IN EXCLUSIVE MODE")
            else:
                c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] > 0:
                return False
            c.execute(self._USER_INSERT, self._user_insert_params(user, password_hash))
            return True

    def get_user(self, username: str) -> Optional[User]:
        """Look a user up by username (case-insensitive per COLLATE NOCASE)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE username = ?", (username.lower(),)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_password_hash(self, username: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username.lower(),)
            ).fetchone()
        return row["password_hash"] if row else None

    def list_users(self) -> list[User]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [self._row_to_user(r) for r in rows]

    def count_users(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def update_user(
        self,
        username: str,
        *,
        role: Optional[str] = None,
        password_hash: Optional[str] = None,
        disabled: Optional[bool] = None,
    ) -> None:
        sets, vals = [], []
        if role is not None:
            sets.append("role = ?")
            vals.append(role)
        if password_hash is not None:
            sets.append("password_hash = ?")
            vals.append(password_hash)
        if disabled is not None:
            sets.append("disabled = ?")
            vals.append(int(disabled))
        if not sets:
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE username = ?",
                (*vals, username.lower()),
            )

    def delete_user(self, username: str) -> None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE username = ?", (username.lower(),)
            ).fetchone()
            if row is None:
                return
            user_id = row["id"]
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM group_members WHERE username = ?", (username.lower(),))
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # -- sessions ----------------------------------------------------------------

    def create_session(
        self, token_hash: str, user_id: str, created_at: str, expires_at: str
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, user_id, created_at, expires_at),
            )

    def get_session(self, token_hash: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def purge_expired_sessions(self, now_iso: str) -> None:
        """ISO-8601 UTC timestamps sort lexicographically, so string compare works."""
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))

    # -- api tokens ---------------------------------------------------------------

    def _token_row_to_dict(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "user_id": row["user_id"],
            "username": row["username"] if "username" in row.keys() else None,
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    _TOKEN_SELECT = (
        "SELECT t.id, t.name, t.user_id, t.token_hash, t.created_at, "
        "t.last_used_at, u.username FROM api_tokens t "
        "LEFT JOIN users u ON u.id = t.user_id"
    )

    def create_api_token(
        self, token_id: str, name: str, token_hash: str, user_id: str, created_at: str
    ) -> dict:
        with self._conn() as c:
            c.execute(
                """INSERT INTO api_tokens (id, name, token_hash, user_id, created_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, NULL)""",
                (token_id, name, token_hash, user_id, created_at),
            )
        token = self.get_api_token(token_id)
        assert token is not None
        return token

    def get_api_token(self, token_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                f"{self._TOKEN_SELECT} WHERE t.id = ?", (token_id,)
            ).fetchone()
        return self._token_row_to_dict(row) if row else None

    def get_api_token_by_hash(self, token_hash: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                f"{self._TOKEN_SELECT} WHERE t.token_hash = ?", (token_hash,)
            ).fetchone()
        return self._token_row_to_dict(row) if row else None

    def list_api_tokens(self, user_id: Optional[str] = None) -> list[dict]:
        with self._conn() as c:
            if user_id is None:
                rows = c.execute(
                    f"{self._TOKEN_SELECT} ORDER BY t.created_at"
                ).fetchall()
            else:
                rows = c.execute(
                    f"{self._TOKEN_SELECT} WHERE t.user_id = ? ORDER BY t.created_at",
                    (user_id,),
                ).fetchall()
        return [self._token_row_to_dict(r) for r in rows]

    def touch_api_token(self, token_id: str, last_used_at: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                (last_used_at, token_id),
            )

    def delete_api_token(self, token_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))

    # -- groups -------------------------------------------------------------------

    def create_group(self, name: str, created_at: str) -> None:
        # Store identity strings lowercase; lookups compare lowercase (Postgres
        # has no COLLATE NOCASE). Callers already lowercase, but normalize here
        # too so the store is correct regardless of caller.
        with self._conn() as c:
            c.execute(
                "INSERT INTO groups (name, created_at) VALUES (?, ?)",
                (name.lower(), created_at),
            )

    def group_exists(self, name: str) -> bool:
        with self._conn() as c:
            return (
                c.execute("SELECT 1 FROM groups WHERE name = ?", (name.lower(),)).fetchone()
                is not None
            )

    def list_groups(self) -> list[dict]:
        with self._conn() as c:
            groups = c.execute(
                "SELECT name, created_at FROM groups ORDER BY name"
            ).fetchall()
            out = []
            for g in groups:
                members = [
                    r["username"]
                    for r in c.execute(
                        "SELECT username FROM group_members WHERE group_name = ? ORDER BY username",
                        (g["name"],),
                    )
                ]
                out.append(
                    {"name": g["name"], "created_at": g["created_at"], "members": members}
                )
        return out

    def delete_group(self, name: str) -> None:
        name = name.lower()
        with self._conn() as c:
            c.execute("DELETE FROM group_members WHERE group_name = ?", (name,))
            c.execute("DELETE FROM groups WHERE name = ?", (name,))

    def set_group_members(self, name: str, usernames: list[str]) -> None:
        name = name.lower()
        with self._conn() as c:
            c.execute("DELETE FROM group_members WHERE group_name = ?", (name,))
            c.executemany(
                self.backend.insert_or_ignore(
                    "group_members", "group_name, username", "?, ?"
                ),
                [(name, u.lower()) for u in usernames],
            )

    def groups_for_user(self, username: str) -> set[str]:
        with self._conn() as c:
            return {
                r["group_name"]
                for r in c.execute(
                    "SELECT group_name FROM group_members WHERE username = ?", (username.lower(),)
                )
            }

    # -- ontology grants ----------------------------------------------------------

    def list_grants(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ontology_grants ORDER BY object_type, subject_kind, subject"
            ).fetchall()
        return [self._grant_row(r) for r in rows]

    def grants_for_type(self, object_type: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ontology_grants WHERE object_type = ?", (object_type,)
            ).fetchall()
        return [self._grant_row(r) for r in rows]

    @staticmethod
    def _grant_row(row: dict) -> dict:
        return {
            "object_type": row["object_type"],
            "subject_kind": row["subject_kind"],
            "subject": row["subject"],
            "can_view": bool(row["can_view"]),
            "can_edit": bool(row["can_edit"]),
        }

    def set_grants_for_type(self, object_type: str, grants: list[dict]) -> None:
        """Replace all grants for an object type atomically."""
        import uuid as _uuid

        with self._conn() as c:
            c.execute("DELETE FROM ontology_grants WHERE object_type = ?", (object_type,))
            c.executemany(
                """INSERT INTO ontology_grants
                   (id, object_type, subject_kind, subject, can_view, can_edit, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        _uuid.uuid4().hex,
                        object_type,
                        g["subject_kind"],
                        g.get("subject", ""),
                        int(g.get("can_view", False)),
                        int(g.get("can_edit", False)),
                        utcnow_iso(),
                    )
                    for g in grants
                ],
            )

    # -- dataset grants -----------------------------------------------------------

    @staticmethod
    def _dataset_grant_row(row: dict) -> dict:
        return {
            "dataset": row["dataset"],
            "subject_kind": row["subject_kind"],
            "subject": row["subject"],
            "can_view": bool(row["can_view"]),
            "can_edit": bool(row["can_edit"]),
        }

    def list_dataset_grants(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM dataset_grants ORDER BY dataset, subject_kind, subject"
            ).fetchall()
        return [self._dataset_grant_row(r) for r in rows]

    def grants_for_dataset(self, dataset: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM dataset_grants WHERE dataset = ?", (dataset,)
            ).fetchall()
        return [self._dataset_grant_row(r) for r in rows]

    def set_grants_for_dataset(self, dataset: str, grants: list[dict]) -> None:
        """Replace all grants for a dataset atomically."""
        import uuid as _uuid

        with self._conn() as c:
            c.execute("DELETE FROM dataset_grants WHERE dataset = ?", (dataset,))
            c.executemany(
                """INSERT INTO dataset_grants
                   (id, dataset, subject_kind, subject, can_view, can_edit, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        _uuid.uuid4().hex,
                        dataset,
                        g["subject_kind"],
                        g.get("subject", ""),
                        int(g.get("can_view", False)),
                        int(g.get("can_edit", False)),
                        utcnow_iso(),
                    )
                    for g in grants
                ],
            )

    # -- dataset policies (row-level security + column masking) -------------------

    def get_dataset_policy(self, dataset: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT policy_json FROM dataset_policies WHERE dataset = ?", (dataset,)
            ).fetchone()
        return json.loads(row["policy_json"]) if row else None

    def list_dataset_policies(self) -> dict[str, dict]:
        with self._conn() as c:
            rows = c.execute("SELECT dataset, policy_json FROM dataset_policies").fetchall()
        return {r["dataset"]: json.loads(r["policy_json"]) for r in rows}

    # -- OIDC transient flow state ------------------------------------------------

    def create_oidc_flow(
        self, state: str, nonce: str, code_verifier: str, redirect_uri: str
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO oidc_flows (state, nonce, code_verifier, redirect_uri, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (state, nonce, code_verifier, redirect_uri, utcnow_iso()),
            )

    def pop_oidc_flow(self, state: str) -> Optional[dict]:
        """Atomically fetch and delete a flow by state (single-use)."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM oidc_flows WHERE state = ?", (state,)).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM oidc_flows WHERE state = ?", (state,))
            return dict(row)

    def purge_oidc_flows(self, before_iso: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM oidc_flows WHERE created_at < ?", (before_iso,))

    # -- classification markings --------------------------------------------------

    def list_markings(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, description, created_at FROM markings ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_marking(self, name: str, description: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO markings (name, description, created_at) VALUES (?, ?, ?)",
                (name.lower(), description, utcnow_iso()),
            )

    def marking_exists(self, name: str) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM markings WHERE name = ?", (name.lower(),)
            ).fetchone() is not None

    def delete_marking(self, name: str) -> None:
        name = name.lower()
        with self._conn() as c:
            c.execute("DELETE FROM markings WHERE name = ?", (name,))
            c.execute("DELETE FROM dataset_markings WHERE marking = ?", (name,))
            c.execute("DELETE FROM clearances WHERE marking = ?", (name,))

    def _markings_for(self, dataset: str, inherited: int) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT marking FROM dataset_markings WHERE dataset = ? AND inherited = ? "
                "ORDER BY marking",
                (dataset, inherited),
            ).fetchall()
        return [r["marking"] for r in rows]

    def get_explicit_markings(self, dataset: str) -> list[str]:
        return self._markings_for(dataset, 0)

    def get_effective_markings(self, dataset: str) -> list[str]:
        return self._markings_for(dataset, 1)

    def set_explicit_markings(self, dataset: str, markings: list[str]) -> None:
        markings = sorted({m.lower() for m in markings})
        with self._conn() as c:
            c.execute(
                "DELETE FROM dataset_markings WHERE dataset = ? AND inherited = 0", (dataset,)
            )
            c.executemany(
                "INSERT INTO dataset_markings (dataset, marking, inherited) VALUES (?, ?, 0)",
                [(dataset, m) for m in markings],
            )

    def _set_effective_markings(self, c: Connection, dataset: str, markings: set[str]) -> None:
        c.execute("DELETE FROM dataset_markings WHERE dataset = ? AND inherited = 1", (dataset,))
        c.executemany(
            "INSERT INTO dataset_markings (dataset, marking, inherited) VALUES (?, ?, 1)",
            [(dataset, m) for m in sorted(markings)],
        )

    def get_clearances(self, username: str) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT marking FROM clearances WHERE username = ? ORDER BY marking",
                (username.lower(),),
            ).fetchall()
        return [r["marking"] for r in rows]

    def set_clearances(self, username: str, markings: list[str]) -> None:
        username = username.lower()
        markings = sorted({m.lower() for m in markings})
        with self._conn() as c:
            c.execute("DELETE FROM clearances WHERE username = ?", (username,))
            c.executemany(
                "INSERT INTO clearances (username, marking) VALUES (?, ?)",
                [(username, m) for m in markings],
            )

    def recompute_all_markings(self) -> None:
        """Recompute every dataset's *effective* markings as its explicit markings
        plus the union of its lineage upstreams' effective markings. This is the
        propagation: a derived dataset inherits its inputs' classifications, so
        classified data can't be laundered through a transform. Runs a single
        topological pass over the dataset-level lineage graph (cycle-safe)."""
        datasets = [d.name for d in self.list_datasets()]
        # dataset-level edges: upstream_dataset -> downstream_dataset
        upstreams: dict[str, set[str]] = {d: set() for d in datasets}
        for e in self.list_lineage():
            upstreams.setdefault(e.downstream_dataset, set()).add(e.upstream_dataset)
            upstreams.setdefault(e.upstream_dataset, set())
        explicit = {d: set(self.get_explicit_markings(d)) for d in upstreams}
        # Topological order (Kahn); nodes in a cycle are processed last, best-effort.
        indeg = {d: len(ups) for d, ups in upstreams.items()}
        ready = [d for d, n in indeg.items() if n == 0]
        downstream: dict[str, set[str]] = {d: set() for d in upstreams}
        for d, ups in upstreams.items():
            for u in ups:
                downstream[u].add(d)
        order: list[str] = []
        while ready:
            d = ready.pop()
            order.append(d)
            for c in downstream[d]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        order += [d for d in upstreams if d not in order]  # any cycle leftovers
        effective: dict[str, set[str]] = {}
        for d in order:
            eff = set(explicit.get(d, set()))
            for u in upstreams.get(d, set()):
                eff |= effective.get(u, set())
            effective[d] = eff
        with self._conn() as c:
            for d, eff in effective.items():
                self._set_effective_markings(c, d, eff)

    def set_dataset_policy(self, dataset: str, policy: Optional[dict]) -> None:
        with self._conn() as c:
            if policy is None:
                c.execute("DELETE FROM dataset_policies WHERE dataset = ?", (dataset,))
            else:
                c.execute(
                    """INSERT INTO dataset_policies (dataset, policy_json, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT (dataset) DO UPDATE SET
                         policy_json = excluded.policy_json,
                         updated_at = excluded.updated_at""",
                    (dataset, json.dumps(policy), utcnow_iso()),
                )
