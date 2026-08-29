"""StarRocks-backed datasets: the plumbing around the governance.

Unlike ``tests/test_clickhouse.py``, this suite cannot fake its engine — chdb
reads a local Parquet file through ``file(path, Parquet)``, and a StarRocks
server has no equivalent. So the file splits in two:

* everything that is a *string* — source validation, the scan expression, DSN
  redaction, the type mapping, and the structural bans on building SQL out of
  values — runs everywhere, with no server;
* everything that needs the engine is marked ``needs_starrocks`` and skips
  unless ``LAURELIN_TEST_STARROCKS`` names one (``tests/starrocks_env.py``).

What the policy *returns* under a policy is ``tests/test_starrocks_governance``;
this file is about wiring.
"""

import ast
import io
import re
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import limits, starrocks
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.dialects import STARROCKS
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService
from tests import starrocks_env

_TICKET = _ChangeTicket(kind="local", actor="test")

VIEWER = User(id="1", username="vic", role=Role.viewer)
CREDS = {"username": "root", "password": "trustno1!"}

EVENTS_DDL = ("`id` BIGINT, `region` VARCHAR(16), `ssn` VARCHAR(32), "
              "`amt` DOUBLE, `ok` BOOLEAN, `d` DATE, `dec2` DECIMAL(12,2)")


def events(n: int = 30) -> list[tuple]:
    return [
        (i, ["us", "eu", "apac"][i % 3], f"{i:03d}-00-0000", i + 0.5, True,
         f"2024-01-{i % 28 + 1:02d}", f"{i}.25")
        for i in range(n)
    ]


@pytest.fixture()
def table():
    name = starrocks_env.load(EVENTS_DDL, events())
    yield name
    starrocks_env.drop(name)


@pytest.fixture()
def env(tmp_path, table):
    ws = Workspace.init(tmp_path / "ws", name="sr")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    return catalog, store, PermissionService(store), starrocks_env.source(table)


# ---------------------------------------------------------------------------
# Validation, redaction and the scan expression — no server needed
# ---------------------------------------------------------------------------

URL = "starrocks://u:p@h:9030/db"


@pytest.mark.parametrize("source, msg", [
    ({"type": "files", "url": URL, "table": "t"}, "Unknown StarRocks source type"),
    ({"type": "parquet", "url": URL, "table": "t"}, "there is no file source"),
    ({"type": "table", "table": "t"}, "starrocks://"),
    ({"type": "table", "url": "mysql://u:p@h:9030/db", "table": "t"}, "starrocks://"),
    ({"type": "table", "url": "starrocks://h:9030", "table": "t"}, "needs a database"),
    ({"type": "table", "url": URL, "table": ""}, "Invalid StarRocks table"),
    ({"type": "table", "url": URL, "table": "a.b.c.d"}, "Invalid StarRocks table"),
    ({"type": "table", "url": URL, "table": "t`x"}, "Invalid StarRocks table"),
    ({"type": "table", "url": URL, "table": "t; DROP TABLE x"}, "Invalid StarRocks table"),
    ({"type": "table", "url": URL, "table": "1t"}, "Invalid StarRocks table"),
])
def test_validate_rejects(source, msg):
    with pytest.raises(ValueError, match=msg):
        starrocks.validate_source(source)


def test_validate_accepts_and_normalises():
    assert starrocks.validate_source(
        {"type": "table", "url": f" {URL} ", "table": " cat.db.t ", "junk": 1}
    ) == {"type": "table", "url": URL, "table": "cat.db.t"}


def test_the_url_parses_into_connection_parameters():
    assert starrocks.parse_url("starrocks://alice:s3cr3t@sr.internal:9031/lau") == {
        "user": "alice", "password": "s3cr3t", "host": "sr.internal",
        "port": 9031, "database": "lau",
    }
    # Port and user are optional; the database is not.
    assert starrocks.parse_url("starrocks://sr/lau")["port"] == 9030
    assert starrocks.parse_url("starrocks://sr/lau")["user"] == "root"


