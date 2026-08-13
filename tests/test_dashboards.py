"""Tests for dashboards: CRUD, validation, permissions, and R2.

The invariant this file has always carried is that a dashboard grants nobody any
new read access. That is still true and is still tested here — but it is true for
a **new reason**. It used to hold because the *client* executed a panel through
``POST /query`` with the viewer's own credentials. It now holds because the
*server* executes the stored panel through
``POST /dashboards/{name}/panels/{id}/run``, as the caller, down the same
``_execute_sql`` path with the same ACL / row-level security / column masking.

The viewer never receives the SQL. They receive the rows.
"""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import DashboardPanel

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="dash")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write(
        "sales",
        pa.table({"region": ["us", "us", "eu"], "amount": [10.0, 5.0, 7.0]}),
    )
    return ws


@pytest.fixture()
def clients(ws):
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={"username": "vic", "password": "password123", "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return admin, viewer


def _client(app, username, password="password123"):
    c = TestClient(app)
    assert c.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    ).status_code == 200
    return c


PANEL = {
    "id": "p1",
    "title": "Revenue by region",
    "sql": "SELECT region, sum(amount) AS total FROM sales GROUP BY region ORDER BY region",
    "chart": "bar",
    "x": "region",
    "y": ["total"],
    "width": 6,
}


