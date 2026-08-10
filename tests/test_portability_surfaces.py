"""The two front doors onto workspace portability: the CLI and the HTTP API.

The core module is tested elsewhere (test_workspace_export / _import /
_secrets / _governance_roundtrip). What is tested here is everything a caller
can reach: who is allowed to ask, what a refusal looks like from the outside,
and whether the archive actually crosses the wire.

Authorization gets the most room, because an export is a bulk read of every
unmasked row in the workspace and an import is a bulk write plus a code-
delivery channel. Both are exfiltration primitives if the guard is wrong, and a
guard is only as real as the test that a viewer hits it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from laurelin.api import create_app, create_server_app
from laurelin.catalog import DatasetCatalog
from laurelin.cli import app as cli
from laurelin.core.config import Workspace
from laurelin.core.control import ControlStore
from laurelin.core.db import MetadataStore

runner = CliRunner()

ADMIN = {"username": "root", "password": "trustno1!"}
PASSWORD = "sup3rsecret"

CREDENTIAL_PIPELINE = (
    "from laurelin.transforms import transform, Input, Output\n"
    "DSN = 'postgresql://svc:hunter2@pg-prod-3.internal:5432/crm'\n"
    "@transform(output=Output('clean'), s=Input('sales'))\n"
    "def clean(s):\n    return s\n"
)

HARMLESS_PIPELINE = (
    "from laurelin.transforms import transform, Input, Output\n"
    "@transform(output=Output('clean'), s=Input('sales'))\n"
    "def clean(s):\n    return s\n"
)


# --------------------------------------------------------------------------- fixtures


def _seed(root: Path, *, pipeline: str = HARMLESS_PIPELINE, name: str = "Acme") -> Workspace:
    ws = Workspace.init(root, name=name)
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write(
        "sales",
        pa.table({"region": ["eu", "us"], "ssn": ["111-22-3333", "444-55-6666"]}),
    )
    (ws.pipelines_dir / "p.py").write_text(pipeline)
    (ws.ontology_dir / "o.yml").write_text(
        "object_types:\n"
        "  - api_name: sale\n"
        "    backing_dataset: sales\n"
        "    primary_key: region\n"
        "    properties:\n"
        "      region: {type: string}\n"
    )
    return ws


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    return _seed(tmp_path / "src")


@pytest.fixture()
def empty_ws(tmp_path) -> Workspace:
    return Workspace.init(tmp_path / "dest", name="Dest")


@pytest.fixture()
def app(ws):
    return create_app(ws)


@pytest.fixture()
def admin(app) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/v1/auth/setup", json=ADMIN).status_code == 200
    assert client.post("/api/v1/auth/login", json=ADMIN).status_code == 200
    return client


def _user(admin_client: TestClient, app, username: str, role: str) -> TestClient:
    r = admin_client.post(
        "/api/v1/users",
        json={"username": username, "password": PASSWORD, "role": role},
    )
    assert r.status_code == 200, r.text
    client = TestClient(app)
    assert client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    ).status_code == 200
    return client


def _cli_user(workspace: Workspace, username: str, role: str) -> None:
    result = runner.invoke(
        cli,
        ["users", "create", username, "--role", role, "--password", PASSWORD,
         "-w", str(workspace.root)],
    )
    assert result.exit_code == 0, result.output


def _archive_bytes(client: TestClient, **params) -> bytes:
    with client.stream("GET", "/api/v1/workspace/export", params=params) as response:
        assert response.status_code == 200, response.read()
        return b"".join(response.iter_bytes())


def _members(raw: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        return [m.name for m in tar]


def _member_text(raw: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        for member in tar:
            if member.name == name:
                return tar.extractfile(member).read().decode()
    raise AssertionError(f"no member {name!r}")


# --------------------------------------------------------------------------- CLI


def test_the_cli_writes_an_archive_only_its_owner_can_read(ws, tmp_path):
    """An archive holds unmasked rows and a map of the deployment. 0644 on a
    shared box would hand both to every local account."""
    out = tmp_path / "out.tar"
    result = runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    assert result.exit_code == 0, result.output
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_an_export_piped_into_an_import_reconstructs_the_workspace(ws, empty_ws, tmp_path):
    """`laurelin export - | laurelin import -` is the reason the format is a
    tar stream rather than a zip, so it is checked end to end through two real
    processes rather than two function calls."""
    env = {**os.environ, "PATH": os.environ["PATH"]}
    exporter = subprocess.Popen(
        [sys.executable, "-m", "laurelin.cli", "export", "-", "-w", str(ws.root)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )
    importer = subprocess.run(
        [sys.executable, "-m", "laurelin.cli", "import", "-", "-w", str(empty_ws.root)],
        stdin=exporter.stdout, capture_output=True, env=env,
    )
    exporter.stdout.close()
    assert exporter.wait() == 0
    assert importer.returncode == 0, importer.stderr.decode()
    store = MetadataStore(empty_ws.metadata_path)
    assert [d.name for d in store.list_datasets()] == ["sales"]


def test_a_refusal_exits_2_so_a_runbook_can_tell_it_from_a_failure(tmp_path):
    """Exit 1 means "it broke, retry or escalate"; exit 2 means "it declined,
    and the message names the flag". Collapsing them makes automation guess."""
    ws = _seed(tmp_path / "src", pipeline=CREDENTIAL_PIPELINE)
    result = runner.invoke(cli, ["export", str(tmp_path / "o.tar"), "-w", str(ws.root)])
    assert result.exit_code == 2, result.output
    assert "--allow-content-warnings" in result.output
    assert not (tmp_path / "o.tar").exists()

    allowed = runner.invoke(
        cli,
        ["export", str(tmp_path / "o.tar"), "-w", str(ws.root), "--allow-content-warnings"],
    )
    assert allowed.exit_code == 0, allowed.output