@pytest.mark.parametrize("table, expected", [
    ("events", "`events`"),
    ("lau.events", "`lau`.`events`"),
    ("iceberg_cat.lake.events", "`iceberg_cat`.`lake`.`events`"),
])
def test_the_scan_expression_quotes_every_part(table, expected):
    assert starrocks.scan_expression({"type": "table", "table": table}) == expected


def test_source_secrets_are_redacted_including_awkward_passwords():
    """The DSN is the whole credential, so the redactor has to survive the
    passwords people actually pick. ``[^@]*`` — the rule before StarRocks — cut
    the match at the *first* '@' and left the rest of the password in the
    "redacted" string, which travels to any viewer who can see the dataset.
    """
    red = starrocks.redacted_source(
        {"type": "table", "url": "starrocks://alice:pa@ss@sr:9030/lau", "table": "t",
         "password": "pa@ss"}
    )
    assert red["password"] == "*****"
    assert red["url"] == "starrocks://alice:*****@sr:9030/lau"
    assert "pa@ss" not in str(red)

    plain = starrocks.redacted_source({"url": "starrocks://alice:hunter2@sr:9030/lau"})
    assert plain["url"] == "starrocks://alice:*****@sr:9030/lau"
    # A url with no credentials in it is left alone rather than mangled.
    assert starrocks.redacted_source(
        {"url": "starrocks://sr:9030/lau"}
    )["url"] == "starrocks://sr:9030/lau"


# ---------------------------------------------------------------------------
# The type mapping — the fail-closed half of column discovery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sr_type, arrow_type", [
    ("boolean", pa.bool_()),
    ("tinyint", pa.int8()),
    ("smallint", pa.int16()),
    ("int", pa.int32()),
    ("bigint", pa.int64()),
    ("float", pa.float32()),
    ("double", pa.float64()),
    ("date", pa.date32()),
    ("datetime", pa.timestamp("us")),
    ("varchar(64)", pa.string()),
    ("varchar(65533)", pa.string()),
    ("char(3)", pa.string()),
    ("decimal(12,2)", pa.decimal128(12, 2)),
    ("decimal(12, 0)", pa.decimal128(12, 0)),
    ("decimal128(20,6)", pa.decimal128(20, 6)),
    # 128-bit, and Arrow has no integer that wide. Text keeps every digit and
    # keeps the two renderings in agreement.
    ("largeint", pa.string()),
])
def test_the_type_mapping_is_what_desc_reports(sr_type, arrow_type):
    assert starrocks.arrow_type_of(sr_type) == arrow_type


@pytest.mark.parametrize("sr_type", [
    "array<int(11)>", "map<varchar(10),int(11)>", "struct<a int(11)>",
    "json", "hll", "bitmap", "varbinary(100)", "percentile",
])
def test_an_unmappable_column_type_is_refused_by_name(sr_type):
    """A guessed Arrow type is a guessed *text form*, and a row policy is a
    comparison of text. Refusing names the column instead of approximating it.
    """
    with pytest.raises(starrocks.StarRocksError, match="not supported"):
        starrocks.arrow_type_of(sr_type)


def test_boolean_and_tinyint_are_distinguishable_only_through_desc():
    """Why ``schema_of`` uses ``DESC`` and not the result-set metadata or
    ``information_schema``.

    Measured: the MySQL protocol reports a StarRocks BOOLEAN as field type 1,
    the same code as TINYINT, and ``information_schema.columns`` reports it as
    ``tinyint(1)``. Either would make a boolean column look like an integer —
    and integers are row-key portable on this dialect while booleans are not,
    so the dialect would claim an agreement it does not have.
    """
    assert starrocks.arrow_type_of("boolean") == pa.bool_()
    assert starrocks.arrow_type_of("tinyint") == pa.int8()
    assert STARROCKS.row_key_matches_arrow(pa.int8())
    assert not STARROCKS.row_key_matches_arrow(pa.bool_())
    # information_schema's spelling maps to the *wrong* one, which is the point.
    assert starrocks.arrow_type_of("tinyint(1)") == pa.int8()


