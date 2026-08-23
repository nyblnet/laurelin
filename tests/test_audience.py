"""R2: the structural guard, plus the invariants the audience machinery rests on.

The guard in this file is the one the brief asks for: **adding a new
viewer-readable route that carries author-written text must fail the suite**,
without anyone having remembered to list that route.

Why the guard is empirical rather than static
---------------------------------------------

The obvious implementation — walk ``app.routes``, read ``route.dependant``,
find ``require_viewer`` — was tried and **does not work on this codebase**.
FastAPI wraps included routers in ``fastapi.routing._IncludedRouter``, which
has no ``.routes``; you have to reach ``r.original_router.routes`` and
``r.include_context.prefix``. Worse, even after recursing the whole
``dependant`` tree, ``/api/v1/users``, ``/api/v1/tokens`` and every
``/api/v1/scim/v2/*`` route report **no gate at all**, which is false. Static
classification of a route's privilege is unreliable here, and a guard built on
it is a false-green machine.

So the guard is empirical: log in as a real principal and drive every route.

What "every route" has to mean, learned the hard way
----------------------------------------------------

The first version of this guard swept **GET only**, treated a 200 as the only
body worth checking, and `continue`d past anything that 404'd. Measured, that
inspected **24 of 144 routes** — six of them `/docs`-class freebies — and left
78 non-GET routes outside it by construction. Three things it could not see, all
of which were live:

* ``POST /dashboards/{name}/panels/{id}/run`` handed a viewer the panel's stored
  ``group_by`` and ``metrics[].property`` in a **400 body**. A POST, and a 4xx.
* ``GET /apps/{name}/objects`` did the same with an app's admin-authored
  ``filters`` as soon as the ontology drifted. A 4xx again.
* ``GET /sources/{name}` handed an **editor** an admin's connector endpoint. The
  guard only ever logged in as a viewer.

And ``GET /dashboards/{name}`` — the route that leaked in all three previous
rounds — was never checked once, because ``PATH_PARAMS["name"]`` was ``"sales"``
while the fixture's dashboard was called ``"board"``, so it 404'd and was
skipped in silence.

So this version: **every method, every route, both privilege levels, and the
body is asserted on whatever the status code is.** Every seeded record shares
one name so a single parameter addresses all of them, and a GET that 404s even
for an admin fails ``test_every_addressable_get_route_is_actually_reached_by_the_guard``
unless it is listed in ``UNSEEDED_GETS`` with a reason.
"""

import ast
import json
import pathlib

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import serialize
from laurelin.core.audience import (
    Audience,
    Governed,
    author_role,
    field_audience,
    field_author_role,
)
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.failure import Failure, FailureCode, Phase
from laurelin.core.models import (
    AnalysisCell,
    AnalysisInfo,
    BuildStatus,
    BuildTaskInfo,
    DashboardInfo,
    DashboardPanel,
    DatasetInfo,
    ObjectAppInfo,
    Role,
    ScheduleInfo,
    SourceInfo,
)

REPO = pathlib.Path(__file__).resolve().parents[1] / "laurelin"
CREDS = {"username": "root", "password": "trustno1!"}

# One unique token per operational free-text field. If any of these reaches a
# viewer's 200 body, R2 is broken somewhere.
SENTINEL = "S3NT1NEL"


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------

def _governed_models() -> list[type]:
    import laurelin.core.models as models

    seen = {Failure}
    for name in dir(models):
        obj = getattr(models, name)
        if isinstance(obj, type) and issubclass(obj, Governed) and obj is not Governed:
            seen.add(obj)
    return sorted(seen, key=lambda c: c.__name__)


def test_every_model_the_api_serializes_declares_an_author_role_and_field_audiences():
    """A field added tomorrow with no annotation is OPERATIONAL, which fails
    closed — but silently. This makes it fail loudly instead, at the one moment
    somebody can still think about who the field is for."""
    for model in _governed_models():
        assert isinstance(author_role(model), Role), model.__name__
        for name in model.model_fields:
            assert isinstance(field_audience(model, name), Audience)
            # PRESENTATION means "written for a lower-privileged reader" and
            # AuthoredBy means "written by a higher-privileged one". A field
            # claiming both is a contradiction, and the serializer would
            # silently resolve it in the projection branch's favour.
            if field_audience(model, name) is Audience.PRESENTATION:
                assert field_author_role(model, name) is author_role(model), (
                    f"{model.__name__}.{name} is PRESENTATION and AuthoredBy at "
                    "once: a caption cannot also be written above its record"
                )
        # A Governed model that declares nothing PRESENTATION is legal (a
        # schedule is machine instructions end to end) but it must be a
        # decision: assert the class carries a docstring saying so.
        if not any(
            field_audience(model, n) is Audience.PRESENTATION
            for n in model.model_fields
        ):
            assert model.__doc__, (
                f"{model.__name__} discloses nothing below "
                f"{author_role(model).value}; say why in its docstring"
            )


def test_the_effective_role_defaults_to_the_lowest_privilege_when_nothing_set_it():
    """Outside any request there is no principal, so the serializer discloses
    the least. Forgetting redacts more, never less."""
    dash = DashboardInfo(
        name="d", title="D",
        panels=[DashboardPanel(id="p", title="P", sql=f"SELECT '{SENTINEL}'")],
    )
    out = serialize.dump(dash)
    assert out["title"] == "D"
    assert "sql" not in out["panels"][0]
    assert SENTINEL not in str(out)


