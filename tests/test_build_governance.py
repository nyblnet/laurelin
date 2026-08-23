"""Build-time input entitlement for API-authored python/sql transforms.

The invariant, in one sentence: **a transform authored through the API builds
only if its recorded author could read every input dataset in full — view
rights, and no row policy or column mask — checked in the Builder immediately
before the task runs, against the recorded author and never the triggering
principal.**

This is the closure of the gap the deleted
``test_the_python_transform_path_still_launders_acls_row_policies_and_column_masks``
(tests/test_flow_governance.py) pinned; its docstring ordered its deletion
when the gap closed. See the CHANGELOG entry.

Disk-authored, imported and in-memory pipelines have no ``pipeline_authors``
row and build unchecked — operator-trusted, deliberately, and pinned by a test
here so a future "fail closed everywhere" change has to argue with a sentence.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import BuildStatus, Role, User
from laurelin.core.permissions import PermissionService
from laurelin.transforms import Builder, collect_transforms

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}

LEAK = (
    "from laurelin.transforms import sql_transform, Input, Output\n\n\n"
    "@sql_transform(\n"
    "    output=Output('leak'),\n"
    "    inputs={'s': Input('secret_ds')},\n"
    "    query='SELECT * FROM s',\n"
    ")\n"
    "def leak():\n"
    "    ...\n"
)

OPEN_COPY = (
    "from laurelin.transforms import sql_transform, Input, Output\n\n\n"
    "@sql_transform(\n"
    "    output=Output('open_copy'),\n"
    "    inputs={'o': Input('open_ds')},\n"
    "    query='SELECT * FROM o',\n"
    ")\n"
    "def open_copy():\n"
    "    ...\n"
)


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="buildgov")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("secret_ds", pa.table({
        "region": ["us", "apac"], "amount": [1, 2], "note": ["a", "b"],
    }))
    cat.write("open_ds", pa.table({"region": ["us", "eu"], "amount": [3, 4]}))
    return ws


@pytest.fixture()
def app(ws):
    return create_app(ws)


def _admin(app) -> TestClient:
    c = TestClient(app)
    assert c.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert c.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    return c


def _make_and_login(app, admin_client, username, role="editor") -> TestClient:
    assert admin_client.post(
        "/api/v1/users",
        json={"username": username, "password": "password123", "role": role},
    ).status_code == 200
    c = TestClient(app)
    assert c.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "password123"},
    ).status_code == 200
    return c


def _grant_to(store, dataset, username):
    store.set_grants_for_dataset(dataset, [{
        "subject_kind": "user", "subject": username,
        "can_view": True, "can_edit": True,
    }])


def _build(ws, targets=None):
    """Build with NO request principal — exactly what the scheduler, the CLI
    and the async worker do. The check must bind to the recorded author."""
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    registry = collect_transforms(ws.pipelines_dir)
    return Builder(ws, catalog, store, registry).build(targets), store, catalog


def test_an_api_authored_transform_over_an_input_its_author_cannot_view_fails_its_build_task_and_nothing_else(
    ws, app
):
    """The measured laundering, refused — and only the laundering.

    bob cannot view `secret_ds` (granted to alice alone). His API-saved
    `SELECT * FROM s` used to build a world-readable copy; now his task fails
    with Laurelin's own refusal, the output dataset is never created, and an
    unrelated transform in the same build still succeeds.
    """
    admin = _admin(app)
    bob = _make_and_login(app, admin, "bob")
    assert bob.put("/api/v1/pipelines/leak", json={"content": LEAK}).status_code == 200
    assert bob.put(
        "/api/v1/pipelines/open_copy", json={"content": OPEN_COPY}
    ).status_code == 200

    store = MetadataStore(ws.metadata_path)
    _grant_to(store, "secret_ds", "alice")
    assert not PermissionService(store).can_view_dataset(
        store.get_user("bob"), "secret_ds"
    )

    build, store, catalog = _build(ws)
    tasks = {t.transform_name: t for t in build.tasks}

    assert build.status == BuildStatus.failed
    assert tasks["leak"].status == BuildStatus.failed
    # A first-party refusal, not a driver error.
    assert tasks["leak"].failure is not None
    assert tasks["leak"].failure.exc_class == "TransformRefused"
    # The output never came into being: nothing to read, no grants row, no
    # world-readable copy.
    assert store.get_dataset("leak") is None
    # Unrelated work in the same build is untouched.
    assert tasks["open_copy"].status == BuildStatus.succeeded
    assert catalog.read("open_copy").num_rows == 2
    # R1/R2: what a reader of the build learns names no column, no mask mode,
    # no policy rule.
    blob = json.dumps(tasks["leak"].failure.as_dict())
    for detail in ("region", "amount", "redact", "mask", "row_policy"):
        assert detail not in blob


def test_a_row_policied_or_masked_input_refuses_an_api_authored_transform_even_for_an_entitled_author(
    ws, app
):
    """The partial-entitlement laundering, closed.

    alice may VIEW `secret_ds` — restricted to `region='us'` by a row policy,
    `amount` masked. Her `SELECT *` output would be a new dataset with neither,
    handing every row and the real values to whoever reads it. `can_view`
    alone does not close item 58; this is the half that needed the full-read
    check. Flow parity, except column granularity: a Python transform's
    touched columns are unknowable, so any mask on an input refuses where a
    flow could drop the masked column.
    """
    admin = _admin(app)
    alice = _make_and_login(app, admin, "alice")
    assert alice.put("/api/v1/pipelines/leak", json={"content": LEAK}).status_code == 200
    assert alice.put(
        "/api/v1/pipelines/open_copy", json={"content": OPEN_COPY}
    ).status_code == 200

    store = MetadataStore(ws.metadata_path)
    _grant_to(store, "secret_ds", "alice")
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "alice", "values": ["us"]},
        ]},
        "column_masks": [],
    })
    # A mask ALONE also refuses (open_ds has no row policy and alice full view
    # rights): "in full" means no mask either, because the output carries none.
    store.set_dataset_policy("open_ds", {
        "dataset": "open_ds", "row_policy": None,
        "column_masks": [{"column": "amount", "mode": "redact", "exempt": []}],
    })

    build, store, _catalog = _build(ws)
    tasks = {t.transform_name: t for t in build.tasks}
    assert tasks["leak"].status == BuildStatus.failed
    assert tasks["leak"].failure.exc_class == "TransformRefused"
    assert tasks["open_copy"].status == BuildStatus.failed
    assert tasks["open_copy"].failure.exc_class == "TransformRefused"
    assert store.get_dataset("leak") is None
    assert store.get_dataset("open_copy") is None


def test_a_policy_that_does_not_apply_to_the_author_builds_but_one_that_restricts_them_refuses(
    ws, app
):
    """The check is about *applicability*, not the mere presence of a policy.

    "In full" means no row policy or column mask that **applies to the recorded
    author**. Refusing on presence alone turned every routine re-save of a
    pipeline over a policied input — including an admin's own re-save, the one
    recovery path — into an unbuildable file: a false refusal, which is as
    serious as a launder. So:

    * an admin (bypasses every policy) re-saving a transform over a
      row-policied input **builds** — this is the recovery path and the
      false-refusal that finding named;
    * an author explicitly **exempt** from a column mask reads that column in
      full and **builds**;
    * an author the row policy actually **filters** (alice, restricted to
      `region='us'`) is still **refused** — the partial-entitlement launder
      the pinned test named is untouched.
    """
    admin = _admin(app)  # root, an admin
    alice = _make_and_login(app, admin, "alice")
    carol = _make_and_login(app, admin, "carol")

    store = MetadataStore(ws.metadata_path)

    # (1) Admin author over a row-policied input: builds. The row policy only
    # names alice; the admin bypasses it and reads secret_ds in full.
    assert admin.put("/api/v1/pipelines/leak", json={"content": LEAK}).status_code == 200
    assert store.get_pipeline_author("leak") == "root"
    _grant_to(store, "secret_ds", "alice")
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "alice", "values": ["us"]},
        ]},
        "column_masks": [],
    })

    # (2) An author exempt from open_ds's mask reads the column in full: builds.
    _grant_to(store, "open_ds", "carol")
    store.set_dataset_policy("open_ds", {
        "dataset": "open_ds", "row_policy": None,
        "column_masks": [{
            "column": "amount", "mode": "redact",
            "exempt": [{"subject_kind": "user", "subject": "carol"}],
        }],
    })
    assert carol.put(
        "/api/v1/pipelines/open_copy", json={"content": OPEN_COPY}
    ).status_code == 200

    build, store, catalog = _build(ws)
    tasks = {t.transform_name: t for t in build.tasks}
    assert tasks["leak"].status == BuildStatus.succeeded, tasks["leak"].failure
    assert catalog.read("leak").num_rows == 2
    assert tasks["open_copy"].status == BuildStatus.succeeded, tasks["open_copy"].failure
    assert catalog.read("open_copy").num_rows == 2

    # (3) The same input, re-authored by the restricted editor alice, is still
    # refused: the row policy filters her, so a `SELECT *` would launder the
    # rows she cannot see. Applicability narrows the refusal; it does not lift
    # it for anyone it applies to.
    assert alice.put("/api/v1/pipelines/leak", json={"content": LEAK}).status_code == 200
    assert store.get_pipeline_author("leak") == "alice"
    build, store, _catalog = _build(ws)
    tasks = {t.transform_name: t for t in build.tasks}
    assert tasks["leak"].status == BuildStatus.failed
    assert tasks["leak"].failure.exc_class == "TransformRefused"


def test_a_no_auth_server_over_a_workspace_with_users_builds_an_api_authored_pipeline(ws):
    """`--no-auth` enforces no ACLs, so a stamped author cannot be checked.

    Under `--no-auth` every request is the implicit admin `anonymous`, which is
    not a real store user. On a workspace that also has real users — one
    imported (users travel with the export), or an authed workspace restarted
    with `--no-auth` — stamping `anonymous` made every later build refuse:
    `_author_user` saw users present and an author who is none of them and
    failed closed. The pipeline must instead fall into the operator-trusted
    bucket (no stamp), because there is no identity to check a no-auth build
    against.
    """
    store = MetadataStore(ws.metadata_path)
    store.create_user(User(id="1", username="realuser", role=Role.admin), "x")
    store.create_user(User(id="2", username="other", role=Role.editor), "x")

    no_auth_app = create_app(ws, no_auth=True)
    c = TestClient(no_auth_app)
    assert c.put(
        "/api/v1/pipelines/open_copy", json={"content": OPEN_COPY}
    ).status_code == 200
    # No accountable author was stamped: the file is operator-trusted.
    assert MetadataStore(ws.metadata_path).get_pipeline_author("open_copy") is None

    r = c.post("/api/v1/builds", json={"targets": ["open_copy"], "wait": True})
    assert r.status_code == 200
    tasks = {t["transform_name"]: t for t in r.json()["tasks"]}
    assert tasks["open_copy"]["status"] == "succeeded", tasks["open_copy"].get("failure")


def test_a_pipeline_file_written_on_disk_builds_unchecked(ws):
    """Operator trust, pinned deliberately.

    A `.py` placed in `pipelines/` without going through the API has no
    `pipeline_authors` row and builds exactly as before this change — even
    over a restricted input. Writing to that directory already requires
    operator or admin privilege: the CLI's stated credential is possession of
    the workspace directory, and imported files are admin-acknowledged on
    every build entry point. Refusing here would break every existing
    deployment to stop no attacker. A future "fail closed everywhere" change
    must delete this sentence and argue in the CHANGELOG.
    """
    store = MetadataStore(ws.metadata_path)
    store.create_user(User(id="1", username="alice", role=Role.editor), "x")
    store.create_user(User(id="2", username="bob", role=Role.editor), "x")
    _grant_to(store, "secret_ds", "alice")

    ws.pipelines_dir.mkdir(parents=True, exist_ok=True)
    (ws.pipelines_dir / "leak.py").write_text(LEAK)
    assert store.get_pipeline_author("leak") is None

    build, store, catalog = _build(ws)
    assert build.status == BuildStatus.succeeded
    assert catalog.read("leak").num_rows == 2


def test_a_stamped_pipeline_whose_author_no_longer_exists_refuses_and_a_zero_user_workspace_builds_anyway(
    ws,
):
    """`_author_user` parity with flows, in both directions.

    A zero-user workspace (`--no-auth`, the CLI, the tutorials) has no
    identity to check and no ACL to enforce: the stamp resolves to a
    synthesized admin and the build proceeds. The moment real users exist, a
    stamp naming someone who is not one of them fails closed — running as
    nobody, or as the system, is how a governance hole gets built.
    """
    store = MetadataStore(ws.metadata_path)
    ws.pipelines_dir.mkdir(parents=True, exist_ok=True)
    (ws.pipelines_dir / "leak.py").write_text(LEAK)

    # Zero users: stamped or not, the build proceeds.
    store.set_pipeline_author("leak", "someone")
    build, _store, catalog = _build(ws)
    assert build.status == BuildStatus.succeeded
    assert catalog.read("leak").num_rows == 2

    # Users exist and the recorded author is not among them: refuse.
    store.create_user(User(id="1", username="alice", role=Role.editor), "x")
    store.set_pipeline_author("leak", "ghost")
    build, _store, _catalog = _build(ws)
    tasks = {t.transform_name: t for t in build.tasks}
    assert tasks["leak"].status == BuildStatus.failed
    assert tasks["leak"].failure.exc_class == "FlowRefused"


def test_the_scheduler_and_the_cli_enforce_the_recorded_author_not_the_trigger(
    ws, app
):
    """An admin pressing "build" does not lend bob their eyes.

    Every direct `_build` in this file already runs with no request principal
    — the scheduler's and the CLI's exact posture. This test makes the other
    half explicit: the build being TRIGGERED by an admin (who can read
    everything) changes nothing, because the check binds to the file's
    recorded author. A route-level check on the trigger would be laundered by
    exactly this — or by EDITOR-gated `POST /schedules/{name}/run`.
    """
    admin = _admin(app)
    bob = _make_and_login(app, admin, "bob")
    assert bob.put("/api/v1/pipelines/leak", json={"content": LEAK}).status_code == 200

    store = MetadataStore(ws.metadata_path)
    _grant_to(store, "secret_ds", "alice")
    # The trigger could read the input; the author cannot.
    assert PermissionService(store).can_view_dataset(
        store.get_user("root"), "secret_ds"
    )

    r = admin.post("/api/v1/builds", json={"wait": True})
    assert r.status_code == 200
    tasks = {t["transform_name"]: t for t in r.json()["tasks"]}
    assert tasks["leak"]["status"] == "failed"
    assert store.get_dataset("leak") is None


def test_re_saving_a_pipeline_file_re_stamps_every_transform_in_it_to_the_new_saver(
    ws, app
):
    """Flow parity (`FlowFiles.write` re-stamps on every save): the file
    builds as its LAST saver, and a multi-transform file has one author."""
    admin = _admin(app)
    alice = _make_and_login(app, admin, "alice")
    bob = _make_and_login(app, admin, "bob")

    assert alice.put(
        "/api/v1/pipelines/shared", json={"content": OPEN_COPY}
    ).status_code == 200
    store = MetadataStore(ws.metadata_path)
    assert store.get_pipeline_author("shared") == "alice"

    assert bob.put(
        "/api/v1/pipelines/shared", json={"content": OPEN_COPY}
    ).status_code == 200
    assert store.get_pipeline_author("shared") == "bob"

    # The re-stamp is enforcement, not bookkeeping: restrict the input to
    # alice and the file — now bob's — refuses to build.
    _grant_to(store, "open_ds", "alice")
    build, _store, _catalog = _build(ws)
    tasks = {t.transform_name: t for t in build.tasks}
    assert tasks["open_copy"].status == BuildStatus.failed
    assert tasks["open_copy"].failure.exc_class == "TransformRefused"


def test_ejecting_a_flow_stamps_the_generated_pipeline_with_the_ejecting_user(
    ws, app
):
    """Without the stamp, an ejected file would fall into the operator-trusted
    bucket and eject's one-time authoring check would silently go stale as
    grants change. Stamped, it is re-checked at every build — strictly
    stronger than today's eject."""
    admin = _admin(app)
    alice = _make_and_login(app, admin, "alice")

    flow = {
        "output": "copyflow", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": "open_ds"}}],
    }
    assert alice.put(
        "/api/v1/flows/copyflow", json={"flow": flow}
    ).status_code == 200
    r = alice.post("/api/v1/flows/copyflow/eject")
    assert r.status_code == 200, r.text

    store = MetadataStore(ws.metadata_path)
    assert store.get_pipeline_author("copyflow") == "alice"

    # The stamped author is entitled to the input, so the ejected file builds.
    build, _store, catalog = _build(ws)
    assert build.status == BuildStatus.succeeded
    assert catalog.read("copyflow").num_rows == 2