# ---------------------------------------------------------------------------
# The structural ban: no value ever becomes StarRocks SQL text
# ---------------------------------------------------------------------------

def test_no_module_builds_starrocks_sql_by_interpolating_a_value():
    """The half that behaviour cannot cover.

    ``StarRocksDialect.literal`` raises, so there is no sanctioned door — but a
    future call site could format a value into a statement directly and every
    behavioural test would still pass, because they only exercise the doors
    that exist. So this walks the AST: every statement handed to
    ``starrocks.run`` (or to the module's own ``_fetch``/``_command``) must be a
    bare name or an f-string whose interpolations are all names, and each name
    must come from ``scan_expression`` or ``SqlDialect.assemble`` — the two
    functions built out of ``quote``, and out of nothing else.
    """
    root = Path(__file__).resolve().parents[1] / "laurelin"
    approved = {"scan_expression", "assemble"}
    offenders = []

    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            sources: dict[str, set[str]] = {}
            for node in ast.walk(func):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    called = node.value.func
                    name = getattr(called, "attr", getattr(called, "id", ""))
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            sources.setdefault(target.id, set()).add(name)

            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                called = node.func
                qualified = (
                    getattr(called, "attr", None) == "run"
                    and getattr(getattr(called, "value", None), "id", None) == "starrocks"
                )
                internal = (
                    path.name == "starrocks.py"
                    and getattr(called, "id", None) in {"run", "_fetch", "_command"}
                )
                if not (qualified or internal):
                    continue
                where = f"{path.relative_to(root)}:{node.lineno}"
                # `_fetch(con, sql, params)` and `_command(con, sql)` take the
                # connection first; `run(sql, ...)` takes the statement first.
                args = node.args
                arg = None
                for candidate in args:
                    if isinstance(candidate, (ast.JoinedStr, ast.Constant)) or (
                        isinstance(candidate, ast.Name) and candidate.id in {"sql"}
                    ):
                        arg = candidate
                        break
                if arg is None and args:
                    arg = args[-1]
                if isinstance(arg, ast.Constant) or isinstance(arg, ast.Name):
                    names = [arg.id] if isinstance(arg, ast.Name) else []
                elif isinstance(arg, ast.JoinedStr):
                    parts = [p for p in arg.values if isinstance(p, ast.FormattedValue)]
                    if not all(isinstance(p.value, ast.Name) for p in parts):
                        offenders.append(f"{where}: interpolates an expression")
                        continue
                    names = [p.value.id for p in parts]
                else:
                    offenders.append(f"{where}: statement is not a name or f-string")
                    continue
                for name in names:
                    if name == "sql":
                        continue  # a statement handed in from `assemble` above
                    if not sources.get(name, set()) & approved:
                        offenders.append(
                            f"{where}: {name!r} is not built by "
                            + "/".join(sorted(approved))
                        )

    assert offenders == [], (
        "StarRocks SQL must be assembled only from scan_expression/assemble: "
        f"{offenders}"
    )


def test_no_module_calls_literal_on_the_starrocks_dialect():
    """``literal`` raising is the mechanism; not calling it is the intent.

    A call site that reached for it would fail at runtime rather than leak —
    but it would fail on a governed read, in production, which is a worse way
    to find out than a grep.
    """
    root = Path(__file__).resolve().parents[1] / "laurelin"
    pattern = re.compile(r"STARROCKS\.literal|starrocks\.literal")
    offenders = [
        str(f.relative_to(root)) for f in root.rglob("*.py")
        if pattern.search(f.read_text())
    ]
    assert offenders == [], f"nothing may escape a value for StarRocks: {offenders}"