def test_the_cli_notices_multi_workspace_mode_and_refuses_to_guess(tmp_path):
    """A workspace under a control plane cannot be exported as if roles lived
    inside it: in multi mode the effective role is workspace_members.role, and
    admin bypasses every gate. Assuming "single" would ship a
    governance-incomplete archive without saying so."""
    root = tmp_path / "srv"
    root.mkdir()
    control = ControlStore(root / "control.db")
    control.create_workspace("alpha", "Alpha")
    control.set_member("alpha", "vic", "admin")
    ws = _seed(root / "alpha")

    refused = runner.invoke(cli, ["export", str(tmp_path / "a.tar"), "-w", str(ws.root)])
    assert refused.exit_code == 2, refused.output
    assert "multi-workspace mode" in refused.output

    ok = runner.invoke(
        cli,
        ["export", str(tmp_path / "a.tar"), "-w", str(ws.root), "--include-membership"],
    )
    assert ok.exit_code == 0, ok.output
    raw = (tmp_path / "a.tar").read_bytes()
    assert "tables/workspace_members.jsonl" in _members(raw)


def test_restricting_datasets_restricts_data_and_never_governance(tmp_path):
    """--dataset is a data filter. Governance is a closure — a partial one
    cannot be proven — so every dataset's catalog row and rules still travel,
    and the ones left out are marked rather than dropped."""
    ws = _seed(tmp_path / "src")
    DatasetCatalog(ws, MetadataStore(ws.metadata_path)).write(
        "hr", pa.table({"id": ["a"]})
    )
    out = tmp_path / "out.tar"
    assert runner.invoke(
        cli, ["export", str(out), "-w", str(ws.root), "--dataset", "sales"]
    ).exit_code == 0

    raw = out.read_bytes()
    parts = [n for n in _members(raw) if n.startswith("data/")]
    assert parts and all(n.startswith("data/sales/") for n in parts)

    rows = [
        json.loads(line)
        for line in _member_text(raw, "tables/datasets.jsonl").splitlines()
        if line
    ]
    assert {r["name"] for r in rows} == {"sales", "hr"}


def test_import_refuses_a_populated_workspace_and_names_the_flag(ws, empty_ws, tmp_path):
    out = tmp_path / "out.tar"
    assert runner.invoke(cli, ["export", str(out), "-w", str(ws.root)]).exit_code == 0
    assert runner.invoke(cli, ["import", str(out), "-w", str(empty_ws.root)]).exit_code == 0

    again = runner.invoke(cli, ["import", str(out), "-w", str(empty_ws.root)])
    assert again.exit_code == 2, again.output
    assert "--merge" in again.output


