"""Tests for pipeline (transform) authoring: file CRUD, validation, generation."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.transforms import Builder, PipelineFiles, collect_transforms

PY_TRANSFORM = (
    "from laurelin.transforms import transform, Input, Output\n\n"
    "@transform(output=Output('clean'), raw=Input('raw'))\n"
    "def clean(raw):\n"
    "    return raw\n"
)


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="authoring")
    DatasetCatalog(ws, MetadataStore(ws.metadata_path)).write(
        "raw", pa.table({"x": [1, 2, 3]})
    )
    return ws


@pytest.fixture()
def files(ws):
    return PipelineFiles(ws.pipelines_dir)


def test_write_read_list_delete(files):
    result = files.write("mypipe", PY_TRANSFORM)
    assert result["name"] == "mypipe"
    assert result["transforms"] == ["clean"]
    assert result["collect_error"] is None

    assert files.read("mypipe")["content"] == PY_TRANSFORM
    listing = files.list()
    # R1: the listing carries a boolean, not a traceback. A pipeline file is
    # `exec`-ed, so its import error is whatever an arbitrary library chose to
    # say — the detail belongs on the detail route and in the log.
    assert listing == [
        {"name": "mypipe", "transforms": ["clean"], "failed": False,
         "bytes": len(PY_TRANSFORM)}
    ]

    files.delete("mypipe")
    assert files.list() == []
    with pytest.raises(KeyError):
        files.read("mypipe")


def test_written_transform_is_buildable(ws, files):
    files.write("pipe", PY_TRANSFORM)
    catalog = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    registry = collect_transforms(ws.pipelines_dir)
    build = Builder(ws, catalog, MetadataStore(ws.metadata_path), registry).build()
    assert build.status.value == "succeeded"
    assert catalog.read("clean").num_rows == 3


def test_syntax_error_rejected_without_writing(files):
    with pytest.raises(ValueError, match="Syntax error"):
        files.write("broken", "def x(:\n")
    assert files.list() == []  # nothing written


def test_path_traversal_rejected(files):
    for bad in ["../evil", "sub/mod", "a.b", "UPPER", "9start", ".hidden"]:
        with pytest.raises(ValueError):
            files.write(bad, "x = 1\n")


def test_leaked_dotfile_temp_is_not_collected_or_executed(ws, files):
    files.write("good", PY_TRANSFORM)
    # Simulate a leaked temp / hidden file that would blow up if exec'd.
    (ws.pipelines_dir / ".good-abcd.py.tmp").write_text("raise RuntimeError('boom')\n")
    (ws.pipelines_dir / ".hidden.py").write_text("raise RuntimeError('boom')\n")
    # list() ignores dotfiles...
    assert [f["name"] for f in files.list()] == ["good"]
    # ...and collect_transforms never compiles/execs them (no RuntimeError).
    registry = collect_transforms(ws.pipelines_dir)
    assert [s.name for s in registry.all()] == ["clean"]


def test_collect_error_surfaced_for_duplicate_output(files):
    files.write("first", PY_TRANSFORM)
    # A second file producing the same output 'clean' is a cross-file conflict:
    # the write succeeds (syntax ok) but collect_error reports the clash.
    result = files.write("second", PY_TRANSFORM.replace("def clean", "def clean2"))
    assert result["collect_error"] is not None
    # A structured failure naming the file, not a formatted exception. It is
    # returned only to the EDITOR who just wrote the file.
    assert result["collect_error"]["code"] == "transform_failed"
    assert result["collect_error"]["subject"] == "pipeline:second"


def test_generate_sql_transform_detects_inputs(files):
    result = files.generate_sql_transform(
        "SELECT region, count(*) AS n FROM raw GROUP BY region",
        output="raw_by_region",
        dataset_names=["raw", "other"],
    )
    content = files.read(result["name"])["content"]
    assert '"raw": Input("raw")' in content
    assert "other" not in content  # not referenced in the SQL
    assert result["transforms"] == ["raw_by_region"]


def test_generate_refuses_existing_file(files):
    files.write("raw_by_region", PY_TRANSFORM)
    with pytest.raises(ValueError, match="already exists"):
        files.generate_sql_transform("SELECT 1", "out", ["raw"], name="raw_by_region")


# -- HTTP surface ------------------------------------------------------------

@pytest.fixture()
def client(ws):
    return TestClient(create_app(ws, no_auth=True))


def test_api_pipeline_crud(client):
    assert client.get("/api/v1/pipelines").json() == []
    w = client.put("/api/v1/pipelines/p1", json={"content": PY_TRANSFORM})
    assert w.status_code == 200
    assert w.json()["transforms"] == ["clean"]
    assert [p["name"] for p in client.get("/api/v1/pipelines").json()] == ["p1"]
    assert client.get("/api/v1/pipelines/p1").json()["content"] == PY_TRANSFORM
    assert client.delete("/api/v1/pipelines/p1").json() == {"ok": True}


def test_api_syntax_error_is_400(client):
    r = client.put("/api/v1/pipelines/bad", json={"content": "def x(:"})
    assert r.status_code == 400


def test_api_from_query(client):
    r = client.post(
        "/api/v1/pipelines/from-query",
        json={"sql": "SELECT count(*) AS n FROM raw", "output": "raw_count"},
    )
    assert r.status_code == 200
    content = client.get("/api/v1/pipelines/raw_count").json()["content"]
    assert '"raw": Input("raw")' in content


def test_a_pipelines_import_failure_is_structured_on_the_editor_only_detail_route(client):
    """A pipeline file is `exec`-ed, so its import error can say anything at
    all. Nothing of what it said is stored or served."""
    client.put("/api/v1/pipelines/boom", json={
        "content": 'raise RuntimeError("connect failed: password=S3KRET")\n'
    })
    listed = [p for p in client.get("/api/v1/pipelines").json() if p["name"] == "boom"]
    assert listed and listed[0]["failed"] is True
    assert "S3KRET" not in client.get("/api/v1/pipelines").text

    detail = client.get("/api/v1/pipelines/boom").json()
    assert detail["failure"]["code"] == "transform_failed"
    assert detail["failure"]["subject"] == "pipeline:boom"
    assert "S3KRET" not in str(detail["failure"])


def test_api_lock_pipelines(ws):
    locked = TestClient(create_app(ws, no_auth=True, lock_pipelines=True))
    assert locked.get("/api/v1/pipelines").status_code == 200  # read still allowed
    assert locked.put("/api/v1/pipelines/x", json={"content": "x=1"}).status_code == 403
    assert locked.delete("/api/v1/pipelines/x").status_code == 403


def _api_routes(app):
    """Every routable endpoint with a dependency tree, prefix applied.

    This FastAPI version defers `include_router` behind an `_IncludedRouter`
    wrapper whose `effective_candidates()` yields the fully-prefixed routes;
    older versions put plain `APIRoute`s straight on `app.routes`. Handle both
    so a FastAPI upgrade does not silently turn this test into a no-op — the
    final assertion below fails loudly if flattening ever finds nothing.
    """
    for route in app.routes:
        if hasattr(route, "dependant"):
            yield route
        elif hasattr(route, "effective_candidates"):
            yield from route.effective_candidates()


def _guarded_routes(app, guard) -> set[tuple[str, str]]:
    """Every (method, path) whose dependency tree contains `guard` by function
    identity — the decorator's `Depends(...)` list lands on `route.dependant`."""
    found: set[tuple[str, str]] = set()

    def walk(dependant) -> bool:
        if dependant.call is guard:
            return True
        return any(walk(sub) for sub in dependant.dependencies)

    for route in _api_routes(app):
        if walk(route.dependant):
            for method in route.methods or ():
                found.add((method, route.path))
    return found