def test_dashboard_crud_roundtrip(clients):
    admin, viewer = clients
    r = admin.put(
        "/api/v1/dashboards/revenue",
        json={"title": "Revenue", "description": "By region", "panels": [PANEL]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["panels"][0]["chart"] == "bar"

    # Viewers can read dashboards — the layout, not the queries.
    listed = viewer.get("/api/v1/dashboards").json()
    assert [d["name"] for d in listed] == ["revenue"]
    dash = viewer.get("/api/v1/dashboards/revenue").json()
    assert dash["title"] == "Revenue"
    assert dash["panels"][0]["title"] == "Revenue by region"

    # Update preserves created_at, bumps updated_at.
    created = dash["created_at"]
    r = admin.put("/api/v1/dashboards/revenue", json={"title": "Revenue v2", "panels": []})
    assert r.status_code == 200
    assert r.json()["created_at"] == created
    assert r.json()["title"] == "Revenue v2"

    assert admin.delete("/api/v1/dashboards/revenue").status_code == 200
    assert viewer.get("/api/v1/dashboards/revenue").status_code == 404


def test_a_viewer_sees_panel_rows_for_sql_they_never_received(clients):
    """The non-negotiable constraint, in one test.

    A viewer must still see a dashboard AND its data. If this passes while the
    viewer's board is an empty box, a security bug has been traded for a broken
    product; if it passes because ``sql`` came back, R2 is not enforced. It
    asserts both halves.

    It is also the test that fails if somebody "solves" R2 by shipping
    ``sql: ""`` and leaving the client to POST it: the rows come from the route,
    not from a query the client composed.
    """
    admin, viewer = clients
    assert admin.put(
        "/api/v1/dashboards/revenue", json={"title": "Revenue", "panels": [PANEL]}
    ).status_code == 200

    dash = viewer.get("/api/v1/dashboards/revenue").json()
    panel = dash["panels"][0]
    assert "sql" not in panel, "a viewer received the panel's query text"

    r = viewer.post(
        "/api/v1/dashboards/revenue/panels/p1/run", json={"max_rows": 1000}
    )
    assert r.status_code == 200, r.text
    assert {row["region"]: row["total"] for row in r.json()["rows"]} == {
        "eu": 7.0, "us": 15.0
    }


def test_two_viewers_get_different_rows_from_the_same_panel(ws):
    """Server-side execution runs as the CALLER, not as the panel's author.

    This carries forward the invariant the old client-side execution gave for
    free. Moving the query onto the server is exactly the change that could have
    lost it — a stored panel that ran with the author's privileges would be a
    privilege-escalation primitive shaped like a chart.
    """
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    for name in ("eve", "uma"):
        assert admin.post(
            "/api/v1/users",
            json={"username": name, "password": "password123", "role": "viewer"},
        ).status_code in (200, 201)

    # One row policy, two users, two different visible row sets.
    assert admin.put(
        "/api/v1/datasets/sales/policy",
        json={
            "row_policy": {
                "column": "region",
                "rules": [
                    {"subject_kind": "user", "subject": "eve", "values": ["eu"]},
                    {"subject_kind": "user", "subject": "uma", "values": ["us"]},
                ],
            },
            "column_masks": [],
        },
    ).status_code == 200
    assert admin.put(
        "/api/v1/dashboards/revenue", json={"title": "Revenue", "panels": [PANEL]}
    ).status_code == 200

    def rows_for(username):
        c = _client(app, username)
        r = c.post("/api/v1/dashboards/revenue/panels/p1/run", json={})
        assert r.status_code == 200, r.text
        return {row["region"]: row["total"] for row in r.json()["rows"]}

    assert rows_for("eve") == {"eu": 7.0}
    assert rows_for("uma") == {"us": 15.0}


def test_an_editor_still_receives_panel_sql_so_the_authoring_textarea_round_trips(clients):
    """R2 withholds from readers who cannot write. An editor can write."""
    admin, _viewer = clients
    assert admin.put(
        "/api/v1/dashboards/revenue", json={"title": "Revenue", "panels": [PANEL]}
    ).status_code == 200
    panel = admin.get("/api/v1/dashboards/revenue").json()["panels"][0]
    assert panel["sql"] == PANEL["sql"]


def test_saving_one_panel_cannot_blank_another_panels_query(clients):
    """The read-modify-write trap.

    Both dashboard editors re-PUT *every* panel from a fetched record. If any
    principal ever holds a trimmed panel — a demoted editor, a stale tab, a
    script written against the viewer projection — saving one panel would blank
    the SQL of every other. Two layers defend it; this exercises the one that
    survives an old client.
    """
    admin, _viewer = clients
    two = [PANEL, {**PANEL, "id": "p2", "title": "Second", "sql": "SELECT 42 AS n"}]
    assert admin.put(
        "/api/v1/dashboards/revenue", json={"title": "Revenue", "panels": two}
    ).status_code == 200

    # A client PUTs the board back with p2 edited and p1's sql missing
    # **entirely** — which is what a round-tripped projection actually looks
    # like, because `serialize._projection` omits operational keys rather than
    # blanking them. Absence inherits; an explicitly-sent `""` does not, and
    # `test_a_whole_board_put_can_clear_an_operational_field_it_sends` is the
    # other half of that rule. Sending `"sql": ""` here made this test pass
    # while making a legitimate edit impossible: an editor could not clear a
    # field, and converting a SQL panel to an object panel returned a 400 that
    # blamed them for text the server had just re-inserted.
    stripped = [
        {"id": "p1", "title": "Revenue by region", "chart": "bar"},
        {**two[1], "title": "Second v2"},
    ]
    r = admin.put(
        "/api/v1/dashboards/revenue", json={"title": "Revenue", "panels": stripped}
    )
    assert r.status_code == 200, r.text
    panels = {p["id"]: p for p in admin.get("/api/v1/dashboards/revenue").json()["panels"]}
    assert panels["p1"]["sql"] == PANEL["sql"], "p1's query was blanked by p2's save"
    assert panels["p2"]["title"] == "Second v2"


def test_a_panel_with_no_title_still_has_a_label_a_viewer_can_read(clients):
    """Without this the viewer's board is a grid of unlabeled boxes.

    The old UI fell back to ``p.sql.slice(0, 48)`` for a label and the workbench
    ships an empty title by default, so unlabeled panels are the norm. The label
    is filled in at WRITE time, so ``title`` is simply always present.
    """
    admin, viewer = clients
    untitled = {k: v for k, v in PANEL.items() if k != "title"}
    assert admin.put(
        "/api/v1/dashboards/revenue",
        json={"title": "Revenue", "panels": [untitled, {**untitled, "id": "p2"}]},
    ).status_code == 200
    panels = viewer.get("/api/v1/dashboards/revenue").json()["panels"]
    assert [p["title"] for p in panels] == ["Panel 1", "Panel 2"]


def test_a_panel_error_does_not_echo_the_query_back_to_the_viewer(clients):
    """Measured before this change: a viewer's 400 body carried the sentinel and
    DuckDB's full ``LINE 1:`` echo of the statement. Withholding ``sql`` on the
    read path while the error path hands it back is not withholding it."""
    admin, viewer = clients
    broken = {
        **PANEL,
        "sql": "SELECT no_such_column_S3KRET FROM sales",
    }
    assert admin.put(
        "/api/v1/dashboards/revenue", json={"title": "Revenue", "panels": [broken]}
    ).status_code == 200
    r = viewer.post("/api/v1/dashboards/revenue/panels/p1/run", json={})
    assert r.status_code == 400
    assert "S3KRET" not in r.text
    assert "LINE 1" not in r.text


# Seven shapes measured to defeat the free-text matcher on this tree, plus five
# more the UI investigation measured passing it. **This test must pass with the
# matcher deleted** — that is its whole purpose. Do NOT read the list as a bug
# list: widening `_KEYWORD_SECRET_RE` to survive a `'` after `password=` is four
# characters and is round four of the exact mistake this task exists to end.
_BYPASSES = [
    "SELECT * FROM postgres_scan('host=db user=alice ' || 'password=SEKRET1', 'p', 't')",
    "SELECT * FROM postgres_scan('host=db password=' || chr(83) || 'EKRET2', 'p', 't')",
    "SELECT * FROM postgres_scan('host=db user=alice password=''SEKRET3''', 'p', 't')",
    "SET s3_secret_access_key='SEKRET4'; SELECT 1",
    "-- Authorization: Bearer SEKRET5\nSELECT 1",
    "SELECT 1 -- -----BEGIN RSA PRIVATE KEY-----\\nSEKRET6",
    "SELECT 1 /* AKIAIOSFODNN7EXAMPLE SEKRET7 */",
    "SELECT 1 /* DefaultEndpointsProtocol=https;AccountKey=SEKRET8; */",
    "SELECT 1 /* jdbc:postgresql://db:5432/prod,alice,SEKRET9 */",
    'SELECT 1 /* {"type":"service_account","private_key":"SEKRET10"} */',
    "SELECT 1 /* mysql -u alice -p SEKRET11 */",
    "SELECT * FROM read_csv('s3://b/k?X-Amz-Signature=SEKRET12')",
]


@pytest.mark.parametrize("sql", _BYPASSES, ids=range(len(_BYPASSES)))
def test_a_viewer_cannot_read_panel_sql_whatever_it_contains(clients, sql):
    """Every one of these saves with 200 and is unreadable by a viewer.

    Saving with 200 is the point: the write gate is an authoring *hint* now, and
    a hint that is wrong costs an editor a yellow banner rather than costing a
    viewer a password. Confidentiality comes from the audience annotation on
    ``DashboardPanel.sql``, which does not read the string at all.
    """
    admin, viewer = clients
    r = admin.put(
        "/api/v1/dashboards/d",
        json={"title": "d", "panels": [{"id": "p1", "title": "t", "sql": sql}]},
    )
    assert r.status_code == 200, r.text
    body = viewer.get("/api/v1/dashboards/d").text
    assert "SEKRET" not in body, body
    assert viewer.get("/api/v1/dashboards").text.count("SEKRET") == 0


def test_a_credential_shaped_panel_saves_with_a_warning_instead_of_a_refusal(clients):
    """The authoring hint, demoted. It advises; it does not block."""
    admin, _ = clients
    r = admin.put(
        "/api/v1/dashboards/d",
        json={"title": "d", "panels": [{
            "id": "p1", "title": "t",
            "sql": "SELECT * FROM postgres_scan('postgresql://a:pw@db/prod', 'p', 't')",
        }]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["warnings"], "the hint should still fire for an obvious DSN"
    assert r.json()["warnings"][0]["field"] == "panels[0].sql"


def test_dashboard_permissions_and_validation(clients):
    admin, viewer = clients
    # Viewers cannot write or delete.
    assert viewer.put("/api/v1/dashboards/x", json={"panels": []}).status_code == 403
    assert viewer.delete("/api/v1/dashboards/x").status_code == 403

    # Bad names and bad panel payloads are 400.
    assert admin.put("/api/v1/dashboards/Bad Name", json={"panels": []}).status_code == 400
    r = admin.put(
        "/api/v1/dashboards/x",
        json={"panels": [{"id": "p", "sql": "SELECT 1", "chart": "pie3d"}]},
    )
    assert r.status_code == 400
    r = admin.put(
        "/api/v1/dashboards/x",
        json={"panels": [{"id": "p", "sql": "SELECT 1", "width": 13}]},
    )
    assert r.status_code == 400
    assert admin.delete("/api/v1/dashboards/nope").status_code == 404


def test_per_panel_routes_edit_one_panel_without_touching_the_others(clients):
    """The real fix for read-modify-write: a client that only sends the panel it
    changed cannot blank the panel it did not."""
    admin, viewer = clients
    assert admin.put(
        "/api/v1/dashboards/d", json={"title": "d", "panels": [PANEL]}
    ).status_code == 200

    added = admin.post(
        "/api/v1/dashboards/d/panels",
        json={"id": "p2", "title": "Second", "sql": "SELECT 42 AS n"},
    )
    assert added.status_code == 200, added.text
    assert [p["id"] for p in added.json()["panels"]] == ["p1", "p2"]
    # A duplicate id is a conflict, not a silent overwrite.
    assert admin.post(
        "/api/v1/dashboards/d/panels", json={"id": "p2", "sql": "SELECT 1"}
    ).status_code == 409

    updated = admin.put(
        "/api/v1/dashboards/d/panels/p2",
        json={"id": "ignored", "title": "Second v2", "sql": "SELECT 43 AS n"},
    )
    assert updated.status_code == 200
    panels = {p["id"]: p for p in updated.json()["panels"]}
    assert panels["p1"]["sql"] == PANEL["sql"]
    assert panels["p2"]["title"] == "Second v2"

    assert admin.delete("/api/v1/dashboards/d/panels/p1").status_code == 200
    assert [p["id"] for p in admin.get("/api/v1/dashboards/d").json()["panels"]] == ["p2"]
    assert viewer.put("/api/v1/dashboards/d/panels/p2", json={"id": "p2", "sql": "x"}).status_code == 403
    assert admin.delete("/api/v1/dashboards/d/panels/nope").status_code == 404


# -- object-backed panels -----------------------------------------------------
#
# A panel charting the *backing dataset* with SQL misses the ontology's edit
# overlay: it answers from rows an action has already changed, and nothing in
# the chart says it disagrees with the object list beside it. An object panel
# goes through /aggregate instead, which sees the overlay.

def test_a_panel_needs_exactly_one_source():
    with pytest.raises(ValidationError, match="either sql or object_type"):
        DashboardPanel(id="p")
    with pytest.raises(ValidationError, match="not both"):
        DashboardPanel(id="p", sql="SELECT 1", object_type="order",
                       metrics=[{"op": "count"}])


def test_an_object_panel_needs_a_metric():
    """Grouping with nothing to measure produces a chart of nothing."""
    with pytest.raises(ValidationError, match="at least one metric"):
        DashboardPanel(id="p", object_type="order")


def test_panel_kinds_are_distinguishable():
    sql = DashboardPanel(id="a", sql="SELECT 1")
    obj = DashboardPanel(id="b", object_type="order", metrics=[{"op": "count"}])
    assert not sql.is_object_panel
    assert obj.is_object_panel


def test_an_object_panel_round_trips_through_the_api(clients):
    admin, viewer = clients
    body = {
        "name": "ops",
        "title": "Ops",
        "panels": [{
            "id": "p1",
            "title": "Aircraft by status",
            "object_type": "aircraft",
            "group_by": ["status"],
            "metrics": [{"op": "count", "alias": "n"}],
            "chart": "bar",
        }],
    }
    assert admin.put("/api/v1/dashboards/ops", json=body).status_code == 200
    got = admin.get("/api/v1/dashboards/ops").json()
    panel = got["panels"][0]
    assert panel["object_type"] == "aircraft"
    assert panel["group_by"] == ["status"]
    assert panel["metrics"] == [{"op": "count", "alias": "n"}]
    assert panel["sql"] == ""

    # An object panel's aggregation is an instruction too: the viewer gets the
    # picture's title and chart kind and none of the query.
    seen = viewer.get("/api/v1/dashboards/ops").json()["panels"][0]
    assert seen["title"] == "Aircraft by status"
    assert "object_type" not in seen
    assert "group_by" not in seen
    assert "metrics" not in seen


def test_a_panel_with_both_sources_is_rejected_by_the_api(clients):
    admin, _ = clients
    r = admin.put("/api/v1/dashboards/bad", json={
        "name": "bad",
        "panels": [{"id": "p", "sql": "SELECT 1", "object_type": "aircraft",
                    "metrics": [{"op": "count"}]}],
    })
    assert r.status_code == 400
    assert "not both" in r.json()["detail"]


# -- the two panel kinds have to look the same on the wire ----------------------
#
# Found by rendering a real dashboard as a real viewer, not by reading the code:
# the run route returned `{groups, group_count, truncated}` for an object panel
# and `{columns, rows, row_count, truncated}` for a SQL one. The browser used to
# reconcile the two itself, deriving the column order from the panel's own
# `group_by` and `metrics` — which are exactly the fields R2 stops sending. So
# every object panel rendered as an empty box for every viewer: a security fix
# that broke the product, which is the one outcome the brief rules out.

_OBJECT_ONTOLOGY = """
object_types:
  - api_name: aircraft
    backing_dataset: fleet
    primary_key: tail_number
    title_property: tail_number
    properties:
      tail_number: {type: string}
      status: {type: string}
"""


@pytest.fixture()
def object_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURELIN_SCHEDULER", "0")
    ws = Workspace.init(tmp_path / "ws", name="objpanels")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("fleet", pa.table({
        "tail_number": ["N1", "N2", "N3"],
        "status": ["maintenance", "active", "maintenance"],
    }))
    (ws.ontology_dir / "o.yml").write_text(_OBJECT_ONTOLOGY)
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={"username": "vic", "password": "password123",
                                      "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return admin, viewer


def test_running_an_object_panel_returns_the_same_shape_as_a_sql_panel(object_clients):
    admin, viewer = object_clients
    assert admin.put("/api/v1/dashboards/fleet", json={
        "title": "Fleet",
        "panels": [
            {"id": "obj", "title": "By status", "object_type": "aircraft",
             "group_by": ["status"], "metrics": [{"op": "count", "alias": "n"}],
             "chart": "bar"},
            {"id": "sql", "title": "Count", "sql": "SELECT count(*) AS n FROM fleet",
             "chart": "stat"},
        ],
    }).status_code == 200

    obj = viewer.post("/api/v1/dashboards/fleet/panels/obj/run", json={}).json()
    sql = viewer.post("/api/v1/dashboards/fleet/panels/sql/run", json={}).json()

    # One renderer, one shape. A client that has to tell the two apart needs the
    # panel's source kind, and a viewer is not given it.
    assert sorted(obj) == sorted(sql) == ["columns", "row_count", "rows", "truncated"]
    # Grouping keys first, then one column per metric under its alias — the
    # order the browser used to build from `group_by`/`metrics`.
    assert obj["columns"] == ["status", "n"]
    assert obj["row_count"] == 2
    assert {r["status"]: r["n"] for r in obj["rows"]} == {"maintenance": 2, "active": 1}


def test_a_metric_with_no_alias_still_gets_a_column_name(object_clients):
    """Because the alias is optional and a nameless column renders as nothing."""
    admin, viewer = object_clients
    admin.put("/api/v1/dashboards/fleet2", json={
        "title": "Fleet",
        "panels": [{"id": "obj", "title": "n", "object_type": "aircraft",
                    "group_by": ["status"], "metrics": [{"op": "count"}],
                    "chart": "table"}],
    })
    run = viewer.post("/api/v1/dashboards/fleet2/panels/obj/run", json={}).json()
    assert run["columns"] == ["status", "count"]


def test_a_panel_error_does_not_echo_an_object_panels_stored_fields_back(object_clients):
    """The SQL branch of the run route was hardened; the object branch three
    lines below it was not, and that made the route an oracle for exactly the
    fields it withholds.

    Measured, as a plain viewer, against panels an editor saved (panels are not
    validated against the ontology at write time, so all three saved with 200):

        run gb -> 400 "Unknown group_by property 'postgresql://svc:PANELSECRET@…'"
        run mp -> 400 "Unknown property 'postgresql://svc:PANELSECRET@…'"
        run fk -> 500 (duckdb BinderException, not a ValueError, nobody caught it)

    Every one of those sentences is Laurelin's own — R1 was satisfied, R2 was
    not. Structure fixes R1's problem; only privilege fixes R2's.
    """
    admin, viewer = object_clients
    marker = "postgresql://svc:PANELSECRET@internal-db:5432/x"
    panels = [
        {"id": "gb", "title": "g", "object_type": "aircraft",
         "group_by": [marker], "metrics": [{"op": "count", "alias": "n"}]},
        {"id": "mp", "title": "m", "object_type": "aircraft",
         "group_by": ["status"],
         "metrics": [{"op": "sum", "property": marker, "alias": "n"}]},
        {"id": "op", "title": "o", "object_type": "aircraft",
         "group_by": ["status"], "metrics": [{"op": marker}]},
        {"id": "fk", "title": "f", "object_type": "aircraft",
         "group_by": ["status"], "metrics": [{"op": "count", "alias": "n"}],
         "filters": {marker: "x"}},
    ]
    assert admin.put(
        "/api/v1/dashboards/oracle", json={"title": "O", "panels": panels}
    ).status_code == 200
    # The read path is right, and always was — that is what makes the error
    # path an oracle rather than a duplicate.
    assert marker not in viewer.get("/api/v1/dashboards/oracle").text

    for panel in panels:
        r = viewer.post(f"/api/v1/dashboards/oracle/panels/{panel['id']}/run", json={})
        # A 500 is not an acceptable answer either: it is the empty box the
        # brief forbids, with no code, no ref and nothing to act on.
        assert r.status_code == 400, (panel["id"], r.status_code, r.text[:300])
        assert marker not in r.text, (panel["id"], r.text[:300])
        assert marker.lower() not in r.text.lower(), (panel["id"], r.text[:300])
        assert "definition_stale" in r.text or "no longer exists" in r.text


def test_an_editor_is_told_which_stored_field_is_stale_because_they_could_fix_it(
    object_clients,
):
    """The other half of the rule. Withholding the message from everyone would
    be safe and useless: the person who can repair the panel has to be told
    which field to repair. They can author it, so they may read it."""
    admin, _viewer = object_clients
    assert admin.put("/api/v1/dashboards/oracle2", json={
        "title": "O",
        "panels": [{"id": "gb", "title": "g", "object_type": "aircraft",
                    "group_by": ["no_such_property"],
                    "metrics": [{"op": "count", "alias": "n"}]}],
    }).status_code == 200
    r = admin.post("/api/v1/dashboards/oracle2/panels/gb/run", json={})
    assert r.status_code == 400
    assert "no_such_property" in r.json()["detail"]


def test_a_whole_board_put_can_clear_an_operational_field_it_sends(object_clients):
    """`_preserve_operational` guards against a read-modify-write client that
    *omits* a key. It used to guard against one that *sends an empty* key too,
    and those are not the same thing.

    Measured before this distinction existed: an editor clearing `group_by` got
    `200` and the old value back — a write silently rejected with a success
    status — and converting a SQL panel to an object panel was unreachable,
    because the server re-inserted the stored `sql` and then returned
    `400 "A panel draws from either sql or object_type, not both"`, blaming the
    editor for something the server had just added.
    """
    admin, _viewer = object_clients
    board = {"title": "B", "panels": [
        {"id": "o", "title": "o", "object_type": "aircraft",
         "group_by": ["status"], "metrics": [{"op": "count", "alias": "n"}]},
        {"id": "s", "title": "s", "sql": "SELECT 1 AS x"},
    ]}
    assert admin.put("/api/v1/dashboards/clear", json=board).status_code == 200

    # Sending `[]` means "make it empty", and is honoured.
    cleared = dict(board)
    cleared["panels"] = [
        {**board["panels"][0], "group_by": []}, board["panels"][1],
    ]
    r = admin.put("/api/v1/dashboards/clear", json=cleared)
    assert r.status_code == 200, r.text
    stored = {p["id"]: p for p in r.json()["panels"]}
    assert stored["o"]["group_by"] == []

    # Omitting the key entirely still inherits — that is the read-modify-write
    # protection, and it is the case the projection actually produces.
    partial = dict(board)
    partial["panels"] = [{"id": "s", "title": "s"}]
    r = admin.put("/api/v1/dashboards/clear", json=partial)
    assert r.status_code == 200, r.text
    assert r.json()["panels"][0]["sql"] == "SELECT 1 AS x"


def test_a_panel_can_be_converted_from_sql_to_object_backed(object_clients):
    """The whole-board PUT is documented for full replacement, so changing a
    panel's kind through it has to work. It did not: the merge re-inserted the
    stored `sql` and the shape validator then rejected the editor's own edit."""
    admin, _viewer = object_clients
    assert admin.put("/api/v1/dashboards/convert", json={
        "title": "C", "panels": [{"id": "s", "title": "s", "sql": "SELECT 1 AS x"}],
    }).status_code == 200
    r = admin.put("/api/v1/dashboards/convert", json={
        "title": "C",
        "panels": [{"id": "s", "title": "s", "sql": "", "object_type": "aircraft",
                    "group_by": ["status"], "metrics": [{"op": "count", "alias": "n"}]}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["panels"][0]["object_type"] == "aircraft"
    assert r.json()["panels"][0]["sql"] == ""


def test_a_panels_column_labels_are_captions_and_reach_the_viewer_by_design(
    object_clients,
):
    """The honest boundary of R2 on a dashboard, written down so nobody has to
    infer it from the guard's silence.

    A viewer receives a panel's `title` and, through the run route, the column
    headers — which come from `metrics[].alias`, or from the aggregation `op`
    when the author left the alias blank. Those are **captions**: text an editor
    wrote *for* this reader, exactly like the panel title, and a table with no
    headers is not a table.

    So an editor who types a credential into a column alias has disclosed it to
    their own audience deliberately, the same way they would by typing it into
    the panel title, and no amount of gating changes that. What R2 withholds is
    the panel's *instructions* — `sql`, `group_by`, `filters`, `search`, and
    each metric's `op` and `property` — and that is what the guard in
    `tests/test_audience.py` plants sentinels in.
    """
    admin, viewer = object_clients
    assert admin.put("/api/v1/dashboards/labels", json={
        "title": "L",
        "panels": [{"id": "o", "title": "Aircraft by status",
                    "object_type": "aircraft", "group_by": ["status"],
                    "metrics": [{"op": "count", "alias": "how many"}],
                    "chart": "table"}],
    }).status_code == 200

    board = viewer.get("/api/v1/dashboards/labels").json()
    panel = board["panels"][0]
    # The caption reaches them; the instruction does not.
    assert panel["title"] == "Aircraft by status"
    assert "group_by" not in panel and "metrics" not in panel and "sql" not in panel

    run = viewer.post("/api/v1/dashboards/labels/panels/o/run", json={}).json()
    assert run["columns"] == ["status", "how many"]