def test_the_budget_rides_in_a_hint_and_the_timeout_is_integral():
    """``SET_VAR(query_timeout=7.5)`` is error 1232, "Incorrect argument type"
    — a budget that fails to parse is a query with no budget at all."""
    hint = limits.starrocks_hint(limits.QueryLimits(memory_limit="512MB", timeout_s=7.5))
    assert "query_timeout=8" in hint
    assert f"query_mem_limit={512 * 1024 * 1024}" in hint
    assert "." not in hint.split("query_timeout=")[1].split(",")[0]

    # A disabled timeout emits no clause rather than "0".
    assert "query_timeout" not in limits.starrocks_hint(
        limits.QueryLimits(timeout_s=0)
    )


# ---------------------------------------------------------------------------
# Against the engine
# ---------------------------------------------------------------------------

pytestmark_engine = starrocks_env.needs_starrocks


@starrocks_env.needs_starrocks
def test_register_and_read(env):
    catalog, store, perms, source = env
    info = catalog.register_starrocks("events", source, "starrocks events")

    assert info.is_starrocks and info.kind == "starrocks"
    assert info.scans_at_source and info.sql_dialect == "starrocks"
    assert info.latest_version is None, "a StarRocks table has no versions"

    table = catalog.read("events")
    assert table.num_rows == 30
    # The declared schema, not whatever the protocol guessed: BOOLEAN stays
    # boolean and DECIMAL keeps its scale.
    assert table.schema.field("ok").type == pa.bool_()
    assert table.schema.field("dec2").type == pa.decimal128(12, 2)
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("d").type == pa.date32()


@starrocks_env.needs_starrocks
def test_register_probes_and_fails_loudly(env):
    catalog, _, _, source = env
    with pytest.raises(starrocks.StarRocksError):
        catalog.register_starrocks(
            "nope", dict(source, table=f"{starrocks_env.database()}.no_such_table")
        )
    assert catalog.store.get_dataset("nope") is None, "a failed probe stores nothing"


@starrocks_env.needs_starrocks
def test_an_unreachable_server_is_an_error_not_an_empty_read(env):
    catalog, _, _, source = env
    dead = dict(source, url=source["url"].replace(":9030/", ":9031/")
                .replace(":59030/", ":59031/"))
    # R1: `_redact(exc, password)` used to build this message by substring
    # substitution, which only worked when the driver quoted the password back
    # verbatim. Classified now, from the driver's own errno (2003 = refused,
    # measured against live StarRocks).
    with pytest.raises(starrocks.StarRocksError, match="Nothing accepted a connection"):
        catalog.register_starrocks("dead", dead)


@starrocks_env.needs_starrocks
def test_a_column_type_laurelin_cannot_render_is_refused_at_registration(env, tmp_path):
    """The alternative is a JSON column carried as an approximate string, which
    a row policy would then happily compare against."""
    catalog, _, _, _ = env
    name = starrocks_env.load("`id` INT, `blob` JSON", [])
    try:
        with pytest.raises(starrocks.StarRocksError, match="not supported"):
            catalog.register_starrocks("js", starrocks_env.source(name))
    finally:
        starrocks_env.drop(name)


@starrocks_env.needs_starrocks
def test_an_empty_column_list_is_a_refusal(env, monkeypatch):
    catalog, _, _, source = env
    monkeypatch.setattr(starrocks, "_command", lambda con, sql: [])
    with pytest.raises(starrocks.StarRocksError, match="no columns"):
        starrocks.schema_of(source)


@starrocks_env.needs_starrocks
def test_rows_pages_at_the_source(env):
    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    assert len(catalog.rows("events", limit=5)) == 5
    ids = [r["id"] for r in catalog.rows("events", limit=30)]
    assert sorted(ids) == list(range(30))


