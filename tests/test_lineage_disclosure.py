"""Task #75: a viewer must not recover a hidden dataset's NAME from the build,
lineage or transform surfaces.

``GET /datasets`` has always row-filtered by dataset visibility, and
``_require_dataset_view`` answers **404** — not 403 — precisely so that a
hidden dataset's *existence* does not leak one URL over. Four routes did not
honour that boundary: ``GET /transforms``, ``GET /builds``, ``GET /builds/{id}``
and ``GET /lineage`` each named every dataset in the workspace to anyone with
the viewer role, because ``laurelin.core.serialize.dump`` enforces R2 at
*field* level and has no concept of a row.

The invariant these tests pin, in one sentence:

    A route may name a dataset, or a transform whose name is a dataset name,
    only to a principal who may view that dataset; where a name is withheld the
    response withholds the whole node and edge and marks the surviving endpoint
    with a single unquantified boolean, and no status, count or shape is
    falsified to make the projection look complete.

The lineage decision, recorded because it was the reason #75 sat open: a
withheld node is **dropped along with both its edges**, and each surviving
neighbour carries one boolean (``has_hidden_upstream`` /
``has_hidden_downstream``). Not a silent drop (the graph would *look* complete
and be wrong — the same class of bug as a schedule row reading "succeeded" over
a failed build), and not an anonymised placeholder (which preserves topology
and count, and counts are the thing #75 is about). One bit per visible node is
the same bit the 404 already concedes.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

_TICKET = _ChangeTicket(kind="local", actor="test")

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}

HIDDEN = "topsecret_payroll"
VISIBLE_IN = "raw_pay"
VISIBLE_OUT = "pay_summary"

# raw_pay -> topsecret_payroll (hidden) -> downstream_of_secret (hidden input)
# raw_pay -> pay_summary (visible throughout)
PIPELINE = f"""
from laurelin.transforms import sql_transform, Input, Output


@sql_transform(
    output=Output('{HIDDEN}'),
    inputs={{'r': Input('{VISIBLE_IN}')}},
    query='SELECT * FROM r',
)
def {HIDDEN}():
    ...


@sql_transform(
    output=Output('{VISIBLE_OUT}'),
    inputs={{'r': Input('{VISIBLE_IN}')}},
    query='SELECT name FROM r',
)
def {VISIBLE_OUT}():
    ...


@sql_transform(
    output=Output('downstream_of_secret'),
    inputs={{'s': Input('{HIDDEN}')}},
    query='SELECT * FROM s',
)
def downstream_of_secret():
    ...