def test_every_python_writing_route_carries_the_pipeline_lock_and_the_guarded_set_is_exactly_this_one(ws):
    """A new route that writes a `.py` MUST carry `require_pipelines_unlocked`
    and be added to the enumeration below; dropping the guard from any of these
    fails this test loudly. "Any future Python-authoring route is guarded" is
    not mechanically decidable, so this pins the exact set instead — if you are
    the author of a new `.py`-writing route, this failure is your reminder.

    Deliberately absent from the set: `POST /workspace/import` and
    `/workspace/import/from-path` check the lock *inside* the handler
    (`_refuse_if_pipelines_locked`, so the 403 can explain the CLI alternative)
    — the dependency tree cannot see those; they are pinned behaviourally by
    tests/test_portability_surfaces.py instead.
    """
    from laurelin.api.routes import require_flows_unlocked, require_pipelines_unlocked

    app = create_app(ws, no_auth=True)

    assert _guarded_routes(app, require_pipelines_unlocked) == {
        ("PUT", "/api/v1/pipelines/{name}"),
        ("DELETE", "/api/v1/pipelines/{name}"),
        ("POST", "/api/v1/pipelines/from-query"),
        # Eject writes a `.py` — it is the escape hatch back into code.
        ("POST", "/api/v1/flows/{name}/eject"),
        # The gate that makes imported `.py` runnable.
        ("POST", "/api/v1/workspace/import/acknowledge-pipelines"),
    }

    # The flows lock covers exactly the no-code writes, plus eject (which
    # consumes a flow file, so it refuses when EITHER lock is set).
    assert _guarded_routes(app, require_flows_unlocked) == {
        ("PUT", "/api/v1/flows/{name}"),
        ("DELETE", "/api/v1/flows/{name}"),
        ("POST", "/api/v1/flows/{name}/eject"),
    }

    # Belt and braces for the mutating half of /pipelines specifically: every
    # non-GET route under the prefix carries the code lock. Also proves the
    # flattening in `_api_routes` actually saw the API surface.
    seen = list(_api_routes(app))
    assert any("/api/v1/pipelines" in r.path for r in seen), (
        "route flattening found no /pipelines routes — FastAPI internals "
        "changed shape and this whole test is asserting over nothing"
    )
    guarded = _guarded_routes(app, require_pipelines_unlocked)
    for route in seen:
        methods = (route.methods or set()) - {"GET", "HEAD"}
        if "/api/v1/pipelines" in route.path and methods:
            assert guarded >= {(m, route.path) for m in methods}, (
                f"unguarded mutating pipeline route: {route.path}"
            )