def test_the_projection_omits_operational_keys_rather_than_blanking_them():
    """A blank `sql` is a value a read-modify-write client will happily PUT
    back over the real one. Absence is not a value."""
    dash = DashboardInfo(name="d", panels=[DashboardPanel(id="p", sql="SELECT 1")])
    panel = serialize.dump_as(dash, Role.viewer)["panels"][0]
    assert "sql" not in panel and "object_type" not in panel
    # `flow` and `top` are the Explore panel's query half; absent for the same
    # mechanical reason `sql` is — no annotation, no field.
    assert "flow" not in panel and "top" not in panel
    assert set(panel) == {
        "id", "title", "chart", "x", "y", "series", "stacked", "width",
    }


def test_an_editor_receives_the_whole_record_they_could_have_written():
    dash = DashboardInfo(name="d", panels=[DashboardPanel(id="p", sql="SELECT 1")])
    assert serialize.dump_as(dash, Role.editor)["panels"][0]["sql"] == "SELECT 1"


def test_the_memoized_projection_still_answers_exactly_what_the_rules_answer():
    """`_projection` is a cache in front of R2, so it must be R2.

    Deciding which keys a reader gets was 68% of serialization time when it ran
    per field per record, so it is memoized on
    ``(class, reader role, author role, narrowed)``. That is only safe while the
    answer depends on nothing else — no field *value*, no instance state — so
    this recomputes the rule from `field_audience` / `field_author_role`
    directly and demands the cache agree on every combination that exists.

    Iterating *every* combination is the point rather than thoroughness for its
    own sake: a cache keyed on too little returns whichever answer it computed
    first to every colliding caller, so a dropped key component shows up here as
    one combination answering with another's field list.
    """
    for model in _governed_models():
        for reader in Role:
            for author in Role:
                for narrowed in (False, True):
                    full, fields = serialize._projection(model, reader, author, narrowed)
                    assert full == (not narrowed and reader.covers(author))
                    expected = tuple(
                        (name, f.alias or name)
                        for name, f in model.model_fields.items()
                        if (
                            reader.covers(field_author_role(model, name, author))
                            if full
                            else field_audience(model, name) is Audience.PRESENTATION
                        )
                    )
                    assert fields == expected, (
                        f"{model.__name__} reader={reader.value} "
                        f"author={author.value} narrowed={narrowed}"
                    )


def test_the_scalar_fast_path_answers_exactly_what_the_full_check_answers():
    """`_holds_model` decides whether a value gets *projected* or copied raw.

    It has an exact-type fast path in front of it for speed, and a fast path
    that disagrees with the check it front-runs is a disclosure: a value wrongly
    called scalar is copied from `model_dump` whole, which is precisely how a
    nested operational field reaches a reader who may not have it. So the fast
    path is asserted against the check without it — including the values that
    make an exact-type test different from an isinstance test.
    """
    from pydantic import BaseModel

    def reference(value):  # `_holds_model` as it reads with the fast path removed
        if isinstance(value, BaseModel):
            return True
        if isinstance(value, (list, tuple)):
            return any(reference(v) for v in value)
        if isinstance(value, dict):
            return any(reference(v) for v in value.values())
        return False

    class Sub(str):  # a str subclass is NOT the exact type `str`
        pass

    panel = DashboardPanel(id="p", sql="SELECT 1")
    battery = [
        "", "text", 0, 1, -1, 1.5, True, False, None,
        Sub("subclassed"), Role.admin, BuildStatus.failed,  # str-valued enums
        [], (), {}, [1, 2], {"a": "b"}, ("x",),
        panel, [panel], {"p": panel}, [[panel]], {"k": [panel]},
        [{"deep": {"deeper": [panel]}}],
        [1, "two", None], {"a": {"b": {"c": 3}}},
    ]
    for value in battery:
        assert serialize._holds_model(value) is reference(value), repr(value)


def test_the_projection_cache_is_keyed_on_every_input_the_answer_turns_on():
    """Each key component earns its place by changing an answer.

    A component that never changes an answer is one somebody will drop from the
    key during a later cleanup — and the collision it opens discloses a field,
    silently, to a reader who should not have it. These are the concrete
    disclosures each component prevents.
    """
    proj = serialize._projection
    # reader: a viewer must not get what an editor gets (panels[].sql).
    assert proj(DashboardPanel, Role.viewer, Role.editor, False) != \
        proj(DashboardPanel, Role.editor, Role.editor, False)
    # author: the same reader against a stricter record discloses less.
    assert proj(DashboardPanel, Role.editor, Role.editor, False) != \
        proj(DashboardPanel, Role.editor, Role.admin, False)
    # narrowed: nesting inside an unauthorable record forces the projection.
    assert proj(DashboardPanel, Role.editor, Role.editor, True) != \
        proj(DashboardPanel, Role.editor, Role.editor, False)
    # class: two classes with the same roles have different fields.
    assert proj(DashboardPanel, Role.admin, Role.admin, False) != \
        proj(DatasetInfo, Role.admin, Role.admin, False)


def test_a_model_that_forgot_to_declare_an_author_role_is_admin_only():
    """New model ⇒ fails closed. Anything not Governed is treated as
    admin-authored, so a model added tomorrow discloses nothing by default."""
    from pydantic import BaseModel

    class Undeclared(BaseModel):
        secret: str = SENTINEL

    assert author_role(Undeclared) is Role.admin
    assert serialize.dump_as(Undeclared(), Role.editor) == {}
    assert serialize.dump_as(Undeclared(), Role.admin) == {"secret": SENTINEL}


