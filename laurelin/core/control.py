"""ControlStore: the multi-workspace control plane (``<root>/control.db``).

In multi-workspace mode identity is global — users, sessions, and API tokens
live here, not in any single workspace — and this store additionally holds the
workspace registry and per-workspace membership. It reuses ``MetadataStore`` for
the auth tables (the inherited dataset/ontology tables simply stay empty in the
control DB); each workspace keeps its own ``metadata.db`` for data + per-
workspace ACLs/groups.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, WorkspaceInfo, utcnow_iso

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")

_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_members (
    slug TEXT NOT NULL,
    username TEXT NOT NULL {{NOCASE}},
    role TEXT NOT NULL CHECK(role IN ('viewer','editor','admin')),
    PRIMARY KEY (slug, username)
);
CREATE INDEX IF NOT EXISTS idx_members_user ON workspace_members (username);
"""


def validate_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid workspace slug {slug!r}: 2-48 chars, lowercase letters, "
            "digits, '-' and '_' (must start with a letter or digit)"
        )
    return slug


class ControlStore(MetadataStore):
    def _ensure_schema(self) -> None:
        super()._ensure_schema()
        with self._conn() as c:
            # {{NOCASE}} in the control schema is substituted; {{EXTRA_DDL}} and
            # {{AUTOINC_PK}} don't appear here, so render() just strips {{NOCASE}}.
            c.executescript(
                self.backend.render_schema(_CONTROL_SCHEMA).replace("{{EXTRA_DDL}}", "")
            )

    # -- workspaces -----------------------------------------------------------

    def create_workspace(self, slug: str, name: str, description: str = "") -> WorkspaceInfo:
        info = WorkspaceInfo(slug=slug, name=name or slug, description=description)
        with self._conn() as c:
            c.execute(
                "INSERT INTO workspaces (slug, name, description, created_at) "
                "VALUES (?, ?, ?, ?)",
                (info.slug, info.name, info.description, info.created_at),
            )
        return info

    def get_workspace(self, slug: str) -> Optional[WorkspaceInfo]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
        return self._row_to_ws(row) if row else None

    @staticmethod
    def _row_to_ws(row: sqlite3.Row) -> WorkspaceInfo:
        return WorkspaceInfo(
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            created_at=row["created_at"],
        )

    def list_workspaces(self) -> list[WorkspaceInfo]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM workspaces ORDER BY slug").fetchall()
        return [self._row_to_ws(r) for r in rows]

    def update_workspace(
        self, slug: str, *, name: Optional[str] = None, description: Optional[str] = None
    ) -> None:
        sets, vals = [], []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if description is not None:
            sets.append("description = ?")
            vals.append(description)
        if not sets:
            return
        with self._conn() as c:
            c.execute(f"UPDATE workspaces SET {', '.join(sets)} WHERE slug = ?", (*vals, slug))

    def delete_workspace(self, slug: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM workspace_members WHERE slug = ?", (slug,))
            c.execute("DELETE FROM workspaces WHERE slug = ?", (slug,))

    # -- membership -----------------------------------------------------------

    def set_member(self, slug: str, username: str, role: Role | str) -> None:
        role = Role(role)
        with self._conn() as c:
            c.execute(
                """INSERT INTO workspace_members (slug, username, role) VALUES (?, ?, ?)
                   ON CONFLICT (slug, username) DO UPDATE SET role = excluded.role""",
                (slug, username.lower(), role.value),
            )

    def remove_member(self, slug: str, username: str) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM workspace_members WHERE slug = ? AND username = ?",
                (slug, username.lower()),
            )

    def member_role(self, slug: str, username: str) -> Optional[Role]:
        with self._conn() as c:
            row = c.execute(
                "SELECT role FROM workspace_members WHERE slug = ? AND username = ?",
                (slug, username.lower()),
            ).fetchone()
        return Role(row["role"]) if row else None

    def list_members(self, slug: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT username, role FROM workspace_members WHERE slug = ? ORDER BY username",
                (slug,),
            ).fetchall()
        return [{"username": r["username"], "role": r["role"]} for r in rows]

    def workspaces_for_user(self, username: str) -> list[dict]:
        """Workspaces a user belongs to, with their role in each."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT w.slug, w.name, w.description, m.role
                   FROM workspace_members m JOIN workspaces w ON w.slug = m.slug
                   WHERE m.username = ? ORDER BY w.slug""",
                (username.lower(),),
            ).fetchall()
        return [
            {"slug": r["slug"], "name": r["name"], "description": r["description"], "role": r["role"]}
            for r in rows
        ]

    def remove_member_everywhere(self, username: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM workspace_members WHERE username = ?", (username.lower(),))

    def delete_user(self, username: str) -> None:
        # Also drop the deleted user's workspace memberships (global identity).
        self.remove_member_everywhere(username)
        super().delete_user(username)