def test_a_merge_applies_only_with_the_digest_of_the_report_that_was_printed(
    ws, empty_ws, tmp_path
):
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    runner.invoke(cli, ["import", str(out), "-w", str(empty_ws.root)])

    merge = ["import", str(out), "-w", str(empty_ws.root), "--merge",
             "--rename-prefix", "copy_"]

    unconfirmed = runner.invoke(cli, merge)
    assert unconfirmed.exit_code == 2
    assert "digest of the report" in unconfirmed.output
    digest = unconfirmed.output.split("--confirm ")[1].split(".")[0].strip()

    wrong = runner.invoke(cli, [*merge, "--confirm", "0" * 64])
    assert wrong.exit_code == 2, wrong.output

    applied = runner.invoke(cli, [*merge, "--confirm", digest])
    assert applied.exit_code == 0, applied.output
    names = {d.name for d in MetadataStore(empty_ws.metadata_path).list_datasets()}
    assert names == {"sales", "copy_sales"}


def test_the_import_report_is_written_0600_because_it_names_every_principal(
    ws, empty_ws, tmp_path
):
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    report = tmp_path / "report.json"
    result = runner.invoke(
        cli, ["import", str(out), "-w", str(empty_ws.root), "--report", str(report)]
    )
    assert result.exit_code == 0, result.output
    assert oct(report.stat().st_mode & 0o777) == "0o600"
    assert json.loads(report.read_text())["applied"] is True


