"""The secrets posture: what the archive must never contain.

Every needle below was measured leaking through at least one of the three
production redactors (``federation.redacted_source``, ``engines._redact_uri``,
``connectors.redacted_config``) before this module existed. Those redactors are
still right for an API response an admin reads on a live system; they are the
wrong control for a file that gets emailed and committed to git, which is why
the export omits by key allowlist instead of reusing them.
"""

import io
import json
import tarfile

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.failure import Failure, FailureCode
from laurelin.core.models import Role, SourceInfo, User
from laurelin.export import (
    ExportOptions,
    ExportRefused,
    ImportOptions,
    NeedsCredentials,
    export_workspace,
    import_workspace,
    preview_manifest,
)

# Each of these was reproduced surviving a production redactor intact.
NEEDLES = (
    b"hunter2",
    b"p@sswd",
    b"p/w-secret",
    b"Bearer SEKRET",
    b"svc_laurelin",
    b"pg-prod-3.internal",
    b"db.internal",
    b"api_key_SEKRET",
    b"Pwd=hunter2",
    b"nested-SEKRET",
    b"header-SEKRET",
)


@pytest.fixture()
def ws(tmp_path):
    workspace = Workspace.init(tmp_path / "src", name="Acme Production")
    store = MetadataStore(workspace.metadata_path)
    catalog = DatasetCatalog(workspace, store)
    catalog.write("sales", pa.table({"region": ["eu"], "amount": [1]}))

    # A federated dataset whose DSN is the classic case, and three more shapes
    # the redactors were measured to pass through untouched.
    store.upsert_dataset("crm", "remote CRM")
    store.set_dataset_source("crm", "federated", {
        "type": "postgres",
        "table": "public.crm",
        "url": "postgresql://svc_laurelin:p/w-secret@pg-prod-3.internal:5432/crm",
    })
    store.upsert_dataset("odbc", "an odbc pointer")
    store.set_dataset_source("odbc", "federated", {
        "type": "parquet",
        "dsn": "Server=db.internal;Uid=alice;Pwd=hunter2;",
    })

    store.upsert_source(SourceInfo(
        name="feed", type="http", dataset="sales",
        config={
            "url": "https://api.example.com/e.csv?api_key=api_key_SEKRET",
            "auth": {"password": "nested-SEKRET"},
            "options": {"retries": 3, "password": "nested-SEKRET"},
            "headers": {"X-Api-Key": "header-SEKRET"},
        },
        created_by="andy",
    ))
    # R1: the store takes a Failure, not a string. There is no longer a
    # parameter here that a driver's sentence fits into — which is the whole
    # point, and is why this line changed shape rather than value.
    store.record_source_sync("feed", status="error", failure=Failure(
        code=FailureCode.AUTH_REJECTED, subject="source:feed",
        endpoint="db.internal:5432", driver="psycopg",
    ))

    store.upsert_engine(
        "trino", "flightsql", "grpc+tls://alice:p@sswd@db.internal:443",
        {"adbc.flight.sql.rpc.call_header.authorization": "Bearer SEKRET"},
        created_by="andy",
    )
    store.create_user(
        User(id="1", username="vic", role=Role.viewer),
        "scrypt:32768:8:1$hunter2hash$deadbeef",
    )
    store.create_session("sessiontokenhash", "1", "2026-01-01", "2027-01-01")
    store.create_oidc_flow("state1", "nonce-SEKRET", "verifier-hunter2",
                           "https://app/callback")
    return workspace


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


def _archive(ws, store, tmp_path, **kwargs) -> bytes:
    path = tmp_path / "export.tar"
    export_workspace(ws, store, path, ExportOptions(**kwargs))
    return path.read_bytes()


def test_no_dsn_survives_anywhere_in_the_archive_bytes(ws, store, tmp_path):
    """A raw-byte scan, not a field-by-field check: the point of an allowlist is
    that a secret cannot reach the archive through a path nobody enumerated."""
    raw = _archive(ws, store, tmp_path)
    leaked = [n for n in NEEDLES if n in raw]
    assert leaked == [], f"the archive carries {leaked!r}"


def test_the_export_withholds_more_than_the_api_redacts(ws, store, tmp_path):
    """The asymmetry is the design claim, so it needs a test that fails if
    someone harmonizes the two.

    The API deliberately keeps ``user@host:port/db`` — an admin reading a live
    system they can already reach loses nothing and gains recognizability. A
    file is different: that string is an internal network map plus a valid
    username.
    """
    from laurelin.connectors import redacted_config

    kept = redacted_config({"url": "postgresql://alice:hunter2@db.internal:5432/prod"})
    assert "alice" in kept["url"] and "db.internal" in kept["url"]

    raw = _archive(ws, store, tmp_path)
    assert b"db.internal" not in raw
    assert b"pg-prod-3.internal" not in raw