# ---------------------------------------------------------------------------
# generate_sql_transform: the SQL an author wrote must be the SQL that runs
#
# Two measured defects, one loud and one silent. The silent one is the reason
# this matters: `write()` compiles the generated file before saving it, which
# catches a file that will not parse — and cannot catch a file that parses
# perfectly while holding SQL nobody wrote.
# ---------------------------------------------------------------------------

# Shared with tests/test_flow_compile.py: a value that breaks one SQL-generating
# surface is worth trying against the others.
from laurelin.core.approvals import ChangeTicket as _ChangeTicket  # noqa: E402
from tests.test_flow_compile import HOSTILE  # noqa: E402

_TICKET = _ChangeTicket(kind="local", actor="test")


def test_generate_sql_transform_round_trips_a_triple_quote_instead_of_failing_to_parse(
    tmp_path,
):
    """REVERT `_py_string` to the f-string into `query=\"\"\"…\"\"\"` and this
    fails with, measured verbatim on this tree:

        ValueError: Syntax error: unterminated triple-quoted string literal
        (detected at line 12) (outa.py, line 9)

    An author who wrote SQL was shown a Python line number, for a file they
    never saw and cannot open.
    """
    files = PipelineFiles(tmp_path / "pipelines")
    sql = 'SELECT """ FROM raw'
    files.generate_sql_transform(sql, "outa", ["raw"])

    registry = collect_transforms(tmp_path / "pipelines")
    assert registry.get("outa").query == sql