@starrocks_env.needs_starrocks
def test_scan_for_reaches_the_source(env):
    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    store.set_dataset_policy("events", {
        "dataset": "events",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["eu"]}]},
        "column_masks": [],
    }, ticket=_TICKET)
    scan = catalog.scan_for("events", plan_for=perms.arrow_policy_fn(VIEWER))
    table = scan if isinstance(scan, pa.Table) else scan.to_table()
    assert set(table.column("region").to_pylist()) == {"eu"}


@starrocks_env.needs_starrocks
def test_iter_batches_yields_the_table_whole(env):
    """Honest rather than pretending to stream something already collected."""
    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    batches = list(catalog.iter_batches("events"))
    assert len(batches) == 1 and batches[0].num_rows == 30


@starrocks_env.needs_starrocks
def test_the_reader_binds_and_the_statement_carries_no_value(env, monkeypatch):
    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    store.set_dataset_policy("events", {
        "dataset": "events",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "vic",
             "values": ["eu", "us') OR 1=1 --"]}]},
        "column_masks": [],
    }, ticket=_TICKET)

    seen = []
    real = starrocks.run

    def spy(sql, params=None, con=None, source=None, schema=None):
        seen.append((sql, params))
        return real(sql, params, con, source, schema)

    monkeypatch.setattr(starrocks, "run", spy)
    table = catalog.source_table("events", sql_policy_for=perms.sql_policy_fn(VIEWER))
    assert set(table.column("region").to_pylist()) == {"eu"}

    sql, params = seen[-1]
    assert "us') OR 1=1 --" not in sql
    assert params == ["eu", "us') OR 1=1 --"]
    assert sql.count("SELECT") == 2, "one projection, one filtered scan"
    assert ";" not in sql, "one statement, and only one"


@starrocks_env.needs_starrocks
def test_write_append_and_upload_all_refuse(env, tmp_path):
    """A write would land local Parquet parts and mint a version row while
    read() kept returning the remote table."""
    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    rows = pa.table({"id": pa.array([99], pa.int64())})

    for call in (
        lambda: catalog.write("events", rows),
        lambda: catalog.append("events", rows),
        lambda: catalog.write_batches("events", [rows]),
        lambda: catalog.append_batches("events", [rows]),
    ):
        with pytest.raises(ValueError, match="scanned at the source"):
            call()

    csv = tmp_path / "more.csv"
    csv.write_text("id,region\n99,us\n")
    with pytest.raises(ValueError, match="scanned at the source"):
        catalog.upload_file("events", csv)

    assert store.get_dataset("events").latest_version is None


@starrocks_env.needs_starrocks
def test_the_read_account_needs_no_write_privilege(env):
    """Defence in depth, and the only layer that survives a bug in the others.

    Measured: a StarRocks user holding only SELECT is refused INSERT, DELETE,
    CREATE and DROP with error 5203 — *including* when the write is smuggled in
    as a second statement on the same execute(), which is the one attack the
    client cannot prevent.
    """
    catalog, store, perms, source = env
    admin = starrocks_env.connect()
    db = starrocks_env.database()
    try:
        cur = admin.cursor()
        cur.execute("DROP USER IF EXISTS 'laurelin_ro_test'")
        cur.execute("CREATE USER 'laurelin_ro_test' IDENTIFIED BY 'ro-pass'")
        cur.execute(
            f"GRANT SELECT ON ALL TABLES IN DATABASE `{db}` TO USER 'laurelin_ro_test'"
        )
        cur.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"the configured account cannot create users: {exc}")
    try:
        params = starrocks.parse_url(source["url"])
        ro_url = (f"starrocks://laurelin_ro_test:ro-pass@{params['host']}:"
                  f"{params['port']}/{params['database']}")
        catalog.register_starrocks("events", dict(source, url=ro_url))
        assert catalog.read("events").num_rows == 30

        con = starrocks.connect({"url": ro_url})
        try:
            scan = starrocks.scan_expression(source)
            # Two independent barriers, asserted separately because either one
            # alone would make the other's failure invisible.
            #
            # (1) The library's own execution path cannot express a write at
            #     all: it runs everything through a prepared cursor, and
            #     StarRocks rejects INSERT in the prepared protocol outright
            #     (error 1295). This is a property of the channel, not of the
            #     grant, so it holds even against a privileged account.
            with pytest.raises(starrocks.StarRocksError, match="1295"):
                starrocks.run(f"INSERT INTO {scan} (`id`) VALUES (1)", con=con)

            # (2) And the account itself is refused — which is what still
            #     protects the table if someone adds an ordinary cursor.
            cur = con.cursor()
            with pytest.raises(Exception, match="5203|Access denied"):
                cur.execute(f"INSERT INTO {scan} (`id`) VALUES (1)")
            cur.close()
        finally:
            con.close()
    finally:
        cur = admin.cursor()
        cur.execute("DROP USER IF EXISTS 'laurelin_ro_test'")
        cur.close()
        admin.close()