def test_password_hashes_never_leave_the_workspace(ws, store, tmp_path):
    """scrypt is an offline-cracking corpus once it is in a file that travels."""
    raw = _archive(ws, store, tmp_path)
    assert b"scrypt" not in raw
    users = json.loads(_member(raw, "tables/users.jsonl").splitlines()[0])
    assert "password_hash" not in users
    assert users["username"] == "vic"


def test_sessions_and_oidc_flows_have_no_member_in_the_archive(ws, store, tmp_path):
    """A session token_hash keeps every cookie minted against the source
    authenticating at the target; an oidc code_verifier is exploitable with an
    intercepted authorization code."""
    raw = _archive(ws, store, tmp_path)
    names = _names(raw)
    assert "tables/sessions.jsonl" not in names
    assert "tables/oidc_flows.jsonl" not in names
    assert b"sessiontokenhash" not in raw
    assert b"verifier-hunter2" not in raw


def test_a_failure_record_is_nulled_because_the_archive_needs_no_failure_history(
    ws, store, tmp_path
):
    """Under R1 nothing free-form is stored to begin with — a `Failure` is safe
    by construction. The export nulls it anyway, because the archive is a file
    that leaves the building and a failure record is history, not state."""
    manifest = preview_manifest(ws, store, ExportOptions())
    assert manifest.nulled_error_fields.get("sources.last_sync_failure_json") == 1
    raw = _archive(ws, store, tmp_path)
    assert b"auth_rejected" not in raw


def test_the_manifest_names_every_withheld_field_and_where_to_resupply_it(ws, store):
    """Positive enumeration: absence is not a report."""
    manifest = preview_manifest(ws, store, ExportOptions())
    fields = {(w.table, w.field) for w in manifest.withheld}
    assert ("engines", "uri") in fields
    assert ("users", "password_hash") in fields
    assert ("datasets", "source_json.url") in fields
    assert ("datasets", "source_json.dsn") in fields
    assert ("sources", "config_json.auth") in fields
    assert ("sources", "config_json.options") in fields, (
        "the nested object connectors.redacted_config was measured to walk "
        "past — and the allowlist withholds a non-shape key whole, so the "
        "report names the key, not a path inside it"
    )
    assert ("sources", "config_json.headers") in fields
    assert (
        "engines",
        "options_json.adbc.flight.sql.rpc.call_header.authorization",
    ) in fields, "the ADBC header engines._SECRET_KEY_RE was measured to miss"
    for withheld in manifest.withheld:
        assert withheld.resupply, f"{withheld.table}.{withheld.field} has no way back"


def test_a_registration_keeps_its_shape_when_its_endpoint_is_withheld(ws, store, tmp_path):
    """Omitting source_json wholesale would destroy the dataset's registration;
    the operator needs the keys to know what to re-fill."""
    raw = _archive(ws, store, tmp_path)
    rows = {
        json.loads(line)["name"]: json.loads(line)
        for line in _member(raw, "tables/datasets.jsonl").splitlines()
    }
    source = json.loads(rows["crm"]["source_json"])
    assert source["type"] == "postgres" and source["table"] == "public.crm"
    assert source["url"] is None

    options = json.loads(
        json.loads(_member(raw, "tables/engines.jsonl").splitlines()[0])["options_json"]
    )
    assert list(options) == ["adbc.flight.sql.rpc.call_header.authorization"]
    assert options["adbc.flight.sql.rpc.call_header.authorization"] is None


def test_an_imported_federated_dataset_refuses_rather_than_returning_zero_rows(
    ws, store, tmp_path
):
    """Zero rows in a governance product is indistinguishable from a working row
    policy, which is exactly the subtly-broken migration this feature exists to
    prevent. The catalog raises; the API maps it to 409."""
    raw = _archive(ws, store, tmp_path)
    target = Workspace.init(tmp_path / "dst", name="dst")
    target_store = MetadataStore(target.metadata_path)
    import_workspace(io.BytesIO(raw), target, target_store, ImportOptions())

    catalog = DatasetCatalog(target, target_store)
    with pytest.raises(NeedsCredentials, match="no endpoint"):
        catalog.source_table("crm")
    # And the managed dataset beside it still reads, so the refusal is targeted.
    assert catalog.read("sales").num_rows == 1