def test_generate_sql_transform_round_trips_a_backslash_byte_for_byte(tmp_path):
    """The SILENT half, and the reason this is a correctness fix.

    REVERT `_py_string` to the f-string and this fails: the authored query

        SELECT * FROM raw WHERE path = 'C:\\temp\\new' AND re = '\\d+'

    came back out of `collect_transforms` holding a real TAB (from `\\t`) and a
    real NEWLINE (from `\\n`) — `IDENTICAL: False`. The file was written, it
    compiled, the build ran, and every layer reported success while executing
    SQL the author never wrote.
    """
    files = PipelineFiles(tmp_path / "pipelines")
    sql = r"SELECT * FROM raw WHERE path = 'C:\temp\new' AND re = '\d+'"
    files.generate_sql_transform(sql, "outb", ["raw"])

    stored = collect_transforms(tmp_path / "pipelines").get("outb").query
    assert stored == sql
    assert "\t" not in stored  # the corruption, named
    assert "\n" not in stored


@pytest.mark.parametrize("value", HOSTILE, ids=repr)
def test_generate_sql_transform_round_trips_every_hostile_string_exactly(
    tmp_path, value
):
    files = PipelineFiles(tmp_path / "pipelines" / value.encode().hex()[:16])
    sql = f"SELECT * FROM raw WHERE note = {value}"
    files.generate_sql_transform(sql, "outc", ["raw"])
    # `sql.strip()` because `generate_sql_transform` has always stripped
    # surrounding whitespace before storing — that predates this fix and is
    # not what it is about. Everything *inside* must survive byte for byte.
    assert collect_transforms(files.dir).get("outc").query == sql.strip()


def test_a_multi_line_query_stays_readable_in_the_editor(tmp_path):
    """`repr` per LINE, not one escaped blob.

    A 30-line query collapsed onto a single escaped line is technically correct
    and unusable — and the CodeMirror editor is where an ejected flow is edited
    from then on.
    """
    files = PipelineFiles(tmp_path / "pipelines")
    files.generate_sql_transform("SELECT a\nFROM raw\nWHERE b = 1", "outd", ["raw"])
    content = (files.dir / "outd.py").read_text()
    assert "'SELECT a\\n'" in content
    assert "'FROM raw\\n'" in content


def test_a_generated_pipeline_can_be_given_its_inputs_explicitly(tmp_path):
    """Flow ejection supplies inputs from the IR, where they are a structural
    fact. The word-boundary regex cannot tell a table name from the same word
    inside a string literal, and is only a fallback."""
    files = PipelineFiles(tmp_path / "pipelines")
    files.generate_sql_transform(
        "SELECT 1 AS x", "oute", dataset_names=["never_mentioned"],
        inputs=["explicit_one"],
    )
    spec = collect_transforms(files.dir).get("oute")
    assert list(spec.inputs) == ["explicit_one"]


def test_a_pipeline_generated_from_a_query_cannot_declare_an_input_its_author_cannot_view(
    ws,
):
    """The regex used to run over EVERY dataset name in the workspace.

    A generated pipeline could therefore declare an input its author cannot
    view — and a transform builds as the *system*, so that input would then be
    read and its rows landed in a new, unrestricted dataset.
    """
    from laurelin.core.db import MetadataStore
    from laurelin.core.models import Role, User

    store = MetadataStore(ws.metadata_path)
    cat = DatasetCatalog(ws, store)
    cat.write("classified", pa.table({"x": [1]}))
    store.set_grants_for_dataset("classified", [
        {"subject_kind": "user", "subject": "someone_else",
         "can_view": True, "can_edit": True},
    ], ticket=_TICKET)
    store.create_user(User(id="7", username="ed", role=Role.editor), _pw("pw"))

    app = create_app(ws)
    editor = TestClient(app)
    editor.post("/api/v1/auth/login", json={"username": "ed", "password": "pw"})

    r = editor.post("/api/v1/pipelines/from-query", json={
        "sql": "SELECT * FROM classified", "output": "sneaky",
    })
    assert r.status_code == 200, r.text
    content = editor.get("/api/v1/pipelines/sneaky").json()["content"]
    assert "classified" not in content.split("query=")[0]  # not in inputs={...}


def _pw(password: str) -> str:
    from laurelin.core.auth import hash_password

    return hash_password(password)
