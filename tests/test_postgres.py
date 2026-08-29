"""Integration test: the multi-workspace control plane on a real PostgreSQL.

Runs only when LAURELIN_TEST_POSTGRES is set to a postgresql:// URL (e.g. a
docker/podman Postgres). Everything else runs on SQLite.
"""

import os

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_server_app
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.db import MetadataStore
from laurelin.core.models import EditKind, ObjectEdit

_TICKET = _ChangeTicket(kind="local", actor="test")

PG_URL = os.environ.get("LAURELIN_TEST_POSTGRES")


def _reset_pg():
    import psycopg

    with psycopg.connect(PG_URL) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.commit()

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="set LAURELIN_TEST_POSTGRES to a postgresql:// URL to run"
)


@pytest.fixture()
def app(tmp_path):
    _reset_pg()
    return create_server_app(tmp_path / "root", control_url=PG_URL)


@pytest.fixture()
def store():
    _reset_pg()
    return MetadataStore(PG_URL)


def test_store_paths_that_differ_by_dialect_on_postgres(store):

    # upsert_dataset — was broken on PG (ambiguous 'description' in ON CONFLICT)
    store.upsert_dataset("ds1", "first")
    store.upsert_dataset("ds1", "")  # empty must not clobber
    assert store.get_dataset("ds1").description == "first"
    store.upsert_dataset("ds1", "second")
    assert store.get_dataset("ds1").description == "second"

    # builds ORDER BY (was ORDER BY rowid — no rowid on PG)
    b1 = store.create_build(["a"])
    b2 = store.create_build(["b"])
    builds = store.list_builds()
    assert [b.id for b in builds] == [b2.id, b1.id]  # newest first

    # object_edits insertion order (was ORDER BY rowid)
    for i, kind in enumerate([EditKind.create, EditKind.update, EditKind.delete]):
        store.add_object_edit(
            ObjectEdit(id=f"e{i}", object_type="widget", pk_value="1",
                       kind=kind, payload={"id": "1"}, created_at="2026-01-01T00:00:00+00:00")
        )
    kinds = [e.kind for e in store.list_object_edits("widget")]
    assert kinds == [EditKind.create, EditKind.update, EditKind.delete]

    # audit ordering + IDENTITY id
    store.log_audit("first_action")
    store.log_audit("second_action")
    assert store.list_audit(1)[0].action == "second_action"


def _edit(edit_id: str) -> ObjectEdit:
    return ObjectEdit(id=edit_id, object_type="widget", pk_value="1",
                      kind=EditKind.update, payload={},
                      created_at="2026-01-01T00:00:00+00:00")


def test_the_edit_watermark_survives_postgres_identity_visibility(store):
    """The reason ``edit_seq`` exists instead of reusing the ordering column.

    Postgres allocates an identity value at INSERT and makes it visible at
    COMMIT, so a transaction holding seq=5 can commit *after* seq=6. A cursor
    parked at "max seq I have seen" would then skip 5 permanently — the edit is
    in the log, and no catch-up ever finds it.

    ``edit_seq`` is allocated as MAX+1 inside the appending transaction and made
    unique by an index, which serializes the two writers and leaves no gap. Here
    T1 opens first and commits last, exactly the interleaving that breaks a
    naive watermark.
    """
    import threading

    import psycopg

    store.add_object_edit(_edit("edit-0"))  # seq 1

    # An open transaction holding seq 2, uncommitted and therefore invisible.
    holder = psycopg.connect(PG_URL)
    holder.autocommit = False
    with holder.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(edit_seq), 0) + 1 FROM object_edits "
            "WHERE object_type = %s", ("widget",),
        )
        assert cur.fetchone()[0] == 2
        cur.execute(
            "INSERT INTO object_edits (id, object_type, pk_value, kind,"
            " payload_json, actor, created_at, edit_seq)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ("edit-holder", "widget", "1", "update", "{}", "t",
             "2026-01-01T00:00:00+00:00", 2),
        )

    # A second writer reads MAX+1 and computes 2 as well, because the holder's
    # row is not visible. This is the exact interleaving that would corrupt a
    # naive watermark.
    result: dict = {}

    def second_writer():
        try:
            result["seq"] = store.add_object_edit(_edit("edit-second"))
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=second_writer, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert thread.is_alive(), (
        "the UNIQUE index must make the second writer wait rather than let two "
        "edits take the same position"
    )

    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in result, (
        f"losing the race must retry, not fail the user's write: {result.get('error')!r}"
    )
    assert result["seq"] == 3, "it re-read MAX+1 and took the next position"

    seqs = sorted(e.edit_seq for e in store.list_object_edits("widget"))
    assert seqs == [1, 2, 3], "gapless, so nothing above a watermark can be skipped"
    # A catch-up cursor parked at 2 finds 3 — and, crucially, one parked at 1
    # still finds 2, which is the edit a naive "max seq I have seen" would lose.
    assert [e.edit_seq for e in store.list_object_edits_since("widget", 1)] == [2, 3]