def test_a_content_warning_preview_does_not_repeat_the_credential(ws, store):
    """The warning report is embedded in manifest.json, which is the archive's
    first member — so a preview that echoed the value would put the credential
    in the file the operator hands around."""
    (ws.pipelines_dir / "leaky.py").write_text("PASSWORD = 'hunter2'\n")
    manifest = preview_manifest(ws, store, ExportOptions(allow_content_warnings=True))
    assert manifest.content_warnings
    for warning in manifest.content_warnings:
        assert "hunter2" not in warning.preview


# --------------------------------------------------------------------------- helpers

def _names(raw: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        return [m.name for m in tar]


def _member(raw: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        for member in tar:
            if member.name == name:
                return tar.extractfile(member).read().decode()
    raise AssertionError(f"no member {name!r}")


# --------------------------------------------------------------------------- the keys
# --------------------------------------------------------------------------- nobody
# --------------------------------------------------------------------------- listed

# Each of these was measured shipping verbatim, with its password intact, while
# the manifest positively certified that the row's endpoint had been withheld.
# `SourceUpsertRequest.config` is `dict[str, Any]`, so the vocabulary is the
# caller's — which is why the guard is now an allowlist over shape keys rather
# than a denylist over the names we happened to think of.
UNLISTED_ENDPOINT_KEYS = {
    "base_url": "https://svc:BASEURL-unlisted@api.corp.internal/v1",
    "endpoint_url": "https://s3.corp.internal/?sig=ENDPOINT-unlisted",
    "hosts": ["es:HOSTS-unlisted@es-prod-1.corp.internal:9200"],
    "bootstrap_servers": "kafka://u:BOOTSTRAP-unlisted@k1.corp:9092",
    "connection": "postgresql://u:CONNECTION-unlisted@pg.corp/db",
    "path": "https://files.corp/x.parquet?token=PATHTOK-unlisted",
}


def test_an_endpoint_key_the_denylist_never_heard_of_is_still_withheld(
    ws, store, tmp_path
):
    from laurelin.core.models import SourceInfo

    store.upsert_source(SourceInfo(
        name="warehouse", type="http", dataset="sales",
        config={**UNLISTED_ENDPOINT_KEYS, "format": "csv"}, created_by="andy",
    ))
    raw = _archive(ws, store, tmp_path)
    for key, value in UNLISTED_ENDPOINT_KEYS.items():
        needle = value[0] if isinstance(value, list) else value
        assert needle.encode() not in raw, f"config_json.{key} still travels"

    row = next(
        json.loads(line)
        for line in _member(raw, "tables/sources.jsonl").splitlines()
        if json.loads(line)["name"] == "warehouse"
    )
    config = json.loads(row["config_json"])
    assert config["format"] == "csv", "a shape key still travels, or nothing can be re-filled"
    assert all(config[key] is None for key in UNLISTED_ENDPOINT_KEYS)

    withheld = {(w.table, w.field) for w in preview_manifest(ws, store).withheld}
    for key in UNLISTED_ENDPOINT_KEYS:
        assert ("sources", f"config_json.{key}") in withheld


def test_a_federated_path_is_withheld_even_though_three_source_types_require_it(
    ws, store, tmp_path
):
    """federation.validate_source requires `path` for parquet, iceberg and
    delta, and _REMOTE_PREFIXES blesses s3:// and https:// — which is exactly
    where a presigned signature or a userinfo pair lives. The posture worked
    for `url` (postgres) and silently failed for the other three."""
    store.upsert_dataset("events", "remote parquet")
    store.set_dataset_source("events", "federated", {
        "type": "parquet",
        "path": "s3://bucket/e.parquet?X-Amz-Signature=FEDSIG-unlisted",
    })
    raw = _archive(ws, store, tmp_path)
    assert b"FEDSIG-unlisted" not in raw
    assert ("datasets", "source_json.path") in {
        (w.table, w.field) for w in preview_manifest(ws, store).withheld
    }


def test_a_credential_in_free_audit_text_does_not_travel(ws, store, tmp_path):
    """auth_routes.py logs {"reason": str(exc)} on an OIDC failure, and a token
    endpoint URL with a client_secret in it lands there verbatim. The audit
    matcher deliberately had no endpoint keys, so it shipped."""
    store.log_audit(
        "oidc_login_failed",
        {"reason": "token endpoint https://idp/token?client_secret=AUDIT-x refused"},
        actor="oidc",
    )
    store.log_audit(
        "source_sync_failed",
        {"source": "feed", "url": "https://svc:AUDITURL-x@api.corp.internal/v1"},
        actor="sys",
    )
    raw = _archive(ws, store, tmp_path)
    assert b"AUDIT-x" not in raw
    assert b"AUDITURL-x" not in raw


def test_an_audit_row_keeps_the_subject_it_is_a_record_of(ws, store, tmp_path):
    """The counterweight: an audit trail with its subjects nulled is not an
    audit trail, and `user` is half a DSN only in a connector config."""
    store.log_audit("user_created", {"username": "vic", "n": 7}, actor="andy")
    raw = _archive(ws, store, tmp_path)
    row = next(
        json.loads(line)
        for line in _member(raw, "tables/audit_log.jsonl").splitlines()
        if json.loads(line)["action"] == "user_created"
    )
    assert json.loads(row["details_json"]) == {"username": "vic", "n": 7}


CREDENTIAL_SHAPES = {
    "bearer": "HEADERS = {'Authorization': 'Bearer eyJhbGciBEARER-x'}",
    "passwd": "CONN = connect(user='svc', passwd='PASSWD-x')",
    "jdbc": "JDBC = 'jdbc:sqlserver://svc:JDBC-x@db.corp/prod'",
    "mongodb": "MONGO = 'mongodb+srv://svc:MONGO-x@c.mongodb.net/db'",
    "rediss": "REDIS = 'rediss://default:REDIS-x@cache.corp:6380'",
}


@pytest.mark.parametrize("label", sorted(CREDENTIAL_SHAPES))
def test_the_scanner_knows_the_credential_shapes_it_used_to_walk_past(
    ws, store, label
):
    """Measured against the word list this replaced: all five reported
    `pipeline_warnings: []` and the export shipped them. The whole safety story
    for pipelines/ is "the export refuses if the scan hits", so a miss is not a
    missing warning — it is a silent ship of code the destination execs."""
    (ws.pipelines_dir / "leaky.py").write_text(CREDENTIAL_SHAPES[label] + "\n")
    with pytest.raises(ExportRefused, match="credential"):
        preview_manifest(ws, store, ExportOptions())


def test_authored_json_columns_are_scanned_like_pipeline_source(ws, store, tmp_path):
    """dashboards.panels_json carries free SQL, and object_apps/schedules carry
    open-vocabulary config. None of the three got any posture at all:
    strip_secrets was never called for them, and the scanner never looked."""
    from laurelin.core.models import (
        DashboardInfo,
        DashboardPanel,
        ObjectAppInfo,
        ScheduleInfo,
    )

    store.upsert_dashboard(DashboardInfo(
        name="ops", title="Ops", created_by="andy",
        panels=[DashboardPanel(id="p", sql=(
            "SELECT * FROM postgres_scan("
            "'postgresql://svc:DASH-x@pg.corp/db','public','t')"
        ))],
    ))
    store.upsert_object_app(ObjectAppInfo(
        name="app", title="App", object_type="sale", created_by="andy",
        filters={"webhook": "https://hooks.corp/x?token=APPTOK-x"},
    ))
    store.upsert_schedule(ScheduleInfo(
        name="nightly", trigger="cron", cron="0 0 * * *", action="build",
        targets=["s3://k:SCHEDPASS-x@bucket/t"], created_by="andy",
    ))

    with pytest.raises(ExportRefused, match="credential"):
        preview_manifest(ws, store, ExportOptions())

    flagged = {
        w.file for w in
        preview_manifest(ws, store, ExportOptions(allow_content_warnings=True))
        .content_warnings
    }
    assert flagged >= {
        "tables/dashboards.jsonl:ops:panels_json",
        "tables/object_apps.jsonl:app:config_json",
        "tables/schedules.jsonl:nightly:targets_json",
    }


def test_ontology_yaml_is_scanned_like_pipeline_source(ws, store):
    """It travels byte-faithful for the same reason pipelines do — stripping it
    would change what it means — so it gets the same scan. It was getting
    neither."""
    (ws.ontology_dir / "extra.yml").write_text(
        "object_types:\n"
        "  - api_name: note\n"
        "    backing_dataset: sales\n"
        "    primary_key: region\n"
        "    x_operator_note: \"postgresql://svc:ONTO-x@pg.corp/db\"\n"
    )
    with pytest.raises(ExportRefused, match="credential"):
        preview_manifest(ws, store, ExportOptions())
    assert any(
        w.file == "ontology/extra.yml"
        for w in preview_manifest(
            ws, store, ExportOptions(allow_content_warnings=True)
        ).content_warnings
    )