# -- the ontology and workbench guards -----------------------------------------

ONTOLOGY = """
object_types:
  - api_name: event
    backing_dataset: events
    primary_key: id
    properties:
      id: {type: integer}
      region: {type: string}
"""


@starrocks_env.needs_starrocks
def test_ontology_refuses_a_starrocks_backing_dataset(env):
    from laurelin.ontology import OntologyService, load_ontology

    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    (catalog.workspace.ontology_dir / "o.yml").write_text(ONTOLOGY)

    svc = OntologyService(
        catalog.workspace, catalog, store, load_ontology(catalog.workspace.ontology_dir)
    )
    with pytest.raises(ValueError, match="scanned at the source"):
        svc.query("event", limit=10)


@starrocks_env.needs_starrocks
def test_workbench_excludes_starrocks_by_default(env, monkeypatch):
    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    monkeypatch.delenv("LAURELIN_FEDERATION_WORKBENCH", raising=False)
    admin = User(id="2", username="ada", role=Role.admin)

    with pytest.raises(Exception):
        catalog.query(
            "SELECT count(*) AS n FROM events",
            plan_for=perms.arrow_policy_fn(admin),
            sql_policy_for=perms.sql_policy_fn(admin),
        )


@starrocks_env.needs_starrocks
def test_workbench_includes_starrocks_when_enabled_and_policied(env, monkeypatch):
    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    monkeypatch.setenv("LAURELIN_FEDERATION_WORKBENCH", "1")
    admin = User(id="2", username="ada", role=Role.admin)

    result = catalog.query(
        "SELECT count(*) AS n FROM events",
        plan_for=perms.arrow_policy_fn(admin),
        sql_policy_for=perms.sql_policy_fn(admin),
    )
    assert result["rows"][0]["n"] == 30

    # Both conditions, not either: the gate being on must not expose a table
    # with no policy renderer attached.
    with pytest.raises(Exception):
        catalog.query("SELECT count(*) FROM events", sql_policy_for=None)


# -- HTTP ----------------------------------------------------------------------

def _admin(tmp_path, name="srapi"):
    ws = Workspace.init(tmp_path / name, name=name)
    app = create_app(ws)
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=CREDS)
    admin.post("/api/v1/auth/login", json=CREDS)
    return app, admin


