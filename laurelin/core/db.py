"""MetadataStore: SQLite-backed catalog of versions, builds, lineage, edits, audit.

Deliberately plain SQL over a single file so users can inspect everything with
any sqlite client. Each operation opens a short-lived connection (WAL mode),
which keeps the store safe across threads.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from laurelin.core.models import (
    AuditEvent,
    BuildInfo,
    BuildStatus,
    BuildTaskInfo,
    ColumnSchema,
    DatasetInfo,
    DatasetVersionInfo,
    EditKind,
    LineageEdge,
    ObjectEdit,
    utcnow_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    schema_json TEXT NOT NULL DEFAULT '[]',
    path TEXT NOT NULL DEFAULT '',
    build_id TEXT,
    source TEXT NOT NULL DEFAULT 'upload',
    PRIMARY KEY (dataset, version)
);
CREATE TABLE IF NOT EXISTS builds (
    id TEXT PRIMARY KEY,
    targets_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
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
    PRIMARY KEY (build_id, transform_name)
);
CREATE TABLE IF NOT EXISTS lineage_edges (
    upstream_dataset TEXT NOT NULL,
    downstream_dataset TEXT NOT NULL,
    transform_name TEXT NOT NULL,
    PRIMARY KEY (upstream_dataset, downstream_dataset, transform_name)
);
CREATE TABLE IF NOT EXISTS object_edits (
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'anonymous',
    action TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
"""


class MetadataStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._ensure_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
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
            c.executescript(_SCHEMA)

    # -- datasets -------------------------------------------------------------

    def upsert_dataset(self, name: str, description: str = "") -> DatasetInfo:
        with self._conn() as c:
            row = c.execute("SELECT * FROM datasets WHERE name = ?", (name,)).fetchone()
            if row is None:
                created = utcnow_iso()
                c.execute(
                    "INSERT INTO datasets (name, description, created_at) VALUES (?, ?, ?)",
                    (name, description, created),
                )
            elif description and description != row["description"]:
                c.execute("UPDATE datasets SET description = ? WHERE name = ?", (description, name))
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
                   (dataset, version, created_at, row_count, schema_json, path, build_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    info.dataset,
                    info.version,
                    info.created_at,
                    info.row_count,
                    json.dumps([s.model_dump() for s in info.schema_]),
                    info.path,
                    info.build_id,
                    info.source,
                ),
            )

    def _row_to_version(self, row: sqlite3.Row) -> DatasetVersionInfo:
        return DatasetVersionInfo(
            dataset=row["dataset"],
            version=row["version"],
            created_at=row["created_at"],
            row_count=row["row_count"],
            schema=[ColumnSchema(**s) for s in json.loads(row["schema_json"])],
            path=row["path"],
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
                    finished_at, error, rows_written, output_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (build_id, transform_name) DO UPDATE SET
                     output_dataset = excluded.output_dataset,
                     status = excluded.status,
                     started_at = excluded.started_at,
                     finished_at = excluded.finished_at,
                     error = excluded.error,
                     rows_written = excluded.rows_written,
                     output_version = excluded.output_version""",
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
                )
                for t in task_rows
            ],
        )

    def list_builds(self, limit: int = 50) -> list[BuildInfo]:
        with self._conn() as c:
            ids = [
                r["id"]
                for r in c.execute("SELECT id FROM builds ORDER BY rowid DESC LIMIT ?", (limit,))
            ]
        return [b for i in ids if (b := self.get_build(i)) is not None]

    # -- lineage ------------------------------------------------------------------

    def replace_lineage_for_transform(self, transform_name: str, edges: list[LineageEdge]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM lineage_edges WHERE transform_name = ?", (transform_name,))
            c.executemany(
                "INSERT OR IGNORE INTO lineage_edges VALUES (?, ?, ?)",
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
                # rowid = insertion order; created_at has second-level collisions
                # and id is a random uuid, so neither gives a stable replay order.
                "SELECT * FROM object_edits WHERE object_type = ? ORDER BY rowid",
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
