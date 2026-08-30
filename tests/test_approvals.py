"""Governance change approval: one chokepoint, every path enumerated (task #74).

The recurring wound this suite guards against is a guard on one path that is
absent on the next. The gate lives on the MetadataStore governance-write
methods (a required ``ticket`` kwarg), so the tests enumerate every path the
investigation found to a governed change — REST, MCP, SCIM, CLI-shaped service
calls, workspace import, flow governance — and prove per path that the gate is
hit. Two meta-tests make adding a new bypass fail the suite: every governance
store method refuses an unticketed write, and ``ChangeTicket`` construction is
AST-asserted to exist only in sanctioned modules.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import policy_diff, serialize
from laurelin.core.approvals import (
    ApprovalService,
    ChangeTicket,
    local_ticket,
)
from laurelin.core.config import Workspace
from laurelin.core.control import ControlStore
from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, ProposalInfo, Role, SubjectKind, User
from laurelin.core.permissions import PermissionService

_T = ChangeTicket(kind="local", actor="test")

ONTOLOGY = """
object_types:
  - api_name: secret_obj
    backing_dataset: secret_ds
    primary_key: id
    properties:
      id: {type: string}
"""

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}
ADMIN2_CREDS = {"username": "root2", "password": "trustno2!"}


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="approvals")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("secret_ds", pa.table({"id": ["s1"], "tenant": ["t1"]}))
    cat.write("public_ds", pa.table({"id": ["p1"]}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return ws


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def perms(store):
    return PermissionService(store)


@pytest.fixture()
def clients(ws):
    """(app, root, root2, viewer-client). Two admins so second-approver mode
    can be enabled; vic is a plain viewer, ed a plain editor — the comparator
    needs a non-admin population whose capabilities a change can widen."""
    app = create_app(ws)
    root = TestClient(app)
    root.post("/api/v1/auth/setup", json=ADMIN_CREDS)
    root.post("/api/v1/auth/login", json=ADMIN_CREDS)
    root.post("/api/v1/users", json={**ADMIN2_CREDS, "role": "admin"})
    root.post("/api/v1/users", json={"username": "vic", "password": "password123",
                                     "role": "viewer"})
    root.post("/api/v1/users", json={"username": "ed", "password": "password123",
                                     "role": "editor"})
    root2 = TestClient(app)
    root2.post("/api/v1/auth/login", json=ADMIN2_CREDS)
    vic = TestClient(app)
    vic.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return app, root, root2, vic


def _enable_second(root):
    r = root.put("/api/v1/settings/approvals", json={"require_second_approver": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"require_second_approver": True}


def _pending(root):
    return [p for p in root.get("/api/v1/proposals").json() if p["state"] == "pending"]


# ---------------------------------------------------------------------------
# The chokepoint itself: no path can write without declaring
# ---------------------------------------------------------------------------

def test_every_governance_store_write_demands_a_ticket(store, tmp_path):
    """The gate is the store method, not a route decorator: an unticketed call
    raises before any SQL runs, so a future caller (a new route, a new
    service) cannot forget — forgetting is a TypeError, not a bypass."""
    grants = [Grant(subject_kind=SubjectKind.user, subject="vic",
                    can_view=True).model_dump(mode="json")]
    unticketed = [
        lambda: store.set_grants_for_dataset("secret_ds", grants),
        lambda: store.set_grants_for_type("secret_obj", grants),
        lambda: store.set_dataset_policy("secret_ds", None),
        lambda: store.set_explicit_markings("secret_ds", []),
        lambda: store.delete_marking("pii"),
        lambda: store.set_clearances("vic", []),
        lambda: store.set_group_members("g", []),
        lambda: store.update_group_members("g", lambda cur: cur),
        lambda: store.delete_group("g"),
        lambda: store.update_user("vic", role="admin"),
    ]
    for call in unticketed:
        with pytest.raises(TypeError):
            call()
    control = ControlStore(tmp_path / "control.db")
    with pytest.raises(TypeError):
        control.set_member("wsx", "vic", Role.admin)
    # ...and a wrong-typed ticket is refused too: the declaration must be the
    # real dataclass, not a truthy stand-in.
    with pytest.raises(TypeError):
        store.set_clearances("vic", [], ticket="approved")


def test_change_tickets_are_constructed_only_in_sanctioned_modules():
    """`ChangeTicket(` is the greppable escape hatch, policed like `as_author`
    (precedent: tests/test_audience.py). Constructing one anywhere else in the
    package is a new bypass and fails here. The factories are policed the same
    way, each pinned to the single module its docstring names."""
    root = Path(__file__).resolve().parents[1] / "laurelin"
    constructor_allowed = {root / "core" / "approvals.py"}
    factory_allowed = {
        "local_ticket": {root / "core" / "approvals.py", root / "cli.py"},
        "scim_ticket": {root / "core" / "approvals.py", root / "api" / "scim_routes.py"},
        "identity_ticket": {root / "core" / "approvals.py", root / "api" / "auth_routes.py"},
        "flow_output_ticket": {
            root / "core" / "approvals.py",
            root / "transforms" / "flow_governance.py",
        },
    }
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "ChangeTicket" and path not in constructor_allowed:
                offenders.append(f"{path}:{node.lineno} ChangeTicket(")
            if name in factory_allowed and path not in factory_allowed[name]:
                offenders.append(f"{path}:{node.lineno} {name}(")
    assert offenders == [], f"ticket constructed outside sanctioned modules: {offenders}"


# ---------------------------------------------------------------------------
# REST path enumeration: in second-approver mode every loosening queues and
# the state is untouched until a different admin approves
# ---------------------------------------------------------------------------

def test_rest_dataset_grants_route_queues_a_loosening(clients, store):
    app, root, root2, vic = clients
    assert root.put("/api/v1/datasets/secret_ds/permissions",
                    json={"grants": [{"subject_kind": "user", "subject": "vic",
                                      "can_view": True}]}).status_code == 200
    _enable_second(root)
    # Emptying the list falls back to RBAC default-open — a loosening.
    r = root.put("/api/v1/datasets/secret_ds/permissions", json={"grants": []})
    assert r.status_code == 202, r.text
    assert r.json()["queued"] is True
    assert [g["subject"] for g in store.grants_for_dataset("secret_ds")] == ["vic"]
    assert len(_pending(root)) == 1


def test_rest_ontology_grants_route_queues_a_loosening(clients, store):
    app, root, root2, vic = clients
    assert root.put("/api/v1/ontology/permissions/secret_obj",
                    json={"grants": [{"subject_kind": "user", "subject": "vic",
                                      "can_view": True}]}).status_code == 200
    _enable_second(root)
    r = root.put("/api/v1/ontology/permissions/secret_obj", json={"grants": []})
    assert r.status_code == 202
    assert [g["subject"] for g in store.grants_for_type("secret_obj")] == ["vic"]


def test_rest_policy_route_queues_a_loosening(clients, store):
    app, root, root2, vic = clients
    assert root.put("/api/v1/datasets/secret_ds/policy",
                    json={"column_masks": [{"column": "tenant", "mode": "redact"}]}
                    ).status_code == 200
    _enable_second(root)
    # Dropping the mask un-masks a column for every non-admin: loosening.
    r = root.put("/api/v1/datasets/secret_ds/policy", json={"column_masks": []})
    assert r.status_code == 202
    assert store.get_dataset_policy("secret_ds") is not None


def test_rest_markings_route_queues_a_loosening(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/markings", json={"name": "pii"})
    assert root.put("/api/v1/datasets/secret_ds/markings",
                    json={"markings": ["pii"]}).status_code == 200
    _enable_second(root)
    # Removing the marking lets every uncleared user back in: loosening.
    r = root.put("/api/v1/datasets/secret_ds/markings", json={"markings": []})
    assert r.status_code == 202
    assert store.get_explicit_markings("secret_ds") == ["pii"]


def test_rest_marking_delete_route_queues_workspace_wide_loosening(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/markings", json={"name": "pii"})
    root.put("/api/v1/datasets/secret_ds/markings", json={"markings": ["pii"]})
    _enable_second(root)
    r = root.delete("/api/v1/markings/pii")
    assert r.status_code == 202
    assert store.marking_exists("pii")


def test_rest_clearances_route_queues_a_loosening(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/markings", json={"name": "pii"})
    root.put("/api/v1/datasets/secret_ds/markings", json={"markings": ["pii"]})
    _enable_second(root)
    r = root.put("/api/v1/users/vic/clearances", json={"markings": ["pii"]})
    assert r.status_code == 202
    assert store.get_clearances("vic") == []


def test_rest_group_members_route_queues_a_loosening(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/groups", json={"name": "analysts"})
    root.put("/api/v1/datasets/secret_ds/permissions",
             json={"grants": [{"subject_kind": "group", "subject": "analysts",
                               "can_view": True}]})
    _enable_second(root)
    r = root.put("/api/v1/groups/analysts/members", json={"members": ["vic"]})
    assert r.status_code == 202
    assert store.groups_for_user("vic") == set()


def test_rest_user_role_route_queues_a_promotion(clients):
    app, root, root2, vic = clients
    _enable_second(root)
    r = root.patch("/api/v1/users/vic", json={"role": "editor"})
    assert r.status_code == 202
    assert root.get("/api/v1/auth/status").status_code == 200
    users = {u["username"]: u for u in root.get("/api/v1/users").json()}
    assert users["vic"]["role"] == "viewer"
    # Creating a non-viewer account is a role grant and classifies like one:
    # the account lands as a viewer and the promotion queues.
    r = root.post("/api/v1/users", json={"username": "newadmin",
                                         "password": "password123", "role": "admin"})
    assert r.status_code == 202
    users = {u["username"]: u for u in root.get("/api/v1/users").json()}
    assert users["newadmin"]["role"] == "viewer"


def test_workspace_member_store_path_is_ticketed_and_classified(tmp_path):
    """The multi-workspace membership write shares the chokepoint through
    inheritance (ControlStore extends MetadataStore): the store method demands
    a ticket, and the comparator ranks a promotion as loosening. The control
    plane's posture is record-only (second-approver mode is never enabled
    there) — the record, filed in control.db, is the ceremony."""
    control = ControlStore(tmp_path / "control.db")
    control.create_workspace("acme", "Acme")
    diff = policy_diff.classify(
        control, None, [], "workspace_member", "acme:vic",
        {"slug": "acme", "username": "vic", "role": "admin"},
    )
    assert diff.classification == "loosening"
    service = ApprovalService(control, None, users=[], local=False)
    out = service.submit(kind="workspace_member", target="acme:vic",
                         payload={"slug": "acme", "username": "vic", "role": "admin"},
                         actor="super", actor_id="sid")
    assert out.applied  # record-and-self-approve, never a queue on this tier
    assert control.member_role("acme", "vic") == Role.admin
    recorded = control.list_proposals()
    assert recorded and recorded[0]["kind"] == "workspace_member"
    assert recorded[0]["ticket_kind"] == "self_approved"


def test_mcp_set_dataset_grants_hits_the_same_gate_as_rest(clients, store):
    """MCP tools are LaurelinClient wrappers over the same routes — the design
    reason MCP is HTTP-only. In second-approver mode the tool gets the 202
    body verbatim and no side door exists."""
    from laurelin.mcp import LaurelinClient

    app, root, root2, vic = clients
    assert root.put("/api/v1/datasets/secret_ds/permissions",
                    json={"grants": [{"subject_kind": "user", "subject": "vic",
                                      "can_view": True}]}).status_code == 200
    token = root.post("/api/v1/tokens", json={"name": "mcp"}).json()["token"]
    _enable_second(root)
    client = LaurelinClient(token=token, http=TestClient(app))
    out = client.set_dataset_grants("secret_ds", [])
    assert out["queued"] is True and out["proposal_id"].startswith("p_")
    assert [g["subject"] for g in store.grants_for_dataset("secret_ds")] == ["vic"]


def test_scim_group_push_files_an_auto_approved_record_with_actor_scim(
    clients, store, monkeypatch
):
    app, root, root2, vic = clients
    monkeypatch.setenv("LAURELIN_SCIM_TOKEN", "scim-secret")
    _enable_second(root)  # even in second-approver mode, IdP pushes never queue
    scim = TestClient(app)
    h = {"Authorization": "Bearer scim-secret"}
    r = scim.post("/api/v1/scim/v2/Groups", headers=h,
                  json={"displayName": "pushed",
                        "members": [{"value": "vic"}]})
    assert r.status_code == 201
    assert store.groups_for_user("vic") == {"pushed"}
    records = [p for p in store.list_proposals() if p["kind"] == "group_members"]
    assert records and records[0]["proposer"] == "scim"
    assert records[0]["state"] == "approved"
    assert records[0]["ticket_kind"] == "scim_sync"


def test_scim_patch_merge_path_is_ticketed_too(clients, store, monkeypatch):
    """update_group_members — the read-merge-write the original inventory
    missed — carries the same scim ticket and files the same record."""
    app, root, root2, vic = clients
    monkeypatch.setenv("LAURELIN_SCIM_TOKEN", "scim-secret")
    scim = TestClient(app)
    h = {"Authorization": "Bearer scim-secret"}
    scim.post("/api/v1/scim/v2/Groups", headers=h, json={"displayName": "pushed"})
    r = scim.patch("/api/v1/scim/v2/Groups/pushed", headers=h,
                   json={"Operations": [{"op": "add", "path": "members",
                                         "value": [{"value": "vic"}]}]})
    assert r.status_code == 200
    assert store.groups_for_user("vic") == {"pushed"}
    records = [p for p in store.list_proposals()
               if p["kind"] == "group_members" and "PATCH" in p["rationale"]]
    assert records and records[0]["ticket_kind"] == "scim_sync"


def test_import_files_an_import_kind_proposal_record_alongside_its_digest_ceremony(
    tmp_path,
):
    from laurelin.export import (
        ExportOptions,
        ImportOptions,
        export_workspace,
        import_workspace,
    )

    src_ws = Workspace.init(tmp_path / "src", name="src")
    src_store = MetadataStore(src_ws.metadata_path)
    DatasetCatalog(src_ws, src_store).write("d", pa.table({"id": ["1"]}))
    src_store.set_grants_for_dataset("d", [
        Grant(subject_kind=SubjectKind.user, subject="vic",
              can_view=True).model_dump(mode="json")
    ], ticket=_T)
    archive = tmp_path / "x.tar"
    export_workspace(src_ws, src_store, archive, ExportOptions())
    dst_ws = Workspace.init(tmp_path / "dst", name="dst")
    dst_store = MetadataStore(dst_ws.metadata_path)
    report = import_workspace(archive, dst_ws, dst_store, ImportOptions(actor="ada"))
    assert report.applied
    records = [p for p in dst_store.list_proposals() if p["kind"] == "import"]
    assert len(records) == 1
    assert records[0]["ticket_kind"] == "import_confirmed"
    assert records[0]["proposer"] == "ada"
    assert records[0]["payload"]["report_sha256"] == report.report_sha256


def test_import_refuses_governance_rules_under_second_approver_mode(tmp_path):
    """The finding: the import digest ceremony is a one-party confirmation, so
    under second-approver mode a lone admin could import an ``everyone
    can_view can_edit`` grant — auto-applied with an auto-approved record and
    zero pending proposals, walking past the queue every route obeys. The
    import must refuse rather than bypass; the wide grant must not land."""
    from laurelin.export import (
        ExportOptions,
        ImportOptions,
        export_workspace,
        import_workspace,
    )
    from laurelin.export.manifest import ImportRefused

    src_ws = Workspace.init(tmp_path / "src", name="src")
    src_store = MetadataStore(src_ws.metadata_path)
    DatasetCatalog(src_ws, src_store).write("wide_ds", pa.table({"id": ["1"]}))
    src_store.set_grants_for_dataset("wide_ds", [
        Grant(subject_kind=SubjectKind.everyone, subject="",
              can_view=True, can_edit=True).model_dump(mode="json")
    ], ticket=_T)
    archive = tmp_path / "x.tar"
    export_workspace(src_ws, src_store, archive, ExportOptions())

    dst_ws = Workspace.init(tmp_path / "dst", name="dst")
    dst_store = MetadataStore(dst_ws.metadata_path)
    dst_store.set_setting("approvals", {"require_second_approver": True})
    with pytest.raises(ImportRefused, match="[Ss]econd-approver"):
        import_workspace(archive, dst_ws, dst_store, ImportOptions(actor="ada"))
    # Nothing landed and no auto-approved import record was filed.
    assert dst_store.grants_for_dataset("wide_ds") == []
    assert [p for p in dst_store.list_proposals() if p["kind"] == "import"] == []


def test_import_of_data_without_governance_rules_still_applies_under_second_approver(
    tmp_path,
):
    """The refusal is scoped to governance rules: a data-only archive still
    imports while second-approver mode is armed, so the gate does not brick
    ordinary migration."""
    from laurelin.export import (
        ExportOptions,
        ImportOptions,
        export_workspace,
        import_workspace,
    )

    src_ws = Workspace.init(tmp_path / "src2", name="src2")
    src_store = MetadataStore(src_ws.metadata_path)
    DatasetCatalog(src_ws, src_store).write("plain_ds", pa.table({"id": ["1"]}))
    archive = tmp_path / "y.tar"
    export_workspace(src_ws, src_store, archive, ExportOptions())

    dst_ws = Workspace.init(tmp_path / "dst2", name="dst2")
    dst_store = MetadataStore(dst_ws.metadata_path)
    dst_store.set_setting("approvals", {"require_second_approver": True})
    report = import_workspace(archive, dst_ws, dst_store, ImportOptions(actor="ada"))
    assert report.applied
    assert any(d.name == "plain_ds" for d in dst_store.list_datasets())


def test_flow_build_author_grant_passes_as_tightening_without_an_approver(store):
    """restrict_output_to_author never passes through a route (scheduled/CLI
    builds have no request identity) — which is why the gate is on the store.
    Its write is provably tightening, re-verified by the ticket factory, and
    applies even under second-approver mode with nobody to approve."""
    from laurelin.transforms.flow_governance import restrict_output_to_author
    from laurelin.transforms.flow_ir import FlowDef

    store.set_grants_for_dataset("secret_ds", [
        Grant(subject_kind=SubjectKind.user, subject="alice",
              can_view=True).model_dump(mode="json")
    ], ticket=_T)
    store.set_setting("approvals", {"require_second_approver": True})
    flow = FlowDef.from_json({
        "output": "derived", "author": "alice", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": "secret_ds"}}],
    }, name="derived")
    assert restrict_output_to_author(store, flow, "alice") is True
    assert [g["subject"] for g in store.grants_for_dataset("derived")] == ["alice"]
    assert all(p["state"] != "pending" for p in store.list_proposals())


# ---------------------------------------------------------------------------
# The comparator: loosening vs tightening is computed, not guessed
# ---------------------------------------------------------------------------

VIC = User(id="u1", username="vic", role=Role.viewer)
ED = User(id="u2", username="ed", role=Role.editor)
USERS = [VIC, ED]


def _classify(store, perms, kind, target, payload):
    return policy_diff.classify(store, perms, USERS, kind, target, payload)


def test_emptying_a_grant_list_classifies_as_loosening(store, perms):
    store.set_grants_for_dataset("secret_ds", [
        Grant(subject_kind=SubjectKind.user, subject="vic",
              can_view=True).model_dump(mode="json")
    ], ticket=_T)
    # [] => RBAC default-open: ed regains view+edit. Counting grants would
    # call this "fewer rows"; per-user evaluation calls it what it is.
    diff = _classify(store, perms, "dataset_grants", "secret_ds", {"grants": []})
    assert diff.classification == "loosening"
    assert any("ed gains" in g for g in diff.gains)


def test_a_shrunken_grant_list_that_adds_can_edit_classifies_as_loosening(store, perms):
    store.set_grants_for_dataset("secret_ds", [
        Grant(subject_kind=SubjectKind.user, subject="vic",
              can_view=True).model_dump(mode="json"),
        Grant(subject_kind=SubjectKind.user, subject="ed",
              can_view=True).model_dump(mode="json"),
    ], ticket=_T)
    # One grant fewer — but vic (a viewer) gains edit above her RBAC default.
    diff = _classify(store, perms, "dataset_grants", "secret_ds", {"grants": [
        {"subject_kind": "user", "subject": "vic", "can_view": True, "can_edit": True},
    ]})
    assert diff.classification == "loosening"
    assert any("vic gains edit" in g for g in diff.gains)


def test_adding_a_grant_that_locks_others_out_is_tightening(store, perms):
    diff = _classify(store, perms, "dataset_grants", "secret_ds", {"grants": [
        {"subject_kind": "user", "subject": "vic", "can_view": True},
    ]})
    assert diff.classification == "tightening"


def test_deleting_a_marking_classifies_as_loosening_workspace_wide(store, perms):
    store.create_marking("pii")
    store.set_explicit_markings("secret_ds", ["pii"], ticket=_T)
    store.recompute_all_markings()
    diff = _classify(store, perms, "marking_delete", "pii", {})
    assert diff.classification == "loosening"
    assert any("secret_ds" in g for g in diff.gains)


def test_adding_a_mask_is_tightening_and_applies_immediately(clients, store):
    app, root, root2, vic = clients
    _enable_second(root)
    r = root.put("/api/v1/datasets/secret_ds/policy",
                 json={"column_masks": [{"column": "tenant", "mode": "redact"}]})
    assert r.status_code == 200  # a 3am mask addition needs nobody
    assert store.get_dataset_policy("secret_ds") is not None


def test_adding_a_member_to_a_referenced_group_is_loosening(store, perms):
    store.create_group("analysts", "2026-01-01T00:00:00Z")
    store.set_grants_for_dataset("secret_ds", [
        Grant(subject_kind=SubjectKind.group, subject="analysts",
              can_view=True).model_dump(mode="json")
    ], ticket=_T)
    diff = _classify(store, perms, "group_members", "analysts", {"members": ["vic"]})
    assert diff.classification == "loosening"
    assert any("vic gains view" in g for g in diff.gains)


def test_adding_a_member_to_an_unreferenced_group_applies_with_record(
    clients, store
):
    app, root, root2, vic = clients
    root.post("/api/v1/groups", json={"name": "book-club"})
    _enable_second(root)
    # No grant, rule or exemption names the group: a no-op diff is
    # tightening-equivalent and applies — with a record, not a queue.
    r = root.put("/api/v1/groups/book-club/members", json={"members": ["vic"]})
    assert r.status_code == 200
    assert store.groups_for_user("vic") == {"book-club"}
    records = [p for p in store.list_proposals() if p["target"] == "book-club"]
    assert records and records[0]["state"] == "approved"


def test_mask_mode_and_row_column_changes_classify_as_loosening_fail_closed(
    store, perms
):
    store.set_dataset_policy("secret_ds", {
        "row_policy": {"column": "tenant", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["t1"]}]},
        "column_masks": [{"column": "tenant", "mode": "redact", "exempt": []}],
    }, ticket=_T)
    # redact -> hash reveals stable equality classes; no mode lattice, just
    # the fail-closed verdict with its reason.
    diff = _classify(store, perms, "dataset_policy", "secret_ds", {"policy": {
        "row_policy": {"column": "tenant", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["t1"]}]},
        "column_masks": [{"column": "tenant", "mode": "hash", "exempt": []}],
    }})
    assert diff.classification == "loosening"
    assert diff.reason == "incomparable"
    # Row-policy column change: different rows become visible, not comparable.
    diff = _classify(store, perms, "dataset_policy", "secret_ds", {"policy": {
        "row_policy": {"column": "id", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["t1"]}]},
        "column_masks": [{"column": "tenant", "mode": "redact", "exempt": []}],
    }})
    assert diff.classification == "loosening"
    assert diff.reason == "incomparable"


def test_a_user_gaining_row_values_classifies_as_loosening(store, perms):
    store.set_dataset_policy("secret_ds", {
        "row_policy": {"column": "tenant", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["t1"]}]},
        "column_masks": [],
    }, ticket=_T)
    diff = _classify(store, perms, "dataset_policy", "secret_ds", {"policy": {
        "row_policy": {"column": "tenant", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["t1", "t2"]}]},
        "column_masks": [],
    }})
    assert diff.classification == "loosening"
    assert any("t2" in g for g in diff.gains)


def test_clearance_addition_is_loosening_and_removal_is_tightening(store, perms):
    store.create_marking("pii")
    store.set_explicit_markings("secret_ds", ["pii"], ticket=_T)
    store.recompute_all_markings()
    add = _classify(store, perms, "clearances", "vic", {"markings": ["pii"]})
    assert add.classification == "loosening"
    store.set_clearances("vic", ["pii"], ticket=_T)
    remove = _classify(store, perms, "clearances", "vic", {"markings": []})
    assert remove.classification == "tightening"


def test_role_promotion_is_loosening(store, perms):
    up = _classify(store, perms, "user_role", "vic", {"role": "editor"})
    assert up.classification == "loosening"
    down = _classify(store, perms, "user_role", "ed", {"role": "viewer"})
    assert down.classification == "tightening"


# ---------------------------------------------------------------------------
# Disabled subjects are latent loosening: the disabled flag is reversible by an
# ungated identity write, so a grant/clearance/membership naming a disabled
# account must classify against its policy capability, not its zero effective
# access — otherwise disable -> grant -> enable walks past second-approver mode.
# ---------------------------------------------------------------------------

MOLE = User(id="u3", username="mole", role=Role.viewer, disabled=True)


def test_a_grant_naming_only_a_disabled_user_classifies_as_loosening(store, perms):
    # secret_ds locked to ed; adding a view grant for the DISABLED mole widens
    # mole's policy capability even though her account is off right now.
    store.set_grants_for_dataset("secret_ds", [
        Grant(subject_kind=SubjectKind.user, subject="ed",
              can_view=True, can_edit=True).model_dump(mode="json"),
    ], ticket=_T)
    diff = policy_diff.classify(store, perms, [VIC, ED, MOLE], "dataset_grants",
                                "secret_ds", {"grants": [
        {"subject_kind": "user", "subject": "ed", "can_view": True, "can_edit": True},
        {"subject_kind": "user", "subject": "mole", "can_view": True, "can_edit": True},
    ]})
    assert diff.classification == "loosening"
    assert any("mole gains" in g for g in diff.gains)


def test_a_clearance_added_to_a_disabled_user_classifies_as_loosening(store, perms):
    store.create_marking("pii")
    store.set_explicit_markings("secret_ds", ["pii"], ticket=_T)
    store.recompute_all_markings()
    diff = policy_diff.classify(store, perms, [VIC, ED, MOLE], "clearances",
                                "mole", {"markings": ["pii"]})
    assert diff.classification == "loosening"


def test_disable_grant_enable_cannot_bypass_second_approver_mode(clients, store):
    """The finding: with second-approver mode ON, a lone admin disables a
    target, grants it access (which used to apply as a phantom tightening), then
    re-enables it (an ungated identity write) — access live, no approver. The
    grant must queue exactly like a grant to an enabled user; re-enabling must
    not surface unreviewed access."""
    app, root, root2, vic = clients
    root.post("/api/v1/users", json={"username": "mole", "password": "password123",
                                     "role": "viewer"})
    root.put("/api/v1/datasets/secret_ds/permissions",
             json={"grants": [{"subject_kind": "user", "subject": "ed",
                               "can_view": True, "can_edit": True}]})
    _enable_second(root)
    # disable mole, grant to the disabled account, re-enable
    assert root.patch("/api/v1/users/mole", json={"disabled": True}).status_code == 200
    r = root.put("/api/v1/datasets/secret_ds/permissions",
                 json={"grants": [
                     {"subject_kind": "user", "subject": "ed", "can_view": True, "can_edit": True},
                     {"subject_kind": "user", "subject": "mole", "can_view": True, "can_edit": True}]})
    assert r.status_code == 202, r.text  # queued, not applied
    assert root.patch("/api/v1/users/mole", json={"disabled": False}).status_code == 200
    # mole has NO access: the loosening is still pending, unapproved.
    mole = TestClient(app)
    mole.post("/api/v1/auth/login", json={"username": "mole", "password": "password123"})
    # 404 not 403: hidden reads as nonexistent (deliberate; see test_dataset_acls)
    assert mole.get("/api/v1/datasets/secret_ds").status_code == 404
    assert any(p["kind"] == "dataset_grants" and p["target"] == "secret_ds"
               for p in _pending(root))


# ---------------------------------------------------------------------------
# Staleness: approve applies what is true now, or refuses
# ---------------------------------------------------------------------------

def test_a_proposal_reclassifies_against_current_state_at_apply_time(clients, store):
    app, root, root2, vic = clients
    root.put("/api/v1/datasets/secret_ds/permissions",
             json={"grants": [{"subject_kind": "user", "subject": "vic",
                               "can_view": True}]})
    _enable_second(root)
    r = root.put("/api/v1/datasets/secret_ds/permissions", json={"grants": []})
    assert r.status_code == 202
    pid = r.json()["proposal_id"]
    # An intervening (ungated) change moves the comparator's inputs: a new
    # viewer account joins the population the emptied list would open up to.
    assert root.post("/api/v1/users", json={"username": "newguy",
                                            "password": "password123",
                                            "role": "viewer"}).status_code == 200
    r = root2.post(f"/api/v1/proposals/{pid}/approve")
    assert r.status_code == 409
    assert "superseded" in r.text
    p = root.get(f"/api/v1/proposals/{pid}").json()
    assert p["state"] == "superseded"
    # Untouched: the stale summary was never applied.
    assert [g["subject"] for g in store.grants_for_dataset("secret_ds")] == ["vic"]


def test_an_approved_proposal_applies_and_audits_with_ticket_kind(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/markings", json={"name": "pii"})
    root.put("/api/v1/datasets/secret_ds/markings", json={"markings": ["pii"]})
    _enable_second(root)
    r = root.put("/api/v1/users/vic/clearances", json={"markings": ["pii"]})
    pid = r.json()["proposal_id"]
    r = root2.post(f"/api/v1/proposals/{pid}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "approved"
    assert store.get_clearances("vic") == ["pii"]
    actions = {e["action"]: e for e in
               [json.loads(json.dumps(a)) for a in
                (root.get("/api/v1/audit?limit=50").json())]}
    assert "proposal_approved" in actions
    assert actions["clearances_set"]["details"]["ticket_kind"] == "approved"
    assert actions["clearances_set"]["details"]["proposal_id"] == pid


def test_a_rejected_proposal_never_applies(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/markings", json={"name": "pii"})
    root.put("/api/v1/datasets/secret_ds/markings", json={"markings": ["pii"]})
    _enable_second(root)
    pid = root.put("/api/v1/users/vic/clearances",
                   json={"markings": ["pii"]}).json()["proposal_id"]
    r = root2.post(f"/api/v1/proposals/{pid}/reject", json={"reason": "no need"})
    assert r.status_code == 200
    assert store.get_clearances("vic") == []
    assert root.get(f"/api/v1/proposals/{pid}").json()["state"] == "rejected"


# ---------------------------------------------------------------------------
# Single-admin behaviour: the record is the product, nothing deadlocks
# ---------------------------------------------------------------------------

def test_a_single_admin_workspace_self_approves_with_a_record(clients, store):
    app, root, root2, vic = clients
    root.put("/api/v1/datasets/secret_ds/permissions",
             json={"grants": [{"subject_kind": "user", "subject": "vic",
                               "can_view": True}]})
    r = root.put("/api/v1/datasets/secret_ds/permissions", json={"grants": []})
    assert r.status_code == 200  # wire-compatible: applied, not queued
    assert store.grants_for_dataset("secret_ds") == []
    records = [p for p in store.list_proposals()
               if p["kind"] == "dataset_grants" and p["classification"] == "loosening"]
    assert records
    assert records[0]["state"] == "approved"
    assert records[0]["ticket_kind"] == "self_approved"
    assert records[0]["decided_by"] == records[0]["proposer"] == "root"


def test_no_auth_mode_auto_approves_with_ticket_local(ws):
    app = create_app(ws, no_auth=True)
    client = TestClient(app)
    # Real accounts can coexist with --no-auth (an imported workspace, or an
    # authed one restarted locally); they give the comparator a population.
    client.post("/api/v1/users", json={"username": "vic", "password": "password123",
                                       "role": "viewer"})
    client.post("/api/v1/users", json={"username": "ed", "password": "password123",
                                       "role": "editor"})
    r = client.put("/api/v1/datasets/secret_ds/permissions",
                   json={"grants": [{"subject_kind": "user", "subject": "vic",
                                     "can_view": True}]})
    assert r.status_code == 200
    # Emptying the list is a loosening (ed regains access) — and it still
    # applies immediately: anything else deadlocks the only local mode.
    r = client.put("/api/v1/datasets/secret_ds/permissions", json={"grants": []})
    assert r.status_code == 200
    store = MetadataStore(ws.metadata_path)
    loosenings = [p for p in store.list_proposals()
                  if p["classification"] == "loosening"
                  and p["kind"] == "dataset_grants"]
    assert loosenings and loosenings[0]["ticket_kind"] == "local"


def test_second_approver_mode_refuses_to_enable_with_one_active_admin(ws):
    app = create_app(ws)
    root = TestClient(app)
    root.post("/api/v1/auth/setup", json=ADMIN_CREDS)
    root.post("/api/v1/auth/login", json=ADMIN_CREDS)
    r = root.put("/api/v1/settings/approvals", json={"require_second_approver": True})
    assert r.status_code == 409
    assert "at least 2" in r.json()["detail"]


def test_the_proposer_cannot_approve_their_own_proposal(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/markings", json={"name": "pii"})
    root.put("/api/v1/datasets/secret_ds/markings", json={"markings": ["pii"]})
    _enable_second(root)
    pid = root.put("/api/v1/users/vic/clearances",
                   json={"markings": ["pii"]}).json()["proposal_id"]
    r = root.post(f"/api/v1/proposals/{pid}/approve")
    assert r.status_code == 409
    assert "proposer" in r.json()["detail"]
    assert store.get_clearances("vic") == []
    # ...compared by user id, and a different admin succeeds.
    assert root2.post(f"/api/v1/proposals/{pid}/approve").status_code == 200


def test_disabling_second_approver_mode_itself_queues(clients):
    app, root, root2, vic = clients
    _enable_second(root)
    r = root.put("/api/v1/settings/approvals", json={"require_second_approver": False})
    assert r.status_code == 202
    assert root.get("/api/v1/settings/approvals").json() == {
        "require_second_approver": True  # still on: the mode cannot be
    }                                    # switched off by the person it constrains
    pid = r.json()["proposal_id"]
    assert root2.post(f"/api/v1/proposals/{pid}/approve").status_code == 200
    assert root.get("/api/v1/settings/approvals").json() == {
        "require_second_approver": False
    }


def test_default_posture_keeps_existing_governance_routes_returning_200_applied(
    clients, store
):
    app, root, root2, vic = clients
    assert root.put("/api/v1/datasets/secret_ds/permissions",
                    json={"grants": [{"subject_kind": "user", "subject": "vic",
                                      "can_view": True}]}).status_code == 200
    assert root.put("/api/v1/datasets/secret_ds/policy",
                    json={"column_masks": [{"column": "tenant",
                                            "mode": "redact"}]}).status_code == 200
    assert root.post("/api/v1/markings", json={"name": "pii"}).status_code == 200
    assert root.put("/api/v1/datasets/secret_ds/markings",
                    json={"markings": ["pii"]}).status_code == 200
    assert root.put("/api/v1/users/vic/clearances",
                    json={"markings": ["pii"]}).status_code == 200
    assert root.delete("/api/v1/markings/pii").status_code == 200
    assert root.patch("/api/v1/users/vic", json={"role": "editor"}).status_code == 200
    users = {u["username"]: u for u in root.get("/api/v1/users").json()}
    assert users["vic"]["role"] == "editor"


# ---------------------------------------------------------------------------
# R2 on proposal contents
# ---------------------------------------------------------------------------

def test_pending_proposal_contents_serialize_admin_only(clients, store):
    """payload_json embeds row-rule VALUES — governed data values. Every
    content field is OPERATIONAL on an admin-authored record, so the one
    serializer withholds all of it below admin; asserted on raw text so a
    value in any stray field fails."""
    app, root, root2, vic = clients
    assert root.put("/api/v1/datasets/secret_ds/policy", json={
        "row_policy": {"column": "tenant", "rules": [
            {"subject_kind": "user", "subject": "vic",
             "values": ["tenant-42-secret"]}]},
        "column_masks": [],
    }).status_code == 200
    p = store.list_proposals()[0]
    assert "tenant-42-secret" in json.dumps(p["payload"])  # the record has it
    editor_view = serialize.dump_as(ProposalInfo(**p), Role.editor)
    text = json.dumps(editor_view)
    assert "tenant-42-secret" not in text
    assert "payload" not in editor_view and "diff" not in editor_view
    assert "rationale" not in editor_view and "target" not in editor_view
    admin_view = serialize.dump_as(ProposalInfo(**p), Role.admin)
    assert "tenant-42-secret" in json.dumps(admin_view)
    # And the HTTP surface is admin-gated outright.
    assert vic.get("/api/v1/proposals").status_code == 403


def test_withdraw_is_proposer_only_and_closes_the_proposal(clients, store):
    app, root, root2, vic = clients
    root.post("/api/v1/markings", json={"name": "pii"})
    root.put("/api/v1/datasets/secret_ds/markings", json={"markings": ["pii"]})
    _enable_second(root)
    pid = root.put("/api/v1/users/vic/clearances",
                   json={"markings": ["pii"]}).json()["proposal_id"]
    assert root2.post(f"/api/v1/proposals/{pid}/withdraw").status_code == 409
    assert root.post(f"/api/v1/proposals/{pid}/withdraw").status_code == 200
    assert root.get(f"/api/v1/proposals/{pid}").json()["state"] == "withdrawn"
    assert store.get_clearances("vic") == []


def test_cli_role_change_carries_a_local_ticket_and_stays_out_of_the_queue(store):
    """The CLI opens the store with the operator's filesystem privileges —
    the documented scope boundary. Its role writes declare kind='local'."""
    from laurelin.core.auth import AuthService

    auth = AuthService(store)
    auth.create_user("carol", "password123", Role.viewer)
    auth.update_user("carol", role=Role.editor, actor="cli",
                     ticket=local_ticket("cli"))
    assert auth.get_user("carol").role == Role.editor