def test_a_dry_run_writes_nothing_and_hands_back_the_confirm_token(ws, empty_ws, tmp_path):
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    result = runner.invoke(
        cli, ["import", str(out), "-w", str(empty_ws.root), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Nothing was written" in result.output
    assert MetadataStore(empty_ws.metadata_path).list_datasets() == []


def test_verify_governance_refuses_a_baseline_that_carries_no_fingerprint(
    ws, empty_ws, tmp_path
):
    """A baseline without a matrix cannot prove anything, and quietly reporting
    "identical: 0 cells" would be the most misleading possible pass."""
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    result = runner.invoke(
        cli, ["verify-governance", "--baseline", str(out), "-w", str(empty_ws.root)]
    )
    assert result.exit_code == 1
    assert "--fingerprint" in result.output


def test_verify_governance_names_the_principals_an_unbound_import_did_not_create(
    ws, empty_ws, tmp_path
):
    """Before any rebind the destination has nobody, and the command must say
    so rather than quietly comparing a smaller matrix and calling it a match."""
    _cli_user(ws, "vic", "viewer")
    out = tmp_path / "out.tar"
    assert runner.invoke(
        cli, ["export", str(out), "-w", str(ws.root), "--fingerprint"]
    ).exit_code == 0
    runner.invoke(cli, ["import", str(out), "-w", str(empty_ws.root)])

    result = runner.invoke(
        cli, ["verify-governance", "--baseline", str(out), "-w", str(empty_ws.root)]
    )
    assert "do not exist here" in result.output
    assert "- vic" in result.output


def test_verify_governance_reports_the_cells_where_two_workspaces_disagree(
    ws, empty_ws, tmp_path
):
    _cli_user(ws, "vic", "viewer")
    out = tmp_path / "out.tar"
    assert runner.invoke(
        cli, ["export", str(out), "-w", str(ws.root), "--fingerprint"]
    ).exit_code == 0
    assert runner.invoke(cli, ["import", str(out), "-w", str(empty_ws.root)]).exit_code == 0
    _cli_user(empty_ws, "vic", "viewer")  # the explicit rebind the report asks for

    same = runner.invoke(
        cli, ["verify-governance", "--baseline", str(out), "-w", str(empty_ws.root)]
    )
    assert same.exit_code == 0, same.output
    assert "Identical" in same.output

    # Change one masking rule at the destination and the same command must
    # notice — a fingerprint that only compared row counts would not.
    MetadataStore(empty_ws.metadata_path).set_dataset_policy(
        "sales",
        {"row_policy": None, "column_masks": [{"column": "ssn", "mode": "redact"}]},
    )
    differs = runner.invoke(
        cli, ["verify-governance", "--baseline", str(out), "-w", str(empty_ws.root)]
    )
    assert differs.exit_code == 1, differs.output
    assert "vic|sales" in differs.output


def test_a_cli_build_refuses_until_imported_pipelines_are_acknowledged(
    ws, empty_ws, tmp_path
):
    """collect_transforms EXECs every file in pipelines/. The CLI is the same
    front door as the API, not a way around the acknowledgement."""
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    runner.invoke(cli, ["import", str(out), "-w", str(empty_ws.root)])

    blocked = runner.invoke(cli, ["build", "-w", str(empty_ws.root)])
    assert blocked.exit_code == 2, blocked.output
    assert "acknowledged" in blocked.output


# --------------------------------------------------------------------------- HTTP authz


PORTABILITY_READS = [
    "/api/v1/workspace/export/preview",
    "/api/v1/workspace/export",
    "/api/v1/workspace/import/report",
    "/api/v1/workspace/import/state",
]


@pytest.mark.parametrize("route", PORTABILITY_READS)
def test_no_portability_route_answers_an_unauthenticated_caller(app, admin, route):
    assert TestClient(app).get(route).status_code == 401


@pytest.mark.parametrize(
    "route",
    [
        "/api/v1/workspace/import",
        "/api/v1/workspace/import/from-path",
        "/api/v1/workspace/import/acknowledge-pipelines",
        "/api/v1/workspace/governance/fingerprint",
    ],
)
def test_no_portability_write_route_answers_an_unauthenticated_caller(app, admin, route):
    assert TestClient(app).post(route, json={}).status_code == 401


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize("route", PORTABILITY_READS)
def test_only_an_admin_may_read_anything_about_the_export(app, admin, role, route):
    """Including the preview. The preview enumerates every table, every dataset
    and the location of every withheld credential — a map of the deployment,
    even though it carries no secret value."""
    client = _user(admin, app, f"{role}1", role)
    assert client.get(route).status_code == 403


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_only_an_admin_may_import(app, admin, role, ws, tmp_path):
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    client = _user(admin, app, f"{role}2", role)
    response = client.post(
        "/api/v1/workspace/import",
        files={"file": ("a.tar", out.read_bytes(), "application/x-tar")},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_only_an_admin_may_acknowledge_imported_pipelines(app, admin, role):
    client = _user(admin, app, f"{role}3", role)
    assert client.post(
        "/api/v1/workspace/import/acknowledge-pipelines"
    ).status_code == 403


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_only_an_admin_may_compute_a_governance_fingerprint(app, admin, role):
    """The matrix answers "what would this principal see" for every principal,
    which is a read of other people's access, not of your own."""
    client = _user(admin, app, f"{role}4", role)
    assert client.post(
        "/api/v1/workspace/governance/fingerprint", json={"principals": []}
    ).status_code == 403


def test_the_export_is_admin_because_an_admin_can_already_read_the_unmasked_rows(
    app, admin
):
    """The whole authorization argument in one test: a viewer's masked cell is
    '***' through the front door, the admin's is not, and the archive carries
    what the admin can already see. If a viewer could export, they would read
    around their own mask; if an admin could not, the gate would be stopping
    nothing."""
    assert admin.put(
        "/api/v1/datasets/sales/policy",
        json={"column_masks": [{"column": "ssn", "mode": "redact"}]},
    ).status_code == 200
    viewer = _user(admin, app, "vic", "viewer")

    masked = viewer.get("/api/v1/datasets/sales/rows").json()["rows"]
    assert {r["ssn"] for r in masked} == {"***"}
    unmasked = admin.get("/api/v1/datasets/sales/rows").json()["rows"]
    assert "111-22-3333" in {r["ssn"] for r in unmasked}

    assert viewer.get("/api/v1/workspace/export").status_code == 403
    assert b"111-22-3333" in _archive_bytes(admin)


def test_import_is_refused_when_the_server_locks_pipeline_authoring(ws, tmp_path):
    """--lock-pipelines means pipeline files may only be edited on disk. An
    archive carries pipelines/*.py and those are exec'd on every build, so an
    HTTP import would make the flag bypassable by uploading a tarball."""
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    locked = create_app(_seed(tmp_path / "locked"), lock_pipelines=True)
    client = TestClient(locked)
    client.post("/api/v1/auth/setup", json=ADMIN)
    client.post("/api/v1/auth/login", json=ADMIN)

    response = client.post(
        "/api/v1/workspace/import",
        files={"file": ("a.tar", out.read_bytes(), "application/x-tar")},
    )
    assert response.status_code == 403
    assert "--lock-pipelines" in response.json()["detail"]
    assert client.post(
        "/api/v1/workspace/import/acknowledge-pipelines"
    ).status_code == 403


# --------------------------------------------------------------------------- multi mode


@pytest.fixture()
def server(tmp_path):
    root = tmp_path / "srv"
    app = create_server_app(root)
    client = TestClient(app)
    assert client.post("/api/v1/auth/setup", json=ADMIN).status_code == 200
    assert client.post("/api/v1/auth/login", json=ADMIN).status_code == 200
    assert client.post("/api/v1/workspaces", json={"slug": "alpha"}).status_code == 200
    return app, client, root


def _workspace_admin(app, superadmin) -> TestClient:
    superadmin.post(
        "/api/v1/users",
        json={"username": "wsadm", "password": PASSWORD, "role": "viewer"},
    )
    superadmin.put(
        "/api/v1/workspaces/alpha/members", json={"username": "wsadm", "role": "admin"}
    )
    client = TestClient(app)
    assert client.post(
        "/api/v1/auth/login", json={"username": "wsadm", "password": PASSWORD}
    ).status_code == 200
    client.headers["X-Laurelin-Workspace"] = "alpha"
    return client


def test_a_workspace_admin_cannot_export_the_control_planes_membership(server):
    """workspace_members lives outside the workspace and is superadmin-only
    through GET /workspaces/{slug}/members. Letting a tenant admin request it
    inside an export would be the same read through a side door."""
    app, superadmin, _ = server
    wsadm = _workspace_admin(app, superadmin)

    refused = wsadm.get(
        "/api/v1/workspace/export/preview", params={"include_membership": True}
    )
    assert refused.status_code == 403
    assert "server administrator" in refused.json()["detail"]

    # …and the same admin can still take a governance-incomplete export.
    assert wsadm.get(
        "/api/v1/workspace/export/preview", params={"include_membership": False}
    ).status_code == 200


def test_multi_mode_refuses_an_export_that_never_decided_about_membership(server):
    app, superadmin, _ = server
    superadmin.headers["X-Laurelin-Workspace"] = "alpha"
    response = superadmin.get("/api/v1/workspace/export/preview")
    assert response.status_code == 409
    assert "--include-membership" in response.json()["detail"]


def test_a_superadmins_export_carries_membership_for_that_slug_only(server):
    app, superadmin, _ = server
    superadmin.post("/api/v1/workspaces", json={"slug": "beta"})
    superadmin.post(
        "/api/v1/users", json={"username": "vic", "password": PASSWORD, "role": "viewer"}
    )
    superadmin.put(
        "/api/v1/workspaces/alpha/members", json={"username": "vic", "role": "editor"}
    )
    superadmin.put(
        "/api/v1/workspaces/beta/members", json={"username": "vic", "role": "admin"}
    )
    superadmin.headers["X-Laurelin-Workspace"] = "alpha"

    raw = _archive_bytes(superadmin, include_membership=True, metadata_only=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        rows = []
        for member in tar:
            if member.name == "tables/workspace_members.jsonl":
                payload = tar.extractfile(member).read().decode()
                rows = [json.loads(line) for line in payload.splitlines() if line]
    assert rows == [{"slug": "alpha", "username": "vic", "role": "editor"}]


def test_importing_a_server_side_path_needs_a_superadmin(server, tmp_path):
    """The caller names a path on the host. In multi mode a workspace admin is
    a tenant; in single mode superadmin IS the workspace admin, so the operator
    loses nothing."""
    app, superadmin, root = server
    ws = _seed(tmp_path / "src")
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    wsadm = _workspace_admin(app, superadmin)

    assert wsadm.post(
        "/api/v1/workspace/import/from-path", json={"path": str(out)}
    ).status_code == 403

    superadmin.headers["X-Laurelin-Workspace"] = "alpha"
    ok = superadmin.post(
        "/api/v1/workspace/import/from-path",
        json={"path": str(out)},
        params={"dry_run": True},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["applied"] is False


# --------------------------------------------------------------------------- HTTP behaviour


def test_the_export_route_streams_a_tar_led_by_its_manifest(admin):
    with admin.stream("GET", "/api/v1/workspace/export") as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-tar"
        assert "laurelin-export-" in response.headers["content-disposition"]
        raw = b"".join(response.iter_bytes())
    names = _members(raw)
    assert names[0] == "manifest.json"
    assert names[-1] == "TRAILER.json"


def test_a_metadata_only_export_is_gzipped_and_carries_no_parts(admin):
    with admin.stream(
        "GET", "/api/v1/workspace/export", params={"metadata_only": True}
    ) as response:
        assert response.headers["content-type"] == "application/gzip"
        raw = b"".join(response.iter_bytes())
    assert raw[:2] == b"\x1f\x8b"
    assert not [n for n in _members(raw) if n.startswith("data/")]


def test_a_refusal_is_409_and_names_the_flag_that_clears_it(tmp_path):
    """A refusal must arrive as a status code, not as a truncated 200 — which
    is why the manifest is built before the response starts."""
    app = create_app(_seed(tmp_path / "warned", pipeline=CREDENTIAL_PIPELINE))
    client = TestClient(app)
    client.post("/api/v1/auth/setup", json=ADMIN)
    client.post("/api/v1/auth/login", json=ADMIN)

    response = client.get("/api/v1/workspace/export")
    assert response.status_code == 409
    assert "allow_content_warnings" in response.json()["detail"].replace("--", "").replace("-", "_")
    assert client.get(
        "/api/v1/workspace/export", params={"allow_content_warnings": True}
    ).status_code == 200


def test_the_preview_names_every_withheld_field_and_a_route_that_exists(admin, app):
    """A withheld secret with no re-supply instruction is a dead end, and one
    pointing at a route that was renamed is worse. Checked against the OpenAPI
    path table rather than app.routes: this FastAPI defers included routers, so
    iterating app.routes yields path=None and the assertion would pass for any
    string at all."""
    manifest = admin.get("/api/v1/workspace/export/preview").json()
    assert manifest["withheld"], "an export always withholds at least the password hashes"
    known = set(app.openapi()["paths"])
    checked = 0
    for entry in manifest["withheld"]:
        assert entry["resupply"], entry
        for token in entry["resupply"].split():
            if token.startswith("/api/v1/"):
                assert token in known, entry
                checked += 1
    assert checked, "no withheld entry named an API route to re-supply it at"


def _fresh_target(tmp_path, name: str = "target") -> tuple[Workspace, TestClient]:
    target = Workspace.init(tmp_path / name, name="Target")
    client = TestClient(create_app(target))
    assert client.post("/api/v1/auth/setup", json=ADMIN).status_code == 200
    assert client.post("/api/v1/auth/login", json=ADMIN).status_code == 200
    return target, client


def _upload(client: TestClient, archive: Path, **params):
    return client.post(
        "/api/v1/workspace/import",
        files={"file": (archive.name, archive.read_bytes(), "application/x-tar")},
        params=params,
    )


def test_over_http_the_target_is_never_user_empty_and_the_refusal_says_why(
    ws, tmp_path
):
    """A plain POST into a freshly-initialized workspace refuses, because the
    caller had to authenticate to get here and an imported grant naming their
    username would bind to their account. That is correct, and reads as a bug
    unless the message explains it — which is the whole reason this route
    rewrites the refusal instead of passing it through."""
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    _, client = _fresh_target(tmp_path)

    response = _upload(client, out)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "never empty of users" in detail
    assert "dry_run=true&merge=true" in detail


def test_an_uploaded_archive_reconstructs_the_workspace_over_http(ws, tmp_path):
    """The two-phase flow the UI drives: preview, read the digest, apply it."""
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    _, client = _fresh_target(tmp_path)

    preview = _upload(client, out, dry_run=True, merge=True)
    assert preview.status_code == 200, preview.text
    assert preview.json()["applied"] is False
    assert preview.json()["target_not_pristine"] == {"users": 1}
    digest = preview.json()["report_sha256"]
    assert client.get("/api/v1/datasets").json() == []

    applied = _upload(client, out, merge=True, confirm=digest)
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["applied"] is True
    assert body["rows_imported"]["datasets"] == 1
    assert {d["name"] for d in client.get("/api/v1/datasets").json()} == {"sales"}
    # And it is readable again on the next request, from the report file.
    assert client.get("/api/v1/workspace/import/report").json()["applied"] is True


def test_a_merge_over_http_needs_the_digest_of_the_report_that_was_returned(
    ws, tmp_path
):
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    _, client = _fresh_target(tmp_path)

    response = _upload(client, out, merge=True, confirm="0" * 64)
    assert response.status_code == 409
    assert "digest of the report" in response.json()["detail"]
    assert client.get("/api/v1/datasets").json() == []


def test_importing_over_a_dataset_of_the_same_name_is_refused_outright(admin, ws, tmp_path):
    """Two different tables claiming one identity has no safe merge, so no
    digest unlocks it — only --rename-prefix does."""
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    response = _upload(admin, out, dry_run=True, merge=True)
    assert response.status_code == 409
    assert "rename" in response.json()["detail"]


def test_builds_refuse_and_transforms_stay_hidden_until_pipelines_are_acknowledged(
    ws, tmp_path
):
    """A build is the moment imported pipeline code would actually run, so that
    is where the loud refusal belongs. Listing is left working but empty:
    409-ing every listing would hide the workspace behind the control meant to
    protect it."""
    out = tmp_path / "out.tar"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root)])
    _, client = _fresh_target(tmp_path)
    digest = _upload(client, out, dry_run=True, merge=True).json()["report_sha256"]
    assert _upload(client, out, merge=True, confirm=digest).status_code == 200

    state = client.get("/api/v1/workspace/import/state").json()
    assert state["pipelines_acknowledged"] is False
    assert client.get("/api/v1/transforms").json() == []
    blocked = client.post("/api/v1/builds", json={"wait": True})
    assert blocked.status_code == 409
    assert "acknowledge" in blocked.json()["detail"]

    assert client.post(
        "/api/v1/workspace/import/acknowledge-pipelines"
    ).status_code == 200
    assert client.get("/api/v1/workspace/import/state").json()[
        "pipelines_acknowledged"
    ] is True
    assert [t["name"] for t in client.get("/api/v1/transforms").json()] == ["clean"]
    assert client.post("/api/v1/builds", json={"wait": True}).status_code == 200


def test_a_dataset_imported_without_its_data_refuses_rather_than_reading_empty(
    ws, tmp_path
):
    """Zero rows in a governance product is indistinguishable from a working row
    policy. A metadata-only restore must say so."""
    out = tmp_path / "meta.tar.gz"
    runner.invoke(cli, ["export", str(out), "-w", str(ws.root), "--metadata-only"])
    _, client = _fresh_target(tmp_path)
    digest = _upload(client, out, dry_run=True, merge=True).json()["report_sha256"]
    assert _upload(client, out, merge=True, confirm=digest).status_code == 200

    response = client.get("/api/v1/datasets/sales/rows")
    assert response.status_code == 409, response.text
    assert "without its data" in response.json()["detail"]


def test_the_fingerprint_route_reports_a_principal_it_could_not_resolve(admin):
    """After an unbound import no principal resolves, and that absence is the
    single most important thing an operator has to see — so it is a field in
    the response, not a silently shorter matrix."""
    response = admin.post(
        "/api/v1/workspace/governance/fingerprint",
        json={"principals": ["root", "ghost", "anonymous"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unresolved_principals"] == ["ghost"]
    assert set(body["fingerprint"]["principals"]) == {"root", "anonymous"}


def test_exporting_is_audited_because_it_is_a_bulk_read_of_everything(admin):
    _archive_bytes(admin, metadata_only=True)
    actions = [e["action"] for e in admin.get("/api/v1/audit").json()]
    assert "workspace_exported" in actions