@starrocks_env.needs_starrocks
def test_registration_is_admin_only(tmp_path, table):
    app, admin = _admin(tmp_path)
    admin.post("/api/v1/users",
               json={"username": "ed", "password": "password123", "role": "editor"})
    editor = TestClient(app)
    editor.post("/api/v1/auth/login",
                json={"username": "ed", "password": "password123"})

    body = {"source": starrocks_env.source(table)}
    assert editor.put("/api/v1/datasets/sr_events/starrocks",
                      json=body).status_code == 403

    r = admin.put("/api/v1/datasets/sr_events/starrocks", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "starrocks"

    # A bad source is a 400; an unreachable one a 502.
    assert admin.put("/api/v1/datasets/bad/starrocks",
                     json={"source": {"type": "nope"}}).status_code == 400
    gone = dict(starrocks_env.source(table), table=f"{starrocks_env.database()}.ghost")
    assert admin.put("/api/v1/datasets/gone/starrocks",
                     json={"source": gone}).status_code == 502


@starrocks_env.needs_starrocks
def test_cannot_shadow_a_managed_dataset(tmp_path, table):
    _, admin = _admin(tmp_path, "srshadow")
    admin.post("/api/v1/datasets", json={"name": "orders"})
    r = admin.put("/api/v1/datasets/orders/starrocks",
                  json={"source": starrocks_env.source(table)})
    assert r.status_code == 409


@starrocks_env.needs_starrocks
def test_the_dsn_never_reaches_a_viewer(tmp_path, table):
    """The source config carries the whole credential, and GET /datasets is
    viewer-readable."""
    _, admin = _admin(tmp_path, "srleak")
    admin.put("/api/v1/datasets/remote/starrocks",
              json={"source": starrocks_env.source(table)})

    password = starrocks.parse_url(starrocks_env.URL)["password"]
    listed = admin.get("/api/v1/datasets").json()
    remote = next(d for d in listed if d["name"] == "remote")
    assert "*****" in remote["source"]["url"] or "@" not in remote["source"]["url"]
    if password:
        assert password not in str(remote)
        assert password not in str(admin.get("/api/v1/datasets/remote").json())


@starrocks_env.needs_starrocks
def test_rows_endpoint_previews_a_starrocks_dataset(tmp_path, table):
    _, admin = _admin(tmp_path, "srrows")
    admin.put("/api/v1/datasets/remote/starrocks",
              json={"source": starrocks_env.source(table)})

    body = admin.get("/api/v1/datasets/remote/rows?limit=3").json()
    assert len(body["rows"]) == 3
    assert body["row_count"] is None, "unknown total, not a fabricated one"


@starrocks_env.needs_starrocks
def test_uploading_to_a_starrocks_dataset_is_refused_over_http(tmp_path, table):
    _, admin = _admin(tmp_path, "srnoup")
    admin.put("/api/v1/datasets/remote/starrocks",
              json={"source": starrocks_env.source(table)})

    files = {"file": ("d.csv", io.BytesIO(b"id,region\n1,us\n"), "text/csv")}
    r = admin.post("/api/v1/datasets/remote/upload", files=files)
    assert r.status_code == 400, r.text
    assert "scanned at the source" in r.text


# -- transforms: reduce at the boundary ----------------------------------------

PIPELINE = """
from laurelin.transforms import sql_transform, Input, Output

@sql_transform(
    output=Output("events_by_region"),
    inputs={"e": Input("events")},
    query="SELECT region, count(*) AS n FROM e GROUP BY region ORDER BY region",
)
def rollup(): ...
"""


@starrocks_env.needs_starrocks
def test_a_transform_reduces_a_starrocks_table_into_a_managed_one(env):
    from laurelin.transforms import Builder, collect_transforms

    catalog, store, perms, source = env
    catalog.register_starrocks("events", source)
    (catalog.workspace.pipelines_dir / "p.py").write_text(PIPELINE)

    registry = collect_transforms(catalog.workspace.pipelines_dir)
    build = Builder(catalog.workspace, catalog, store, registry).build()
    assert build.status.value == "succeeded", build.tasks

    rollup = store.get_dataset("events_by_region")
    assert rollup.kind == "managed" and rollup.latest_version == 1
    rows = {r["region"]: r["n"] for r in catalog.rows("events_by_region", limit=10)}
    assert rows == {"apac": 10, "eu": 10, "us": 10}