def test_the_edit_log_accounts_and_prunes_on_postgres(store):
    """Four bits of dialect-sensitive SQL in one path: a CASE-WHEN aggregate,
    ``length()`` over the payload, a chunked ``IN`` list, and a DELETE whose
    ``rowcount`` is the answer returned to the caller.

    The two refusals are re-asserted here rather than trusted from the SQLite
    suite, because they are enforced *in the statement*: a dialect that silently
    dropped either predicate would delete a live edit, or the row the sequence
    allocator reads, and nothing above this layer would notice.
    """
    for i in range(3):
        store.add_object_edit(_edit(f"e{i}"))
    assert store.mark_edits_folded(["e0", "e1"], 7) == 2

    stats = store.object_edit_stats("widget")[0]
    assert (stats["edits"], stats["live"], stats["folded"]) == (3, 1, 2)
    assert stats["payload_bytes"] > 0 and stats["max_edit_seq"] == 3

    folded = store.list_folded_edits("widget")
    assert [e["edit_seq"] for e in folded] == [1, 2]
    assert all(e["folded_into_version"] == 7 for e in folded)

    assert store.delete_object_edits("widget", ["e2"]) == 0, "e2 is live"
    assert store.delete_object_edits("widget", ["e0", "e1"]) == 2
    assert store.max_edit_seq("widget") == 3, "the allocator's floor is untouched"

    store.mark_edits_folded(["e2"], 8)
    assert store.delete_object_edits("widget", ["e2"]) == 0, (
        "folded, but it is the highest position — deleting it would let the "
        "next edit re-use a number a materialization claims to have applied"
    )


ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
actions:
  - api_name: found_city
    object_type: city
    kind: create
    parameters:
      name: {type: string, required: true}
      realm: {type: string, required: true}