"""


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="disclosure")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write(VISIBLE_IN, pa.table({"name": ["a", "b"], "pay": [1, 2]}))
    (ws.pipelines_dir / "pay.py").write_text(PIPELINE)
    return ws


@pytest.fixture()
def app(ws):
    return create_app(ws)


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    assert c.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert c.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    return c


@pytest.fixture()
def built(ws, app, admin):
    """The #75 repro, end to end: build everything, then restrict the payroll
    output (and its downstream) to admin, and hand back a logged-in viewer."""
    r = admin.post("/api/v1/builds", json={"wait": True})
    assert r.status_code == 200, r.text
    store = MetadataStore(ws.metadata_path)
    for name in (HIDDEN, "downstream_of_secret"):
        store.set_grants_for_dataset(name, [{
            "subject_kind": "role", "subject": "admin",
            "can_view": True, "can_edit": True,
        }], ticket=_TICKET)
    assert admin.post("/api/v1/users", json={
        "username": "vic", "password": "password123", "role": "viewer",
    }).status_code == 200
    viewer = TestClient(app)
    assert viewer.post("/api/v1/auth/login", json={
        "username": "vic", "password": "password123",
    }).status_code == 200
    return viewer


def _bodies(client) -> dict[str, object]:
    out = {}
    for path in ("/api/v1/datasets", "/api/v1/transforms", "/api/v1/builds",
                 "/api/v1/lineage"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        out[path] = r.json()
    return out


def test_a_viewer_never_reads_a_hidden_dataset_name_from_builds_lineage_or_transforms(built):
    viewer = built
    bodies = _bodies(viewer)

    # The precedent that already held.
    assert {d["name"] for d in bodies["/api/v1/datasets"]} == {VISIBLE_IN, VISIBLE_OUT}

    # The three that did not. The name must not appear ANYWHERE in the body —
    # not as an output, not as an input, not as a transform name, not as a
    # `Failure.subject` ("transform:topsecret_payroll" is the dataset name in
    # a namespace, which is still the name).
    for path in ("/api/v1/transforms", "/api/v1/builds", "/api/v1/lineage"):
        blob = json.dumps(bodies[path])
        assert HIDDEN not in blob, f"{path} leaked {HIDDEN!r}: {blob}"
        assert "downstream_of_secret" not in blob, f"{path} leaked a hidden name"

    # Withholding, not blanking: what the viewer MAY see is still there.
    assert {t["name"] for t in bodies["/api/v1/transforms"]} == {VISIBLE_OUT}
    assert bodies["/api/v1/transforms"][0]["inputs"] == [VISIBLE_IN]
    assert bodies["/api/v1/builds"], "the build itself is not withheld"
    assert {n["id"] for n in bodies["/api/v1/lineage"]["nodes"]} == {
        VISIBLE_IN, VISIBLE_OUT
    }


def test_a_build_id_a_viewer_may_not_see_answers_404_exactly_like_an_unknown_id(
    ws, app, admin, built
):
    """The oracle by construction: `GET /builds/{id}` used to answer 404 for an
    unknown id and a full body for a known one, so a viewer could confirm a
    build existed. The two answers must be byte-identical."""
    viewer = built
    # A build whose every target and task is hidden from the viewer.
    r = admin.post("/api/v1/builds", json={"wait": True, "targets": [HIDDEN]})
    assert r.status_code == 200, r.text
    hidden_build_id = r.json()["id"]

    withheld = viewer.get(f"/api/v1/builds/{hidden_build_id}")
    unknown = viewer.get("/api/v1/builds/build-that-never-existed")
    assert withheld.status_code == unknown.status_code == 404
    # Identical *shape*; the id differs because the caller supplied it.
    assert withheld.json()["detail"] == f"Build not found: {hidden_build_id!r}"
    assert unknown.json()["detail"] == (
        "Build not found: 'build-that-never-existed'"
    )
    assert HIDDEN not in withheld.text


def test_a_failed_build_still_reads_failed_when_the_failing_task_is_withheld(
    ws, app, admin
):
    """Status is never falsified to make the projection self-consistent.

    A build whose visible task succeeded and whose hidden task failed reads
    `failed` with `hidden_tasks: true`. Reporting `succeeded` would be the
    schedule-row bug again: self-consistent, and a lie.
    """
    # A transform that fails for real: it selects a column that is not there.
    (ws.pipelines_dir / "broken.py").write_text(
        "from laurelin.transforms import sql_transform, Input, Output\n\n\n"
        "@sql_transform(output=Output('secret_broken'),\n"
        f"               inputs={{'r': Input('{VISIBLE_IN}')}},\n"
        "               query='SELECT nope FROM r')\n"
        "def secret_broken():\n    ...\n"
    )
    r = admin.post("/api/v1/builds", json={
        "wait": True, "targets": [VISIBLE_OUT, "secret_broken"],
    })
    assert r.status_code == 200, r.text
    build = r.json()
    assert build["status"] == "failed"

    store = MetadataStore(ws.metadata_path)
    store.set_grants_for_dataset("secret_broken", [{
        "subject_kind": "role", "subject": "admin", "can_view": True,
    }], ticket=_TICKET)
    assert admin.post("/api/v1/users", json={
        "username": "vic", "password": "password123", "role": "viewer",
    }).status_code == 200
    viewer = TestClient(app)
    assert viewer.post("/api/v1/auth/login", json={
        "username": "vic", "password": "password123",
    }).status_code == 200

    got = viewer.get(f"/api/v1/builds/{build['id']}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert "secret_broken" not in json.dumps(body)
    assert body["status"] == "failed"           # not falsified
    assert body["hidden_tasks"] is True          # marked, not counted
    assert [t["output_dataset"] for t in body["tasks"]] == [VISIBLE_OUT]


def test_a_withheld_lineage_neighbour_is_marked_not_counted(built):
    viewer = built
    graph = viewer.get("/api/v1/lineage").json()
    by_id = {n["id"]: n for n in graph["nodes"]}

    # No placeholder node stands in for the hidden one: the surviving nodes are
    # exactly the visible datasets and the one transform whose output is
    # visible (a flow's transform name IS its output name, so the transform
    # node for `pay_summary` shares the dataset node's id).
    assert set(by_id) == {VISIBLE_IN, VISIBLE_OUT}
    assert all(n["type"] in ("dataset", "transform") for n in graph["nodes"])

    # raw_pay feeds a transform the viewer cannot see -> one bit, no count.
    assert by_id[VISIBLE_IN]["has_hidden_downstream"] is True
    assert by_id[VISIBLE_IN]["has_hidden_upstream"] is False
    for node in graph["nodes"]:
        assert isinstance(node["has_hidden_upstream"], bool)
        assert isinstance(node["has_hidden_downstream"], bool)

    # Nothing anywhere in the body counts the hidden things.
    blob = json.dumps(graph)
    assert "hidden_count" not in blob and "hidden_nodes" not in blob

    # Every surviving edge has both endpoints present.
    for edge in graph["edges"]:
        assert edge["from"] in by_id and edge["to"] in by_id


def test_failed_task_counters_never_count_tasks_the_reader_cannot_see(
    ws, app, admin
):
    """`counters={'failed_tasks': N}` on the build-level failure is a count over
    tasks the reader may not see — a quantified leak. It is recounted over the
    visible projection, and dropped entirely rather than reported as 0."""
    (ws.pipelines_dir / "broken.py").write_text(
        "from laurelin.transforms import sql_transform, Input, Output\n\n\n"
        "@sql_transform(output=Output('secret_broken'),\n"
        "               inputs={'r': Input('raw_pay')},\n"
        "               query='SELECT nope FROM r')\n"
        "def secret_broken():\n    ...\n"
    )
    r = admin.post("/api/v1/builds", json={
        "wait": True, "targets": [VISIBLE_OUT, "secret_broken"],
    })
    assert r.status_code == 200
    build_id = r.json()["id"]
    assert r.json()["failure"]["counters"]["failed_tasks"] == 1

    store = MetadataStore(ws.metadata_path)
    store.set_grants_for_dataset("secret_broken", [{
        "subject_kind": "user", "subject": "ed2", "can_view": True,
    }], ticket=_TICKET)
    # An EDITOR, so `counters` is inside their audience — and still must not
    # count a task they cannot see.
    assert admin.post("/api/v1/users", json={
        "username": "ed", "password": "password123", "role": "editor",
    }).status_code == 200
    editor = TestClient(app)
    assert editor.post("/api/v1/auth/login", json={
        "username": "ed", "password": "password123",
    }).status_code == 200

    body = editor.get(f"/api/v1/builds/{build_id}").json()
    assert "secret_broken" not in json.dumps(body)
    assert body["status"] == "failed"
    assert body["failure"]["code"]  # still says *that* it failed
    # The recount is 0, so the counter is dropped rather than reported as 0.
    assert "failed_tasks" not in body["failure"].get("counters", {})


def _flow_json(dataset: str) -> dict:
    return {
        "name": "f", "output": "f", "author": "root", "description": "",
        "terminal": "s1", "expectations": [],
        "nodes": [{"id": "s1", "kind": "source", "inputs": [],
                   "params": {"dataset": dataset}}],
    }


def test_running_a_stored_flow_discloses_one_bit_about_a_hidden_source_and_never_its_name(
    ws, app, admin
):
    """The related oracle, measured and settled — NOT collapsed.

    Running a panel someone else saved answers 403 ("reads data not shared with
    your role") for a withheld source and 400 (definition_stale) for an absent
    one. Those two ARE machine-distinguishable, and that is deliberate: it is
    the same single unquantified bit `has_hidden_upstream` concedes, and
    collapsing it would put back the bug where a viewer's perfectly good shared
    chart was diagnosed as broken. See the comment in
    `laurelin/transforms/flow_governance.py`.

    What must hold, and is what this test pins: **neither answer names a
    dataset, and the withheld answer is byte-identical whichever dataset is
    withheld** — so the bit cannot be diffed into an enumeration.
    """
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("secret_a", pa.table({"a": [1]}))
    cat.write("secret_b", pa.table({"a": [1]}))
    for name, dataset in (("p_a", "secret_a"), ("p_b", "secret_b"),
                          ("p_gone", "never_existed_ds")):
        r = admin.put(f"/api/v1/dashboards/{name}", json={
            "name": name, "title": name,
            "panels": [{"id": "p1", "title": "t",
                        "flow": _flow_json(dataset), "chart": "table"}],
        })
        assert r.status_code == 200, r.text
    store = MetadataStore(ws.metadata_path)
    for dataset in ("secret_a", "secret_b"):
        store.set_grants_for_dataset(dataset, [{
            "subject_kind": "role", "subject": "admin", "can_view": True,
        }], ticket=_TICKET)
    assert admin.post("/api/v1/users", json={
        "username": "vic", "password": "password123", "role": "viewer",
    }).status_code == 200
    viewer = TestClient(app)
    assert viewer.post("/api/v1/auth/login", json={
        "username": "vic", "password": "password123",
    }).status_code == 200

    def run(board: str):
        return viewer.post(
            f"/api/v1/dashboards/{board}/panels/p1/run", json={"max_rows": 10}
        )

    a, b, gone = run("p_a"), run("p_b"), run("p_gone")
    assert a.status_code == b.status_code == 403
    # The withheld sentence does not vary with WHICH dataset is withheld.
    assert a.json()["detail"] == b.json()["detail"]
    for r in (a, b, gone):
        for name in ("secret_a", "secret_b", "never_existed_ds"):
            assert name not in r.text


def test_an_entitled_reader_still_sees_the_whole_graph(built, admin):
    """Governance display must not regress in the other direction: the marks
    are absent, not merely hidden, for a principal who sees everything."""
    graph = admin.get("/api/v1/lineage").json()
    ids = {n["id"] for n in graph["nodes"]}
    assert HIDDEN in ids and "downstream_of_secret" in ids
    assert all(
        n["has_hidden_upstream"] is False and n["has_hidden_downstream"] is False
        for n in graph["nodes"]
    )
    builds = admin.get("/api/v1/builds").json()
    assert all(b["hidden_tasks"] is False for b in builds)


# ---------------------------------------------------------------------------
# The surfaces the first pass at #75 did not enumerate. Every one of these was
# reproduced live against a real `laurelin serve` before it was fixed.
# ---------------------------------------------------------------------------

def test_the_iceberg_impact_route_never_names_a_dataset_its_reader_may_not_view(built):
    """`GET /datasets/{n}/iceberg/schema/impact` checked the SUBJECT dataset
    and then returned `catalog.downstream_of(name)` — the whole transitive
    closure over the same lineage edges #75 had just projected.

    Measured live as a viewer who could see four datasets:
    `{"downstream": ["downstream_of_secret", "joined_public", "pay_summary",
    "topsecret_payroll"]}`, byte-identical to the admin's answer. That is the
    NAMES and the CARDINALITY of the hidden topology — exactly what the
    anonymised-placeholder option was rejected for.
    """
    pytest.importorskip("pyiceberg")
    viewer = built
    r = viewer.get(f"/api/v1/datasets/{VISIBLE_IN}/iceberg/schema/impact")
    assert r.status_code == 200, r.text
    body = r.json()
    assert HIDDEN not in r.text and "downstream_of_secret" not in r.text
    assert VISIBLE_OUT in body["downstream"]
    # One unquantified boolean, never a count.
    assert body["hidden_downstream"] is True
    assert not any(type(v) is int for v in body.values())


def test_an_iceberg_route_answers_a_withheld_name_and_an_absent_one_identically(built):
    """`_require_dataset_view` passes for a name with NO grants (default view),
    so the storage lookup one line later raised for an absent name while a
    withheld name was stopped at the guard. Measured as a viewer:

        topsecret_payroll/snapshots -> 404 Dataset not found
        no_such_zzz/snapshots       -> 500 NoSuchTableError
        no_such_zzz/schema/impact   -> 200 {"downstream": []}

    which is a clean binary enumeration oracle over the whole namespace.
    """
    pytest.importorskip("pyiceberg")
    viewer = built
    for route in ("snapshots", "storage", "branches", "schema/impact"):
        withheld = viewer.get(f"/api/v1/datasets/{HIDDEN}/iceberg/{route}")
        absent = viewer.get(f"/api/v1/datasets/no_such_zzz_dataset/iceberg/{route}")
        assert withheld.status_code == absent.status_code == 404, (
            route, withheld.status_code, absent.status_code, absent.text
        )
        assert withheld.json()["detail"] == f"Dataset not found: {HIDDEN!r}"
        assert absent.json()["detail"] == "Dataset not found: 'no_such_zzz_dataset'"


def test_an_object_type_a_reader_may_not_see_answers_like_an_unknown_one(ws, app, admin, built):
    """`GET /ontology/object-types` filters a withheld type out of the list and
    the by-name routes handed its existence straight back with
    `403 "You do not have access to object type 'secret_person'"`, while an
    unknown name answered 404. Second-order, and that is what makes it matter:
    the type is withheld BECAUSE its backing dataset is
    (`object_type_permission` composes the dataset's view right), so the 403
    is an oracle on the hidden dataset one indirection away.
    """
    (ws.root / "ontology").mkdir(exist_ok=True)
    (ws.root / "ontology" / "atk.yml").write_text(
        "object_types:\n"
        "  - api_name: secret_person\n"
        "    display_name: Secret person\n"
        f"    backing_dataset: {HIDDEN}\n"
        "    primary_key: name\n"
        "    title_property: name\n"
        "    properties:\n"
        "      name: {type: string, display_name: Name}\n"
    )
    viewer = built
    listed = viewer.get("/api/v1/ontology/object-types")
    assert listed.status_code == 200 and "secret_person" not in listed.text

    for path in ("object-types", "objects"):
        withheld = viewer.get(f"/api/v1/ontology/{path}/secret_person")
        unknown = viewer.get(f"/api/v1/ontology/{path}/nope_xyz")
        assert withheld.status_code == unknown.status_code == 404, path
        assert withheld.json()["detail"] == "Unknown object type: 'secret_person'"
        assert unknown.json()["detail"] == "Unknown object type: 'nope_xyz'"


def test_a_build_request_cannot_confirm_a_target_the_author_may_not_view(ws, app, admin, built):
    """The WRITE door must not mint what the read door withholds.

    `POST /builds {"targets": ["topsecret_payroll"]}` answered 200 with the
    name echoed through `targets` and every task, to an editor whose
    `GET /datasets/topsecret_payroll` was 404 — and whose `GET /builds/{id}`
    for the build he had just created was 404 as well. It was also an
    unmitigated oracle: a hidden target answered 200 and an absent one 400, so
    any name in the workspace could be tested one request at a time.
    """
    assert admin.post("/api/v1/users", json={
        "username": "eddie", "password": "password123", "role": "editor",
    }).status_code == 200
    editor = TestClient(app)
    assert editor.post("/api/v1/auth/login", json={
        "username": "eddie", "password": "password123",
    }).status_code == 200

    hidden = editor.post("/api/v1/builds", json={"wait": True, "targets": [HIDDEN]})
    absent = editor.post("/api/v1/builds",
                         json={"wait": True, "targets": ["no_such_thing"]})
    assert hidden.status_code == absent.status_code == 400
    assert hidden.json()["detail"] == (
        f"Unknown build target {HIDDEN!r}: no transform produces it"
    )
    assert absent.json()["detail"] == (
        "Unknown build target 'no_such_thing': no transform produces it"
    )
    # And a build-everything run, which names nothing on the way in, does not
    # name anything on the way out either.
    every = editor.post("/api/v1/builds", json={"wait": True})
    assert every.status_code == 200, every.text
    assert HIDDEN not in every.text and "downstream_of_secret" not in every.text
    assert every.json()["hidden_tasks"] is True
    # The true status survives the projection.
    assert every.json()["status"] in ("succeeded", "failed")


def test_a_pipeline_file_is_withheld_when_the_datasets_it_produces_are(ws, app, admin, built):
    """`GET /pipelines` returned `{"transforms": ["topsecret_payroll", ...]}` —
    the same structured list of dataset names `/transforms` had just been
    filtered on — and `GET /pipelines/{name}` returned the source containing
    `Output('topsecret_payroll')`, to an editor who got 404 on the dataset
    itself. The Builds page told that same principal "Parts of this lineage are
    not shared with your role" one click away.

    A file's CONTENT cannot be row-filtered (it is arbitrary authored Python),
    so the read is refused whole, in the words an absent file gets.
    """
    assert admin.post("/api/v1/users", json={
        "username": "eddie", "password": "password123", "role": "editor",
    }).status_code == 200
    editor = TestClient(app)
    assert editor.post("/api/v1/auth/login", json={
        "username": "eddie", "password": "password123",
    }).status_code == 200

    listed = editor.get("/api/v1/pipelines")
    assert listed.status_code == 200, listed.text
    assert HIDDEN not in listed.text and "downstream_of_secret" not in listed.text
    entry = next(e for e in listed.json() if e["name"] == "pay")
    assert entry["transforms"] == [VISIBLE_OUT]
    assert entry["hidden_transforms"] is True

    withheld = editor.get("/api/v1/pipelines/pay")
    unknown = editor.get("/api/v1/pipelines/no_such_file")
    assert withheld.status_code == unknown.status_code == 404
    assert withheld.json()["detail"] == "Pipeline file not found: 'pay'"
    # The admin, who may view everything, still reads it.
    assert admin.get("/api/v1/pipelines/pay").status_code == 200


def test_a_schedule_never_names_a_dataset_its_reader_may_not_view(ws, app, admin, built):
    """The fifth surface. The same editor is denied the name on /datasets,
    /transforms, /builds and /lineage and was handed it by `GET /schedules`
    as `"targets": ["topsecret_payroll"]`."""
    assert admin.put("/api/v1/schedules/nightly", json={
        "trigger": "cron", "action": "build", "cron": "0 2 * * *",
        "targets": [HIDDEN], "enabled": True,
    }).status_code == 200
    assert admin.put("/api/v1/schedules/daytime", json={
        "trigger": "cron", "action": "build", "cron": "0 9 * * *",
        "targets": [VISIBLE_OUT, HIDDEN], "enabled": True,
    }).status_code == 200

    assert admin.post("/api/v1/users", json={
        "username": "eddie", "password": "password123", "role": "editor",
    }).status_code == 200
    editor = TestClient(app)
    assert editor.post("/api/v1/auth/login", json={
        "username": "eddie", "password": "password123",
    }).status_code == 200

    listed = editor.get("/api/v1/schedules")
    assert listed.status_code == 200
    assert HIDDEN not in listed.text
    names = {s["name"] for s in listed.json()}
    # Every target withheld -> the whole schedule is withheld; a mixed one
    # survives with one unquantified bit.
    assert names == {"daytime"}
    mixed = next(s for s in listed.json() if s["name"] == "daytime")
    assert mixed["targets"] == [VISIBLE_OUT]
    assert mixed["hidden_targets"] is True

    withheld = editor.get("/api/v1/schedules/nightly")
    unknown = editor.get("/api/v1/schedules/no_such_schedule")
    assert withheld.status_code == unknown.status_code == 404
    assert withheld.json()["detail"] == "Schedule not found: 'nightly'"


def test_an_output_can_be_withheld_before_its_first_successful_build(ws, app, admin):
    """A build that fails writes no version, so its intended-secret output had
    no row in `datasets` — and `PUT /datasets/{n}/permissions` answered
    `404 Dataset not found`. There was no door in the API to withhold it, for
    the whole window before its first SUCCESSFUL build and permanently if the
    build never succeeded. Measured: `secret_broken` reached a plain viewer
    through `GET /transforms` and `GET /builds` with a failure naming it.

    The projections were right; the ACL-WRITE door was the hole.
    """
    (ws.pipelines_dir / "broken.py").write_text(
        "from laurelin.transforms import sql_transform, Input, Output\n\n\n"
        "@sql_transform(output=Output('secret_broken'),\n"
        f"               inputs={{'r': Input('{VISIBLE_IN}')}},\n"
        "               query='SELECT nope FROM r')\n"
        "def secret_broken():\n    ...\n"
    )
    r = admin.post("/api/v1/builds", json={"wait": True, "targets": ["secret_broken"]})
    assert r.status_code == 200 and r.json()["status"] == "failed"

    listing = admin.get("/api/v1/dataset-permissions").json()
    pending = next(e for e in listing if e["dataset"] == "secret_broken")
    assert pending["built"] is False, "an unbuilt output must still be addressable"

    assert admin.put("/api/v1/datasets/secret_broken/permissions", json={
        "grants": [{"subject_kind": "role", "subject": "admin",
                    "can_view": True, "can_edit": True}],
    }).status_code == 200

    assert admin.post("/api/v1/users", json={
        "username": "vic2", "password": "password123", "role": "viewer",
    }).status_code == 200
    viewer = TestClient(app)
    assert viewer.post("/api/v1/auth/login", json={
        "username": "vic2", "password": "password123",
    }).status_code == 200
    for path in ("/api/v1/transforms", "/api/v1/builds", "/api/v1/lineage"):
        assert "secret_broken" not in viewer.get(path).text, path