def test_a_failure_nested_in_a_build_projects_to_code_and_subject_for_a_viewer():
    task = BuildTaskInfo(
        transform_name="clean", output_dataset="out",
        failure=Failure(
            code=FailureCode.AUTH_REJECTED, phase=Phase.authenticate,
            subject="transform:clean", endpoint="db.internal:5432",
            vendor_code="28P01", exc_class="OperationalError",
        ),
    )
    seen = serialize.dump_as(task, Role.viewer)["failure"]
    assert seen == {"code": "auth_rejected", "subject": "transform:clean"}
    # An editor owns the transform, so they keep the diagnostic fields whose
    # values are closed sets or shape-gated — but NOT `endpoint`, which is
    # rebuilt from an admin's connector config. See the next test.
    full = serialize.dump_as(task, Role.editor)["failure"]
    assert full["exc_class"] == "OperationalError"
    assert full["vendor_code"] == "28P01"
    assert "endpoint" not in full
    assert serialize.dump_as(task, Role.admin)["failure"]["endpoint"] == "db.internal:5432"


def test_a_nested_record_can_never_be_disclosed_more_widely_than_its_parent():
    """Recursion narrows. It used to widen, and that was a live disclosure.

    `SourceInfo` is admin-authored and `Failure` is editor-authored, so an
    editor reading `GET /sources/{name}` got a *projection* of the source —
    correctly withholding `config` — and then a **full** dump of the nested
    failure, whose `endpoint` is rebuilt from that very config. The one field
    `source_routes._public` exists to keep from an editor round-tripped back
    through a field annotated PRESENTATION.
    """
    source = SourceInfo(
        name="crm", type="postgres", dataset="sales",
        config={"url": f"postgresql://svc:{SENTINEL}@db.internal:5432/crm"},
        created_by="root",
        last_sync_failure=Failure(
            code=FailureCode.AUTH_REJECTED, subject="source:crm",
            endpoint="db.internal:5432",
        ),
    )
    seen = serialize.dump_as(source, Role.editor)
    assert "config" not in seen
    assert seen["last_sync_failure"] == {
        "code": "auth_rejected", "subject": "source:crm"
    }, "the nested failure inherited a clearance its parent did not grant"


def test_a_field_authored_above_its_record_is_withheld_from_the_records_author():
    """`DatasetInfo` is editor-authored; `DatasetInfo.source` is written only by
    the three ADMIN registration routes.

    This is the last place in the tree where somebody's confidentiality rested
    on the free-text matcher, and it is the reason `AuthoredBy` exists.
    Measured before it did: a plain editor read five live credentials out of
    `GET /datasets/{name}` — a quoted libpq conninfo, a quoted ODBC keyword
    string, a colon-delimited form, a bare AWS key pair and a positional JDBC
    URL — because `keyword_credential` returned False for all five. Adding a
    quote after `password=` was enough to defeat the boundary.
    """
    info = DatasetInfo(
        name="remote", kind="federated",
        source={"type": "postgres", "table": "public.crm",
                "conninfo": f"host=db user=svc password='{SENTINEL}'"},
    )
    for role in (Role.viewer, Role.editor):
        seen = serialize.dump_as(info, role)
        assert "source" not in seen, role
        assert SENTINEL not in str(seen), role
        # ...and the product still works: shape, built by us, from an allowlist.
        assert seen["source_descriptor"] == {"type": "postgres", "table": "public.crm"}
    assert "source" in serialize.dump_as(info, Role.admin)


def test_the_source_descriptor_is_built_from_an_allowlist_not_by_subtraction():
    """Every key absent unless named, every value absent unless it is shaped
    like an identifier. Nothing here asks whether a string 'looks like' a
    secret — that question is what three rounds of this bug walked around."""
    from laurelin.core.models import source_descriptor

    assert source_descriptor({
        "type": "postgres",
        "table": "public.orders",
        # On the list, but not shaped like an identifier: a path, a URL, a
        # keyword string. Absent.
        "database": "host=db password='hunter2'",
        # Not on the list, whatever it holds.
        "url": f"postgresql://svc:{SENTINEL}@db:5432/x",
        "path": f"s3://k:{SENTINEL}@bucket/t.parquet",
        "options": {"secret": SENTINEL},
        "some_key_nobody_thought_of": f"Pwd={SENTINEL};",
    }) == {"type": "postgres", "table": "public.orders"}