"""


def test_creating_an_object_does_not_overflow_the_ordinal_column(store, tmp_path):
    """``ord`` must be 64-bit, and only PostgreSQL can tell you it is not.

    ``INTEGER`` is 64-bit on SQLite and 32-bit here, and a created object sorts
    at ``ORD_CREATED_BASE + edit_seq = 2**62 + n``. So on the default
    production control plane the very first object create overflowed the
    column: the INSERT raised inside the write path, the write path swallowed
    it and fell back to a log-only append, the user was told the write
    succeeded, and the materialization was permanently behind from then on —
    reads silently reverting to the full scan this whole feature exists to
    remove. The advertised repair, a rebuild, hit the same overflow and 500ed.

    Nothing caught it because no test created an ontology object against
    Postgres. This one does.
    """
    import pyarrow as pa

    from laurelin.catalog import DatasetCatalog
    from laurelin.core.config import Workspace
    from laurelin.ontology import OntologyService, load_ontology
    from laurelin.ontology.store import ORD_CREATED_BASE

    ws = Workspace.init(tmp_path / "pgord", name="pgord")
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", pa.table({"name": ["c0"], "realm": ["valinor"]}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    svc = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))

    assert svc.reindex("city") == 1
    ot = svc.ontology.object_type("city")
    svc.apply_action("found_city", pk=None, parameters={"name": "z0", "realm": "new"})

    assert svc.store_is_caught_up(ot), "one create must not kill the materialization"
    row = svc.object_store.rows_for("city", ["z0"])["z0"]
    assert int(row["ord"]) == ORD_CREATED_BASE + 1
    assert svc.reindex("city") == 2, "and a rebuild must not raise either"
    assert svc.verify_digest(ot)


def test_group_case_insensitivity_on_postgres(store):
    # groups were stored verbatim but read lowercased -> broken on PG
    store.create_group("Eng", "2026-01-01T00:00:00+00:00")
    assert store.group_exists("eng") is True
    assert store.group_exists("ENG") is True
    store.create_user(_user("alice"), "hash")
    store.set_group_members("ENG", ["Alice"], ticket=_TICKET)
    assert store.groups_for_user("ALICE") == {"eng"}


def _user(username):
    from laurelin.core.models import Role, User

    return User(id=username, username=username, role=Role.viewer)


ROOT = {"username": "root", "password": "trustno1!"}


def test_multiworkspace_flow_on_postgres(app):
    admin = TestClient(app)
    # setup superadmin + login
    assert admin.post("/api/v1/auth/setup", json=ROOT).status_code == 200
    assert admin.post("/api/v1/auth/login", json=ROOT).status_code == 200
    assert admin.get("/api/v1/auth/me").json()["superadmin"] is True

    # workspace CRUD (registry in Postgres)
    assert admin.post("/api/v1/workspaces", json={"slug": "alpha", "name": "Alpha"}).status_code == 200
    assert admin.post("/api/v1/workspaces", json={"slug": "beta"}).status_code == 200
    assert admin.post("/api/v1/workspaces", json={"slug": "alpha"}).status_code == 409

    # global user + per-workspace membership
    assert admin.post(
        "/api/v1/users", json={"username": "ed", "password": "password123", "role": "viewer"}
    ).status_code == 200
    assert admin.put("/api/v1/workspaces/alpha/members", json={"username": "ed", "role": "editor"}).status_code == 200

    ed = TestClient(app)
    assert ed.post("/api/v1/auth/login", json={"username": "ed", "password": "password123"}).status_code == 200
    assert [(w["slug"], w["role"]) for w in ed.get("/api/v1/auth/me").json()["workspaces"]] == [("alpha", "editor")]

    # isolation: ed is editor in alpha, denied beta
    A, B = {"X-Laurelin-Workspace": "alpha"}, {"X-Laurelin-Workspace": "beta"}
    assert ed.get("/api/v1/datasets", headers=A).status_code == 200
    assert ed.post("/api/v1/datasets", json={"name": "d1"}, headers=A).status_code == 200
    assert ed.get("/api/v1/datasets", headers=B).status_code == 403

    # server-level ops remain superadmin-only
    assert ed.get("/api/v1/workspaces").status_code == 403
    assert ed.get("/api/v1/users").status_code == 403

    # case-insensitive identity (Postgres has no COLLATE NOCASE)
    assert admin.post("/api/v1/auth/login", json={"username": "ED", "password": "password123"}).status_code == 200


# -- DDL statement splitting --------------------------------------------------
#
# These need no server: they exercise the pure function that only the PostgreSQL
# path uses. SQLite hands the script to native executescript and never reaches
# it, which is exactly why a bug here stayed invisible until a Postgres run.

@pytest.mark.parametrize("name, script, expected", [
    ("two statements", "CREATE TABLE a (x INT); CREATE TABLE b (y INT);", 2),
    # The regression. A semicolon inside a comment cut the enclosing CREATE
    # TABLE in half; psycopg raised "syntax error at end of input" and a
    # Postgres deployment could not create its schema at all.
    ("semicolon inside a comment",
     "CREATE TABLE a (\n  -- tracks the data; not the definition\n  x INT\n);", 1),
    ("semicolon inside a string literal",
     "CREATE TABLE a (x TEXT DEFAULT 'a;b');", 1),
    ("escaped quote inside a literal",
     "CREATE TABLE a (x TEXT DEFAULT 'it''s; fine');", 1),
    ("no trailing semicolon", "CREATE TABLE a (x INT)", 1),
])
def test_ddl_splitting_ignores_semicolons_that_are_not_separators(name, script, expected):
    from laurelin.core.backend import _split_statements

    assert len(_split_statements(script)) == expected, name


def test_the_real_schema_splits_into_runnable_statements():
    """The shipped schema, not a toy string.

    Every statement must be non-empty and look like DDL — a split that lands
    mid-statement produces a fragment starting with a column name or a comment,
    which is what the comment-semicolon bug did.
    """
    from laurelin.core.backend import SQLiteBackend, _split_statements
    from laurelin.core.db import _SCHEMA

    rendered = SQLiteBackend(":memory:").render_schema(_SCHEMA)
    statements = _split_statements(rendered)
    assert statements, "the schema produced no statements"
    for stmt in statements:
        head = "\n".join(
            line for line in stmt.splitlines() if line.strip() and not line.strip().startswith("--")
        ).lstrip().upper()
        assert head.startswith(("CREATE", "INSERT", "ALTER", "DROP")), (
            f"statement does not start with DDL — split landed mid-statement:\n{stmt[:200]}"
        )