def test_the_author_escape_hatch_is_only_used_off_the_http_path():
    """`as_author` is the deliberate, greppable opt-out for the CLI and the
    export writer, where possession of the workspace directory *is* the
    credential. It must never appear under laurelin/api/."""
    def calls_as_author(tree) -> bool:
        """A *call*, by AST. Matching the string finds the docstring that
        explains the rule and the comment above the call — which is how the
        first version of this test stayed green after both real call sites were
        removed."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "as_author":
                return True
        return False

    offenders, users = [], []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if not calls_as_author(ast.parse(path.read_text())):
            continue
        if rel.parts[0] in ("cli.py", "export"):
            users.append(str(rel))
        elif rel.parts[0] != "core":  # core/serialize.py *defines* it
            offenders.append(str(rel))
    assert offenders == [], f"as_author used on the HTTP path: {offenders}"
    # ...and it must actually be used off it. This assertion is the fix for a
    # test that passed vacuously: `as_author` had no production caller at all,
    # so "it is not used on the HTTP path" was true of a function nobody called,
    # and R2 in practice reached only `laurelin/api` — the exact outcome the
    # move of `_dump` into `core` was made to avoid.
    assert users, (
        "as_author has no caller outside laurelin/api — this guard passes "
        "vacuously and R2's escape hatch is documentation only"
    )


def test_no_api_module_serializes_a_model_outside_the_serializer():
    """`model_dump` in a route is a bypass of R2 by definition.

    This used to allowlist ``routes.py``, ``auth_routes.py`` and
    ``export_routes.py`` **wholesale**, which is three of the four files that
    matter — so it could not see `routes.py`'s
    ``[c.model_dump() for c in info.schema_]`` on `GET /datasets/{name}/schema`,
    or the five in `auth_routes.py`. Neither leaked, because every field of
    every model involved happened to be PRESENTATION; both were unannotated
    paths where a field added tomorrow ships to whoever can reach the route,
    which is exactly the fail-closed property `audience.py` claims for new
    fields.

    Per **line** now, and the opt-out is an inline `# serialize-ok:` marker with
    a reason on it — so a new bypass costs its author a sentence, and a reader
    of that sentence can disagree.
    """
    offenders = []
    for path in sorted((REPO / "api").glob("*.py")):
        lines = path.read_text().splitlines()
        for n, line in enumerate(lines, start=1):
            if ".model_dump(" not in line and ".model_dump_json(" not in line:
                continue
            # The marker may sit on the call or in the comment immediately above
            # it, because the reason usually needs more than a trailing clause.
            context = "\n".join(lines[max(0, n - 6):n])
            if "serialize-ok:" in context:
                continue
            offenders.append(f"{path.name}:{n}: {line.strip()[:80]}")
    assert offenders == [], (
        "models serialized outside serialize.dump — route it through "
        "`serialize.dump`, or mark the line `# serialize-ok: <why>`:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# The structural guard
# ---------------------------------------------------------------------------

# Two sentinels, one per authoring level, because a single one cannot express
# the rule. A viewer may read neither. An *editor* may legitimately read
# editor-authored text — that is the whole point of R2 — but must never read
# admin-authored text, and a guard that only ever logs in as a viewer cannot see
# that crossing. One was live and shipped: an editor read an admin's connector
# endpoint out of `GET /sources/{name}`.
SENTINEL_ADMIN = f"{SENTINEL}ADM"
SENTINEL_EDITOR = f"{SENTINEL}EDT"

# Every seeded record shares one name so that a single `PATH_PARAMS["name"]`
# addresses all of them. That is not a tidiness preference: with a dataset
# called "sales", a dashboard called "board" and a source called "feed", the
# guard sent `GET /api/v1/dashboards/sales`, got a 404, and `continue`d — so
# `GET /dashboards/{name}`, the route that leaked in all three previous rounds,
# was never once checked.
SEEDED = "sales"

_ONTOLOGY = """
object_types:
  - api_name: aircraft
    backing_dataset: sales
    primary_key: region
    title_property: region
    properties:
      region: {type: string}
      amount: {type: double}
"""


@pytest.fixture()
def sentinel_workspace(tmp_path):
    """Seed every operational free-text field with a token, at both levels."""
    ws = Workspace.init(tmp_path / "ws", name="sentinel")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write(SEEDED, pa.table({"region": ["eu"], "amount": [1.0]}))
    (ws.ontology_dir / "o.yml").write_text(_ONTOLOGY)

    # A federated dataset whose DSN is the classic shape. ADMIN: only the three
    # registration routes write `datasets.source_json`.
    store.upsert_dataset("crm", "remote crm")
    store.set_dataset_source("crm", "federated", {
        "type": "postgres", "table": "public.crm",
        "url": f"postgresql://svc:{SENTINEL_ADMIN}_DSN@pg.internal:5432/crm",
        # The five shapes that walked past `keyword_credential`. Under R2 none
        # of them is serialized below admin at all, so none of them needs to be
        # recognised.
        "conninfo": f"host=pg user=svc password='{SENTINEL_ADMIN}_QUOTED'",
        "odbc": f"Server=pg;Uid=svc;Pwd='{SENTINEL_ADMIN}_ODBCQ';",
        "colon": f"Server:pg User:svc Password:{SENTINEL_ADMIN}_COLON",
        "aws": f"AKIAIOSFODNN7EXAMPLE wJalrXUtnFEMI/{SENTINEL_ADMIN}_AWS",
        "jdbc": f"jdbc:mysql://pg:3306/prod,svc,{SENTINEL_ADMIN}_JDBC",
    })

    # A connector config, an ODBC keyword string under a non-url key. ADMIN.
    store.upsert_source(SourceInfo(
        name=SEEDED, type="http", dataset=SEEDED,
        config={"url": f"https://api.example.com/e.csv?api_key={SENTINEL_ADMIN}_HTTP",
                "path": f"Server=db;Uid=a;Pwd={SENTINEL_ADMIN}_ODBC;"},
        created_by="root",
    ))
    store.record_source_sync(SEEDED, "failed", failure=Failure(
        code=FailureCode.AUTH_REJECTED, subject=f"source:{SEEDED}",
        endpoint=f"{SENTINEL_ADMIN.lower()}.internal:5432"))

    # A schedule whose target and source are free-form. EDITOR.
    store.upsert_schedule(ScheduleInfo(
        name=SEEDED, trigger="cron", cron="0 3 * * *", action="sync",
        source=SEEDED, targets=[f"s3://k:{SENTINEL_EDITOR}_SCHED@bucket/t"],
        created_by="root",
    ))
    store.record_schedule_run(SEEDED, "failed", failure=Failure(
        code=FailureCode.TRANSFORM_FAILED, subject=f"schedule:{SEEDED}"))

    # Dashboard panels: the field three rounds of this bug leaked, plus every
    # *instruction* field of an object panel. Note what is deliberately NOT
    # seeded: `title` and `metrics[].alias`. Both are captions — labels an
    # editor chooses for the reader's chart and the reader's column header —
    # and they reach a viewer by design, exactly as `panel.title` does. The
    # honest invariant is about instructions, not about labels.
    store.upsert_dashboard(DashboardInfo(
        name=SEEDED, title="Board",
        panels=[
            DashboardPanel(
                id="p1", title="P",
                sql=f"SELECT * FROM postgres_scan('host=db password={SENTINEL_EDITOR}_SQL')",
            ),
            DashboardPanel(
                id="p2", title="Objects", object_type="aircraft",
                group_by=[f"{SENTINEL_EDITOR}_GROUPBY"],
                metrics=[{"op": f"{SENTINEL_EDITOR}_OP",
                          "property": f"{SENTINEL_EDITOR}_PROP", "alias": "n"}],
                filters={f"{SENTINEL_EDITOR}_FILTER": "x"},
                search=f"{SENTINEL_EDITOR}_SEARCH",
            ),
        ],
        created_by="root",
    ))

    # An analysis: the same split as a dashboard, with the two extra
    # instruction fields cells carry — `flow` (whose filter values are author
    # text) and `inputs` (the instruction graph). Titles are captions and stay
    # unseeded, exactly as panel titles are.
    store.upsert_analysis(AnalysisInfo(
        name=SEEDED, title="Notebook",
        cells=[
            AnalysisCell(
                id="c1", title="C1",
                sql=f"SELECT * FROM postgres_scan('host=db password={SENTINEL_EDITOR}_CELLSQL')",
            ),
            AnalysisCell(
                id="c2", title="C2", inputs=["c3"], top=7,
                flow={"terminal": "s1", "nodes": [
                    {"id": "s1", "kind": "filter", "inputs": ["cell:c3"],
                     "params": {"predicate": {"t": "op", "op": "eq", "args": [
                         {"t": "col", "name": f"{SENTINEL_EDITOR}_CELLCOL"},
                         {"t": "lit", "type": "string",
                          "value": f"{SENTINEL_EDITOR}_CELLVALUE"}]}}}]},
            ),
            AnalysisCell(
                id="c3", title="C3",
                flow={"terminal": "s1", "nodes": [
                    {"id": "s1", "kind": "source", "inputs": [],
                     "params": {"dataset": SEEDED}}]},
            ),
        ],
        created_by="root",
    ))

    # An object app whose stored filters are ADMIN-authored instructions, named
    # against a property that does not exist — the ontology-drift case, which is
    # what turned this route into an oracle.
    store.upsert_object_app(ObjectAppInfo(
        name=SEEDED, title="Ops", object_type="aircraft", columns=["region"],
        filters={f"{SENTINEL_ADMIN}_APPFILTER": "x"},
        created_by="root",
    ))

    # A delegated engine. ADMIN.
    store.upsert_engine(
        SEEDED, "flightsql", f"grpc+tls://trino:443?token={SENTINEL_ADMIN}_ENGINE",
        {"adbc.flight.sql.authorization_header": f"Bearer {SENTINEL_ADMIN}_HDR"},
        created_by="root",
    )

    # A pipeline file that will not import, so its traceback is author text.
    ws.pipelines_dir.mkdir(parents=True, exist_ok=True)
    (ws.pipelines_dir / f"{SEEDED}.py").write_text(
        f'raise RuntimeError("connect failed: password={SENTINEL_EDITOR}_PIPE")\n'
    )

    # A no-code flow, so `/flows`, `/flows/{name}` and `/flows/{name}/sql` are
    # addressable and the sweep actually reaches them. A flow is a *file* in
    # pipelines/, not a row in metadata.db, so nothing else here would seed one.
    #
    # No sentinel goes in it, and that is the finding rather than an omission:
    # a flow has no operational free-text field to leak. Every value an author
    # supplies is a bound parameter, every identifier is a schema-validated
    # column name, and everything else is a closed enum — so there is no
    # equivalent of `panel.sql` or a pipeline's source to withhold. Its one
    # free-text field, `description`, is a *caption*: it becomes the output
    # dataset's description exactly as `Output(..., description=...)` does for
    # a Python transform, and reaches a viewer by design, like `panel.title`.
    (ws.pipelines_dir / f"{SEEDED}.flow.json").write_text(json.dumps({
        "name": SEEDED, "output": SEEDED, "author": "root",
        "description": "Sales, by region", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": SEEDED}}],
    }))

    # A failed build, so `builds.failure_json` and its task are populated.
    build = store.create_build([SEEDED])
    store.update_build(build.id, status=BuildStatus.failed, failure=Failure(
        code=FailureCode.TRANSFORM_FAILED, subject=f"build:{build.id}"))
    store.upsert_build_task(build.id, BuildTaskInfo(
        transform_name="clean", output_dataset=SEEDED, status=BuildStatus.failed,
        failure=Failure(code=FailureCode.TRANSFORM_FAILED,
                        subject="transform:clean",
                        endpoint=f"{SENTINEL_ADMIN.lower()}.internal:5432")))
    PATH_PARAMS["build_id"] = build.id

    # Audit rows at both levels, written the way a careless caller would.
    store.log_audit("source_sync_failed",
                    {"reason": f"driver said {SENTINEL_ADMIN}_AUDIT"}, actor="root")
    store.log_audit("dashboard_updated",
                    {"reason": f"driver said {SENTINEL_EDITOR}_AUDIT_ED"},
                    actor="vic", min_read_role=Role.editor)
    return ws


@pytest.fixture()
def sentinel_app(sentinel_workspace):
    # `raise_server_exceptions=False`: the sentinel workspace deliberately holds
    # a broken pipeline and half-registered datasets, so several routes 500. A
    # 500 is not viewer-readable and the guard should record it as a status
    # code, not re-raise it into the test.
    app = create_app(sentinel_workspace)
    admin = TestClient(app, raise_server_exceptions=False)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    assert admin.post(
        "/api/v1/users",
        json={"username": "vic", "password": "password123", "role": "viewer"},
    ).status_code in (200, 201)
    viewer = TestClient(app, raise_server_exceptions=False)
    assert viewer.post(
        "/api/v1/auth/login", json={"username": "vic", "password": "password123"}
    ).status_code == 200
    return app, admin, viewer


# Concrete values for every path parameter in the route table. A route whose
# parameter is not here FAILS the test rather than being skipped, so a new route
# cannot slip through by being unaddressable.
PATH_PARAMS = {
    "name": SEEDED,
    "type_name": "aircraft",
    # Ontology definition authoring routes. "aircraft" is defined in the
    # fixture's hand-written YAML, so the admin sweep's PUT is a 409 (refuses
    # to shadow a hand-written file) and DELETE likewise — probed without
    # mutating the ontology the rest of the sweep depends on.
    "api_name": "aircraft",
    "build_id": "b1",
    "panel_id": "p1",
    "cell_id": "c1",
    "pk": "eu",
    "link_name": "operated_by",
    "username": "vic",
    "slug": "ws",
    "branch": "main",
    "token_id": "t1",
    "group": "g1",
    "user_id": "u1",
    "id": "1",
    "resource": "Users",
    "job_id": "j1",
    "path": SEEDED,
    "filename": "x.csv",
    "app_name": SEEDED,
    "version": "1",
    "scim_id": "u1",
}


def _enumerate_routes(app) -> list[tuple[str, str]]:
    """(method, path) for every route, recursing through FastAPI's
    _IncludedRouter wrapper.

    Naively walking `app.routes` yields **3** routes on this app because the
    included routers are wrapped; the floor assertion below exists so that if a
    future FastAPI changes this internal again, the guard fails loudly instead
    of quietly passing over an empty list.
    """
    out: list[tuple[str, str]] = []

    def walk(routes, prefix=""):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                ctx = getattr(r, "include_context", None)
                walk(inner.routes, prefix + getattr(ctx, "prefix", ""))
                continue
            path = getattr(r, "path", None)
            if path is None:
                continue
            for method in sorted(getattr(r, "methods", None) or []):
                out.append((method, prefix + path))

    walk(app.routes)
    return sorted(set(out))


# GET routes the fixture deliberately cannot address, each with the reason. A
# GET route that 404s for the *admin* and is not listed here fails the coverage
# test: that is what stops a route being silently skipped.
UNSEEDED_GETS = {
    "/api/v1/auth/oidc/login": "no IdP configured in this fixture",
    "/api/v1/auth/oidc/callback": "no IdP configured in this fixture",
    "/api/v1/auth/saml/login": "no IdP configured in this fixture",
    "/api/v1/auth/saml/metadata": "no IdP configured in this fixture",
    "/api/v1/auth/saml/acs": "no IdP configured in this fixture",
    "/api/v1/scim/v2/{resource}/{scim_id}": "SCIM ids are provisioner-issued",
    "/api/v1/tokens/{token_id}": "token ids are issued, not seeded",
    "/api/v1/groups/{group}": "group membership is not part of this fixture",
    "/api/v1/users/{user_id}": "user ids are issued, not seeded",
    "/api/v1/workspaces/{slug}": "single-workspace app; no control plane",
    "/api/v1/workspaces/{slug}/members": "single-workspace app; no control plane",
    "/api/v1/exports/{job_id}": "export jobs are created, not seeded",
    "/api/v1/exports/{job_id}/download": "export jobs are created, not seeded",
    "/api/v1/imports/{job_id}": "import jobs are created, not seeded",
    "/api/v1/uploads/{filename}": "uploads are transient",
    "/api/v1/ontology/objects/{type_name}/{pk}/links/{link_name}":
        "the fixture ontology declares no link types",
    "/api/v1/workspaces": "single-workspace app; there is no control plane here",
    "/api/v1/workspace/import/report": "nothing has been imported into this workspace",
    "/api/v1/scim/v2/Users": "SCIM is off unless a provisioner is configured",
    "/api/v1/scim/v2/Users/{scim_id}": "SCIM is off unless a provisioner is configured",
    "/api/v1/scim/v2/Groups": "SCIM is off unless a provisioner is configured",
    "/api/v1/scim/v2/Groups/{name}": "SCIM is off unless a provisioner is configured",
    "/api/v1/scim/v2/ServiceProviderConfig": "SCIM is off unless a provisioner is configured",
}

# Bodies for the non-GET sweep. `{}` unless a route needs more to get past
# validation and actually run — and `POST /dashboards/{n}/panels/{id}/run` needs
# to actually run, because it is the route whose failure path was an oracle.
ROUTE_BODIES = {
    "/api/v1/dashboards/{name}/panels/{panel_id}/run": {"max_rows": 10},
    "/api/v1/query": {"sql": "SELECT 1"},
}


# One parameter name can mean two different things. `{name}` is a dataset, a
# dashboard, a source, a schedule, an engine, an app *and* an object type, and
# the object type is called "aircraft" because that is what an ontology looks
# like. Keyed on the route path, so a route cannot 404 its way out of the sweep
# by sharing a parameter name with something else.
PATH_PARAM_OVERRIDES = {
    "/api/v1/ontology/object-types/{name}": {"name": "aircraft"},
    "/api/v1/ontology/object-types/{name}/permissions": {"name": "aircraft"},
    "/api/v1/ontology/object-types/{name}/grants": {"name": "aircraft"},
    "/api/v1/ontology/object-types/{name}/edit-log": {"name": "aircraft"},
    "/api/v1/ontology/object-types/{name}/edit-log/prune": {"name": "aircraft"},
}


def _concrete(path: str) -> tuple[str, str | None]:
    """(url, missing_param_name). A parameter with no mapping is an error, not
    a skip."""
    params = {**PATH_PARAMS, **PATH_PARAM_OVERRIDES.get(path, {})}
    out = path
    for part in path.split("/"):
        if part.startswith("{") and part.endswith("}"):
            key = part[1:-1].split(":")[0]
            if key not in params:
                return out, key
            out = out.replace(part, params[key])
    return out, None


# Exactly two, by full path rather than by prefix: a `startswith` here would
# quietly exclude any future route that happens to share the prefix.
_SESSION_ENDING = frozenset({"/api/v1/auth/login", "/api/v1/auth/logout"})


def _sweep(client, app):
    """Every route, every method, as this client. Yields (method, url, response).

    DELETE last, so a route that succeeds in removing a seeded record cannot
    silently reduce what the rest of the sweep reaches.
    """
    order = {"GET": 0, "HEAD": 1, "POST": 2, "PUT": 3, "PATCH": 4, "DELETE": 5}
    routes = sorted(_enumerate_routes(app), key=lambda mp: (order.get(mp[0], 9), mp[1]))
    unmapped = []
    for method, path in routes:
        if path in _SESSION_ENDING:
            continue
        url, missing = _concrete(path)
        if missing:
            unmapped.append(f"{method} {path} (add {missing!r} to PATH_PARAMS)")
            continue
        body = ROUTE_BODIES.get(path, {})
        try:
            r = client.request(method, url, json=None if method in ("GET", "HEAD") else body)
        except Exception as exc:  # noqa: BLE001 - a crash is a result, not a skip
            raise AssertionError(f"{method} {url} raised {exc!r}") from exc
        yield method, url, r
    assert not unmapped, "unmapped path parameters: " + "; ".join(unmapped)


def test_no_viewer_readable_route_returns_operational_text(sentinel_app):
    """The guard. **Every route, every method**, checked for every sentinel.

    A 200 is not the only thing worth checking, and restricting to GET was the
    hole. Three of the confirmed disclosures this round came back in **4xx
    bodies** — a stored panel's `group_by` in a 400, an app's stored filter in a
    400, an operator's S3 credential in a 400 — and the route that carried the
    first of them is a **POST**. So: assert on the body whatever the status.

    Measured on the version this replaces: 24 of 144 routes inspected, six of
    them `/docs`-class freebies, 78 routes outside the sweep by construction,
    and `GET /dashboards/{name}` — the route that leaked in all three previous
    rounds — never checked at all, because `PATH_PARAMS["name"]` was `"sales"`
    and the fixture's dashboard was called `"board"`, so it 404'd and was
    `continue`d. Two probe routes added to prove it (a viewer-gated POST, and a
    viewer-gated GET under an unaddressed name) both returned panel SQL to a
    viewer with the suite fully green.
    """
    app, _admin, viewer = sentinel_app
    routes = _enumerate_routes(app)
    assert len(routes) >= 120, (
        f"only {len(routes)} routes discovered — the router walk is broken, "
        "not the API. A naive walk of `app.routes` finds 3."
    )

    seen = {}
    for method, url, r in _sweep(viewer, app):
        seen[method] = seen.get(method, 0) + 1
        for token in (SENTINEL_ADMIN, SENTINEL_EDITOR):
            assert token not in r.text, (
                f"{method} {url} -> {r.status_code} returned author-written "
                f"text to a viewer:\n{r.text[:500]}"
            )
    # Per-method floors. The old single floor passed at 24 with six freebies;
    # these say how much of each method's surface was actually driven.
    assert seen.get("GET", 0) >= 60, seen
    assert seen.get("POST", 0) >= 30, seen
    assert seen.get("PUT", 0) >= 15, seen
    assert seen.get("DELETE", 0) >= 15, seen


def test_no_editor_readable_route_returns_admin_authored_text(sentinel_app):
    """The same sweep one privilege level up — the level the old guard could not
    see, because it only ever logged in as a viewer.

    R2 is "if you cannot write it, you cannot read it", and an editor cannot
    write a connector config, an engine URI, a federated dataset's source or an
    object app. Two confirmed disclosures lived exactly here: a nested `Failure`
    restoring an admin's endpoint to an editor inside `GET /sources/{name}`, and
    an editor reading five live credentials out of `GET /datasets/{name}`.

    Editor-authored text is *not* asserted against: an editor reading their own
    panel's SQL is the product working.
    """
    app, admin, _viewer = sentinel_app
    assert admin.post(
        "/api/v1/users",
        json={"username": "ed", "password": "password123", "role": "editor"},
    ).status_code in (200, 201)
    editor = TestClient(app, raise_server_exceptions=False)
    assert editor.post(
        "/api/v1/auth/login", json={"username": "ed", "password": "password123"}
    ).status_code == 200

    checked = 0
    for method, url, r in _sweep(editor, app):
        checked += 1
        assert SENTINEL_ADMIN not in r.text, (
            f"{method} {url} -> {r.status_code} returned admin-authored text "
            f"to an editor:\n{r.text[:500]}"
        )
    assert checked >= 120, checked


def test_every_addressable_get_route_is_actually_reached_by_the_guard(sentinel_app):
    """The anti-silent-skip test, and the one that would have caught the hole.

    A GET route whose path parameters are all mapped must resolve **for the
    admin** — otherwise the fixture does not address it, the sweep above learns
    nothing from it, and nobody finds out. Listing it in `UNSEEDED_GETS` with a
    reason is the only way to opt out, which makes the opt-out reviewable.
    """
    app, admin, _viewer = sentinel_app
    unreached = []
    for method, path in _enumerate_routes(app):
        if method != "GET" or path in UNSEEDED_GETS:
            continue
        url, missing = _concrete(path)
        if missing:
            continue  # the sweep already fails on this
        r = admin.get(url)
        if r.status_code == 404:
            unreached.append(f"{url} (from {path})")
    assert unreached == [], (
        "these GET routes 404 even for an admin, so the guard learns nothing "
        "from them — seed them in `sentinel_workspace` or list them in "
        "`UNSEEDED_GETS` with a reason: " + "; ".join(unreached)
    )


def test_the_pipeline_source_is_not_readable_below_the_level_that_can_write_it(
    sentinel_app,
):
    """Writing a pipeline file is code-execution-equivalent — routes.py says so
    directly above the route. Reading one was VIEWER, and I confirmed live that
    a viewer read a driver-authored sentinel out of GET /pipelines/{name}.

    The viewer's lineage need is served structurally instead, and those two
    routes stay VIEWER: names and edges, not authored prose."""
    _app, admin, viewer = sentinel_app
    assert viewer.get("/api/v1/pipelines").status_code == 403
    assert viewer.get(f"/api/v1/pipelines/{SEEDED}").status_code == 403
    detail = admin.get(f"/api/v1/pipelines/{SEEDED}")
    assert detail.status_code == 200
    # The editor gets their own source back — of course; they wrote it, and the
    # textarea has to round-trip. What they do NOT get is the *driver's* words
    # about it: the failure is structured even here, at the highest privilege
    # on the path, because R1 converts at the catch site rather than at a route.
    assert SENTINEL_EDITOR in detail.json()["content"]
    assert SENTINEL_EDITOR not in json.dumps(detail.json()["failure"])
    assert detail.json()["failure"]["code"] == "transform_failed"
    # The viewer's lineage need is served structurally and stays VIEWER: edges,
    # not authored prose. (`/transforms` is skipped here only because this
    # workspace's pipeline is deliberately unimportable, which 500s the registry
    # dependency — an orthogonal, pre-existing behaviour.)
    assert viewer.get("/api/v1/lineage").status_code == 200


def test_readiness_tells_an_anonymous_caller_nothing_but_whether_it_is_ready(
    sentinel_app,
):
    _app, _admin, viewer = sentinel_app
    r = TestClient(_app).get("/health/ready")
    assert r.status_code in (200, 503)
    assert set(r.json()) <= {"status"}


def test_the_workspace_route_does_not_hand_a_viewer_the_servers_filesystem_path(
    sentinel_app,
):
    _app, admin, viewer = sentinel_app
    assert "root" not in viewer.get("/api/v1/workspace").json()
    assert "root" in admin.get("/api/v1/workspace").json()


# ---------------------------------------------------------------------------
# The escape-hatch AST guards
# ---------------------------------------------------------------------------

def test_the_free_text_matcher_has_only_the_callers_it_is_allowed(sentinel_app):
    """`authoring_hints` is not a boundary, and the way it stays not-a-boundary
    is that its caller list stays this short."""
    allowed = {
        pathlib.Path("api/routes.py"),
        pathlib.Path("api/schedule_routes.py"),
        pathlib.Path("core/failure.py"),
        pathlib.Path("core/authoring_hints.py"),
    }
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if rel in allowed:
            continue
        # An *import*, by AST — not a substring. `core/redaction.py` names this
        # module in its docstring to explain the split, and a guard that cannot
        # tell a sentence from a dependency is a guard nobody will keep.
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "authoring_hints" in (node.module or ""):
                offenders.append(str(rel))
            elif isinstance(node, ast.Import) and any(
                "authoring_hints" in a.name for a in node.names
            ):
                offenders.append(str(rel))
            elif (isinstance(node, ast.Attribute)
                  and isinstance(node.value, ast.Name)
                  and node.value.id == "authoring_hints"):
                offenders.append(str(rel))
    assert sorted(set(offenders)) == [], f"authoring_hints called from {set(offenders)}"


def test_no_store_write_persists_a_formatted_exception():
    """AST guard over laurelin/: `str(exc)` and f"{type(exc).__name__}: {exc}"
    may not flow into a `store.*` call.

    This is the guard that catches the *next* backend added the way StarRocks
    was — `routes.py`'s StarRocks registration never joined `_driver_failure`
    and shipped with no redaction at all on that path for a whole release.
    """
    offenders = []
    for path in REPO.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            target = ""
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                target = f"{func.value.id}.{func.attr}"
            if not target.startswith(("store.", "self.store.")):
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if _formats_an_exception(arg):
                    offenders.append(
                        f"{path.relative_to(REPO)}:{node.lineno} {target}"
                    )
    assert offenders == [], (
        "a formatted exception is being written to the store; convert it at the "
        "catch site with Failure.from_exception instead:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def _formats_an_exception(node: ast.AST) -> bool:
    """`str(exc)`, `f"...{exc}..."`, or `repr(exc)` for any name that looks like
    a caught exception."""
    names = {"exc", "e", "err", "error", "ex"}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id in ("str", "repr") and any(
                isinstance(a, ast.Name) and a.id in names for a in sub.args
            ):
                return True
        if isinstance(sub, ast.FormattedValue):
            for inner in ast.walk(sub.value):
                if isinstance(inner, ast.Name) and inner.id in names:
                    return True
    return False
