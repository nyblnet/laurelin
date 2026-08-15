"""What a flow author may read, and what the flow's output inherits.

The invariant these tests state, taken together: **a no-code author cannot use
a flow to read data they could not read directly, and cannot use one to strip a
row policy or a column mask off data they can.**

That is deliberately *stricter* than the Python transform path, which launders
all three. The last test in this file pins that gap as a documented fact rather
than leaving it to be discovered.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService
from laurelin.transforms import Builder, Input, Output, TransformRegistry, TransformSpec
from laurelin.transforms.flow_compile import compile_flow
from laurelin.transforms.flow_files import flow_schemas, flow_spec
from laurelin.transforms.flow_governance import (
    check_flow_governance,
    check_flow_sources,
    referenced_columns,
    restrict_output_to_author,
)
from laurelin.transforms.flow_ir import FlowDef, FlowRefused

ALICE = User(id="1", username="alice", role=Role.editor)
BOB = User(id="2", username="bob", role=Role.editor)


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="flowgov")
    store = MetadataStore(ws.metadata_path)
    cat = DatasetCatalog(ws, store)
    cat.write("secret_ds", pa.table({
        "region": ["us", "apac"], "amount": [1, 2], "note": ["a", "b"],
    }))
    cat.write("open_ds", pa.table({"region": ["us", "eu"], "amount": [3, 4]}))
    for user in (ALICE, BOB):
        store.create_user(user, "x")
    return ws


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def catalog(ws, store):
    return DatasetCatalog(ws, store)


@pytest.fixture()
def perms(store):
    return PermissionService(store)


def simple_flow(dataset="secret_ds", author="alice", name="copy", columns=None):
    nodes = [{"id": "n0", "kind": "source", "inputs": [],
              "params": {"dataset": dataset}}]
    terminal = "n0"
    if columns:
        nodes.append({"id": "n1", "kind": "select", "inputs": ["n0"],
                      "params": {"mode": "keep", "columns": columns}})
        terminal = "n1"
    return FlowDef.from_json(
        {"output": name, "author": author, "terminal": terminal, "nodes": nodes},
        name=name,
    )


def grant_to(store, dataset, username):
    store.set_grants_for_dataset(dataset, [{
        "subject_kind": "user", "subject": username,
        "can_view": True, "can_edit": True,
    }])


# ---------------------------------------------------------------------------
# Read rights on inputs
# ---------------------------------------------------------------------------


def test_a_flow_cannot_read_a_dataset_its_author_cannot_view(store, perms):
    """The measured Python-path hole, closed for flows.

    Without this check an editor who cannot view `secret_ds` builds a copy of
    it and reads every row. This feature's whole purpose is to make every
    analyst an editor, so inheriting that would hand the hole to the entire
    business.
    """
    grant_to(store, "secret_ds", "alice")
    assert not perms.can_view_dataset(BOB, "secret_ds")

    flow = simple_flow(author="bob")
    with pytest.raises(FlowRefused) as exc:
        check_flow_sources(store, perms, "bob", flow)
    assert "secret_ds" in str(exc.value)
    assert "bob" in str(exc.value)

    # …and alice, who may read it, is not obstructed.
    check_flow_sources(store, perms, "alice", simple_flow(author="alice"))


def test_a_flow_whose_author_no_longer_exists_stops_building_rather_than_falling_back(
    store, perms
):
    """Fail closed, with the remedy in the message.

    A flow builds *as its author*. If that user is deleted, guessing — running
    as nobody, or as the system — is how a governance hole gets built. The
    refusal names the reassignment.
    """
    flow = simple_flow(author="ghost")
    with pytest.raises(FlowRefused) as exc:
        check_flow_sources(store, perms, "ghost", flow)
    assert "ghost" in str(exc.value)
    assert "administrator" in str(exc.value)


def test_a_flow_with_no_recorded_author_is_refused(store, perms):
    flow = simple_flow(author="")
    with pytest.raises(FlowRefused):
        check_flow_sources(store, perms, "", flow)


def test_a_flow_reading_a_dataset_that_does_not_exist_is_refused(store, perms):
    flow = simple_flow(dataset="nope", author="alice")
    with pytest.raises(FlowRefused) as exc:
        check_flow_sources(store, perms, "alice", flow)
    assert "nope" in str(exc.value)


# ---------------------------------------------------------------------------
# Row policies
# ---------------------------------------------------------------------------


def test_a_flow_over_a_row_policied_input_is_refused(store, perms):
    """Refused whatever the flow projects.

    A transform's output is a new dataset with no policy of its own, so *any*
    read of a row-policied input launders its rows. There is no policy algebra
    that survives an aggregate — "rows where region='us'" has no meaning once
    the rows have been summed — and inventing one silently would be worse than
    refusing.
    """
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "alice", "values": ["us"]},
        ]},
        "column_masks": [],
    })
    flow = simple_flow(author="alice")
    with pytest.raises(FlowRefused) as exc:
        check_flow_governance(store, perms, "alice", flow, output_columns=["region"])
    assert "row policy" in str(exc.value)
    assert "secret_ds" in str(exc.value)


# ---------------------------------------------------------------------------
# Column masks
# ---------------------------------------------------------------------------


def test_a_flow_that_reads_a_masked_column_is_refused_and_the_message_names_it(
    store, perms
):
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds", "row_policy": None,
        "column_masks": [{"column": "amount", "mode": "redact", "exempt": []}],
    })
    flow = simple_flow(author="alice", columns=["amount"])
    with pytest.raises(FlowRefused) as exc:
        check_flow_governance(
            store, perms, "alice", flow, output_columns=["amount"]
        )
    assert "amount" in str(exc.value)


def test_a_mask_on_a_column_the_flow_never_touches_does_not_block_it(store, perms):
    """Precise rather than blunt.

    A masked column the flow neither reads nor emits cannot have been
    laundered, and refusing on its mere existence would make any dataset with
    one mask unusable in the builder.
    """
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds", "row_policy": None,
        "column_masks": [{"column": "note", "mode": "redact", "exempt": []}],
    })
    flow = simple_flow(author="alice", columns=["region"])
    check_flow_governance(store, perms, "alice", flow, output_columns=["region"])


def test_a_masked_column_that_merely_passes_through_to_the_output_is_still_refused(
    store, perms
):
    """`SELECT *` is the default, so "never mentioned" is not "never read".

    A source node emits every column. A flow that mentions `note` nowhere but
    ends without narrowing still lands `note` in its output — unmasked, in a
    new dataset with no policy. The output-schema half of the check is what
    catches that, which is why the callers that can supply it must.
    """
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds", "row_policy": None,
        "column_masks": [{"column": "note", "mode": "redact", "exempt": []}],
    })
    flow = simple_flow(author="alice")  # no select: everything passes through
    assert "note" not in referenced_columns(flow)
    with pytest.raises(FlowRefused) as exc:
        check_flow_governance(
            store, perms, "alice", flow,
            output_columns=["region", "amount", "note"],
        )
    assert "note" in str(exc.value)


def test_an_exempt_author_still_cannot_launder_a_masked_column(store, perms):
    """The mask refusal ignores exemptions, deliberately.

    An author exempt from a mask may *read* the column — but the flow's output
    is a new dataset with no mask on it, readable by everyone who can read the
    output. The question is not "may this author see it" but "may this column
    leave the policy behind", and the answer is no either way.
    """
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds", "row_policy": None,
        "column_masks": [{
            "column": "amount", "mode": "redact",
            "exempt": [{"subject_kind": "user", "subject": "alice"}],
        }],
    })
    flow = simple_flow(author="alice", columns=["amount"])
    with pytest.raises(FlowRefused):
        check_flow_governance(
            store, perms, "alice", flow, output_columns=["amount"]
        )


# ---------------------------------------------------------------------------
# Output grants
# ---------------------------------------------------------------------------


def test_a_flow_over_a_restricted_input_produces_an_output_restricted_to_its_author(
    store,
):
    grant_to(store, "secret_ds", "alice")
    flow = simple_flow(author="alice")
    assert restrict_output_to_author(store, flow, "alice") is True
    grants = store.grants_for_dataset("copy")
    assert [(g["subject"], g["can_view"]) for g in grants] == [("alice", True)]


def test_the_output_grant_is_the_author_alone_and_never_the_union_of_the_inputs(
    store,
):
    """With inputs A(alice) and B(bob), the union would be {alice, bob}.

    Alice would then gain access to B's data through a dataset she had no
    rights to. "Derived from something restricted ⇒ restricted to whoever
    derived it" is the only rule here that fails closed and can be explained in
    one sentence.
    """
    grant_to(store, "secret_ds", "alice")
    grant_to(store, "open_ds", "bob")
    flow = FlowDef.from_json({
        "output": "joined", "author": "alice", "terminal": "j",
        "nodes": [
            {"id": "l", "kind": "source", "inputs": [],
             "params": {"dataset": "secret_ds"}},
            {"id": "r", "kind": "source", "inputs": [],
             "params": {"dataset": "open_ds"}},
            {"id": "j", "kind": "join", "inputs": ["l", "r"], "params": {
                "how": "inner",
                "keys": [{"left": "region", "right": "region"}]}},
        ],
    }, name="joined")
    restrict_output_to_author(store, flow, "alice")
    subjects = {g["subject"] for g in store.grants_for_dataset("joined")}
    assert subjects == {"alice"}
    assert "bob" not in subjects


def test_a_flow_over_unrestricted_inputs_leaves_its_output_unrestricted(store):
    """Parity with today. Restricting every derived dataset would be a
    behaviour change for workspaces that use no dataset grants at all."""
    flow = simple_flow(dataset="open_ds", author="alice")
    assert restrict_output_to_author(store, flow, "alice") is False
    assert store.grants_for_dataset("copy") == []


def test_an_administrators_widened_grant_survives_a_rebuild(store):
    grant_to(store, "secret_ds", "alice")
    flow = simple_flow(author="alice")
    restrict_output_to_author(store, flow, "alice")
    # An admin later widens it.
    store.set_grants_for_dataset("copy", [
        {"subject_kind": "role", "subject": "editor",
         "can_view": True, "can_edit": False},
    ])
    assert restrict_output_to_author(store, flow, "alice") is False
    assert {g["subject"] for g in store.grants_for_dataset("copy")} == {"editor"}


# ---------------------------------------------------------------------------
# End to end, through the real Builder
# ---------------------------------------------------------------------------


def _build_flow(ws, store, catalog, flow):
    registry = TransformRegistry()
    registry.register(flow_spec(flow))
    return Builder(ws, catalog, store, registry).build([flow.output])


def test_building_a_flow_its_author_may_not_read_fails_the_task_and_writes_no_output(
    ws, store, catalog, perms
):
    """Build-time enforcement, not just authoring-time.

    A scheduled build has no request user, and a grant can be added *after* a
    flow was saved — so the check that matters is this one.
    """
    from laurelin.core.models import BuildStatus

    grant_to(store, "secret_ds", "alice")
    flow = simple_flow(author="bob")
    build = _build_flow(ws, store, catalog, flow)

    assert build.status == BuildStatus.failed
    with pytest.raises(KeyError):
        catalog.read("copy")


def test_a_flow_its_author_may_read_builds_and_lands_the_rows(
    ws, store, catalog
):
    from laurelin.core.models import BuildStatus

    flow = simple_flow(dataset="open_ds", author="alice")
    build = _build_flow(ws, store, catalog, flow)
    assert build.status == BuildStatus.succeeded
    assert catalog.read("copy").num_rows == 2


def test_a_flow_records_the_same_lineage_a_python_transform_would(
    ws, store, catalog
):
    """Lineage comes from the IR's source nodes, structurally.

    Never from a regex over generated SQL — that inference is how a workspace
    acquires markings nobody can explain.
    """
    flow = simple_flow(dataset="open_ds", author="alice")
    _build_flow(ws, store, catalog, flow)
    edges = store.list_lineage()
    ours = [e for e in edges if e.transform_name == "copy"]
    assert [(e.upstream_dataset, e.downstream_dataset) for e in ours] == [
        ("open_ds", "copy")
    ]


def test_a_flow_and_the_equivalent_sql_transform_produce_the_same_rows(
    ws, store, catalog
):
    """The flow compiles onto the same executor, so it must agree with it."""
    flow = FlowDef.from_json({
        "output": "flow_out", "author": "alice", "terminal": "n2",
        "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "open_ds"}},
            {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
                "predicate": {"t": "op", "op": "gte", "args": [
                    {"t": "col", "name": "amount"},
                    {"t": "lit", "type": "bigint", "value": 4}]}}},
            {"id": "n2", "kind": "sort", "inputs": ["n1"], "params": {
                "by": [{"column": "region", "dir": "asc", "nulls": "last"}]}},
        ],
    }, name="flow_out")

    registry = TransformRegistry()
    registry.register(flow_spec(flow))
    registry.register(TransformSpec(
        name="sql_out", output=Output("sql_out"),
        inputs={"open_ds": Input("open_ds")}, kind="sql",
        query='SELECT * FROM open_ds WHERE amount >= 4 ORDER BY region ASC',
    ))
    Builder(ws, catalog, store, registry).build(["flow_out", "sql_out"])

    assert catalog.read("flow_out").to_pylist() == catalog.read("sql_out").to_pylist()


def test_the_preview_and_the_build_execute_the_same_sql_text(ws, store, catalog):
    """Alias == dataset name is what makes this true.

    `Builder._register_inputs` registers each input under its alias and
    `catalog.query` registers each dataset under its name; the compiler emits
    the dataset name as the table name, so the two coincide and one statement
    serves both.
    """
    flow = simple_flow(dataset="open_ds", author="alice")
    schemas = flow_schemas(catalog, flow)
    build_sql = compile_flow(flow, schemas).sql
    preview_sql = compile_flow(flow, schemas, upto=flow.terminal).sql
    assert build_sql == preview_sql
    assert '"open_ds"' in build_sql


# ---------------------------------------------------------------------------
# Inherited gaps: stated, not fixed
# ---------------------------------------------------------------------------


def test_the_python_transform_path_still_launders_acls_row_policies_and_column_masks(
    ws, store, catalog
):
    """A KNOWN, DOCUMENTED GAP — asserted so it cannot regress unnoticed.

    Measured on this tree: an editor who cannot view `secret_ds` writes
    `@sql_transform(query="SELECT * FROM s")`, builds it, and the copy is
    world-readable with the row policy and the masks gone.

    Flows do not do this (see the tests above). The Python path is out of scope
    for this change — narrowing it is a behaviour change for every existing
    pipeline — but the inconsistency is *stated* here rather than left to be
    discovered by someone who assumed the two paths were equivalent.

    If a future change closes this, delete the test and say so in the
    CHANGELOG; do not weaken it.
    """
    from laurelin.core.models import BuildStatus

    grant_to(store, "secret_ds", "alice")
    store.set_dataset_policy("secret_ds", {
        "dataset": "secret_ds",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "alice", "values": ["us"]},
        ]},
        "column_masks": [{"column": "amount", "mode": "redact", "exempt": []}],
    })
    perms = PermissionService(store)
    assert not perms.can_view_dataset(BOB, "secret_ds")

    registry = TransformRegistry()
    registry.register(TransformSpec(
        name="leak", output=Output("leak"),
        inputs={"s": Input("secret_ds")}, kind="sql",
        query="SELECT * FROM s",
    ))
    build = Builder(ws, catalog, store, registry).build(["leak"])
    assert build.status == BuildStatus.succeeded

    # All of these are the gap, not the intent:
    assert perms.can_view_dataset(BOB, "leak")          # ACL gone
    assert store.grants_for_dataset("leak") == []       # no grants inherited
    assert store.get_dataset_policy("leak") is None     # policy gone
    rows = catalog.read("leak").to_pylist()
    assert {r["region"] for r in rows} == {"us", "apac"}  # row policy gone
    assert {r["amount"] for r in rows} == {1, 2}          # mask gone


def test_deleting_a_flow_leaves_its_lineage_edges_and_therefore_its_marking_propagation(
    ws, store, catalog, tmp_path
):
    """Retaining lineage on delete is fail-closed and deliberate.

    Nothing in this tree deletes lineage except
    `replace_lineage_for_transform`. Deleting it here would *declassify* every
    downstream dataset that inherited a classification marking through this
    flow — a strictly worse outcome than a stale edge.
    """
    from laurelin.transforms.flow_files import FlowFiles

    files = FlowFiles(ws.pipelines_dir)
    flow = simple_flow(dataset="open_ds", author="alice")
    files.write("copy", flow.as_json(), "alice")
    _build_flow(ws, store, catalog, flow)
    assert [e for e in store.list_lineage() if e.transform_name == "copy"]

    files.delete("copy")
    assert [e for e in store.list_lineage() if e.transform_name == "copy"]


def test_lineage_is_not_updated_when_a_build_task_fails_so_it_still_describes_the_previous_inputs(
    ws, store, catalog
):
    """Lineage is written only on success, inside the try, after the write.

    Edit a flow's sources, build, fail → lineage and markings still describe
    the *old* sources. A save-then-build gesture hits this far more often than
    a Python author does. Out of scope to fix; pinned so it is not a surprise.
    """
    from laurelin.core.models import BuildStatus

    flow = simple_flow(dataset="open_ds", author="alice")
    _build_flow(ws, store, catalog, flow)
    before = [(e.upstream_dataset, e.downstream_dataset)
              for e in store.list_lineage() if e.transform_name == "copy"]

    # Repoint at a dataset the author cannot read: the build fails…
    grant_to(store, "secret_ds", "bob")
    moved = simple_flow(dataset="secret_ds", author="alice")
    build = _build_flow(ws, store, catalog, moved)
    assert build.status == BuildStatus.failed

    # …and the lineage still claims the old upstream.
    after = [(e.upstream_dataset, e.downstream_dataset)
             for e in store.list_lineage() if e.transform_name == "copy"]
    assert after == before


# ---------------------------------------------------------------------------
# Expectations on a flow
# ---------------------------------------------------------------------------


def _flow_with_expectations(expectations, dataset="open_ds", name="checked"):
    return FlowDef.from_json({
        "output": name, "author": "alice", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": dataset}}],
        "expectations": expectations,
    }, name=name)


def test_a_flow_expectation_stops_the_version_being_published(ws, store, catalog):
    """Same guarantee as a Python transform's: checked before the commit.

    The version is published by inserting its manifest row, so a failing check
    means the bad data was never visible to anyone — no reader saw it, no
    downstream build consumed it.
    """
    from laurelin.core.models import BuildStatus

    flow = _flow_with_expectations([
        {"kind": "row_count_between", "min": 99},
    ])
    build = _build_flow(ws, store, catalog, flow)
    assert build.status == BuildStatus.failed
    with pytest.raises(KeyError):
        catalog.read("checked")


def test_a_flow_expectation_that_holds_lets_the_build_through(ws, store, catalog):
    """Also the test for the lazy resolution.

    A flow's expectations are checked against its *output* schema, which is only
    known once the flow has been compiled — so they are resolved inside the
    validator callback rather than at collection time. That works because
    DuckDB's result is an argument to `catalog.write`, and Python evaluates it
    before `write` invokes the validator. If that ordering ever changes, this
    test fails with a KeyError on the schema cache rather than silently
    skipping the checks.
    """
    from laurelin.core.models import BuildStatus

    flow = _flow_with_expectations([
        {"kind": "row_count_between", "min": 1, "max": 10},
        {"kind": "not_null", "column": "region"},
        {"kind": "unique", "column": "region"},
        {"kind": "accepted_values", "column": "region", "values": ["us", "eu"]},
    ])
    build = _build_flow(ws, store, catalog, flow)
    assert build.status == BuildStatus.succeeded, build
    assert catalog.read("checked").num_rows == 2

    task = store.get_build(build.id).tasks[0]
    checks = {c["expectation"]: c["passed"] for c in task.expectations}
    assert all(checks.values()), checks
    assert len(checks) == 4


def test_a_flow_expectation_naming_a_column_the_result_does_not_have_is_refused(
    ws, store, catalog
):
    from laurelin.core.models import BuildStatus

    flow = _flow_with_expectations([{"kind": "not_null", "column": "nope"}])
    build = _build_flow(ws, store, catalog, flow)
    assert build.status == BuildStatus.failed


def test_a_flow_can_never_reach_the_raw_predicate_expectation(ws, store, catalog):
    """`expectations.expression()` interpolates author text into
    `WHERE NOT (...)`. It stays for the Python path and is unreachable from a
    flow at any level — the IR's expectation vocabulary is closed."""
    with pytest.raises(FlowRefused):
        _flow_with_expectations([
            {"kind": "expression", "column": "region",
             "predicate": "1=1) OR (1=1"},
        ])


# ---------------------------------------------------------------------------
# Regressions. Each of these was reproduced end to end through the HTTP API
# before it was fixed; the docstrings carry the measurement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["ssn", "SSN", "ssn ", "ｓsn"])
def test_a_mask_is_matched_the_same_way_the_policy_resolver_matches_it(
    ws, store, catalog, perms, spelling
):
    """One fold for "the same name, mistyped", shared with `permissions.py`.

    `_reject_case_mismatch` exists there precisely so a mask authored as `SSN`
    against a column `ssn` fails CLOSED rather than serving plaintext — every
    policied read of such a dataset raises `PolicyRenderError`. This check
    compared with a plain `in`, so it disagreed, and the disagreement ran the
    wrong way. Measured, for each near-miss spelling below: nobody but an admin
    could read `masked_ds` at all, and a flow of one `source` node copied it out
    verbatim into a dataset with no mask and no grants.
    """
    catalog.write("masked_ds", pa.table({
        "person": ["a", "b"], "ssn": ["111-22-3333", "444-55-6666"],
    }))
    store.set_dataset_policy("masked_ds", {
        "column_masks": [{"column": spelling, "mode": "redact"}],
    })
    flow = simple_flow(dataset="masked_ds", author="alice", name="leak")
    with pytest.raises(FlowRefused) as exc:
        check_flow_governance(store, perms, "alice", flow,
                              output_columns=["person", "ssn"])
    assert "masked_ds" in str(exc.value)


def test_a_mask_naming_a_column_the_dataset_does_not_have_still_does_not_deny_it(
    ws, store, catalog, perms
):
    """The other direction, and it must keep working: a mask left behind by
    schema evolution names a genuinely dropped column, and denying the whole
    dataset for it would turn a rename into a denial of service."""
    catalog.write("evolved", pa.table({"person": ["a"], "amount": [1]}))
    store.set_dataset_policy("evolved", {
        "column_masks": [{"column": "long_gone", "mode": "redact"}],
    })
    check_flow_governance(
        store, perms, "alice", simple_flow(dataset="evolved", author="alice"),
        output_columns=["person", "amount"],
    )


def test_a_flow_cannot_overwrite_a_dataset_its_author_cannot_change(store, perms):
    """A flow's name *is* its output dataset's name, so claiming somebody
    else's dataset is one text field.

    Measured: `secret_ds` granted to root alone; an ordinary editor saved a flow
    named `secret_ds` reading a dataset she could read, built it, and destroyed
    the contents of a dataset she still returns 403 on.
    """
    grant_to(store, "secret_ds", "root")
    flow = FlowDef.from_json(
        {"output": "secret_ds", "author": "alice", "terminal": "n0",
         "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                    "params": {"dataset": "open_ds"}}]},
        name="secret_ds",
    )
    with pytest.raises(FlowRefused) as exc:
        check_flow_governance(store, perms, "alice", flow)
    assert "secret_ds" in str(exc.value) and "alice" in str(exc.value)


def test_a_flow_may_rebuild_the_output_it_already_owns(store, perms, catalog):
    """The check above must not lock a flow out of its own dataset on the
    second build — which is exactly what happens when the first build restricts
    the output to its author."""
    grant_to(store, "secret_ds", "alice")
    flow = simple_flow(author="alice")
    catalog.write("copy", pa.table({"region": ["us"]}))
    assert restrict_output_to_author(store, flow, "alice") is True
    check_flow_governance(store, perms, "alice", flow)


def test_marking_laundering_by_repointing_a_flows_source_is_refused(
    ws, store, catalog, perms
):
    """The measured attack that the output check also closes.

    A flow `mid` reads a `secret`-marked dataset, so `mid` — and everything
    downstream — inherits the marking. An uncleared editor cannot name the
    marked dataset as a source (`check_flow_sources` refuses him), but he could
    edit `mid` to read a public dataset instead: the build replaced the lineage
    edge, `recompute_all_markings` found no marked upstream, and every
    downstream dataset was declassified *while still holding the classified
    rows*. Bob was 403 on `report` before and 200 after.

    Requiring view rights on the flow's own output closes it, because a dataset
    carrying a marking he has no clearance for is one he cannot view.
    """
    store.create_marking("secret")
    catalog.write("mid", pa.table({"region": ["us"], "amount": [1]}))
    store.set_explicit_markings("mid", ["secret"])
    store.recompute_all_markings()
    assert not perms.can_view_dataset(BOB, "mid")

    repointed = FlowDef.from_json(
        {"output": "mid", "author": "bob", "terminal": "n0",
         "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                    "params": {"dataset": "open_ds"}}]},
        name="mid",
    )
    with pytest.raises(FlowRefused) as exc:
        check_flow_governance(store, perms, "bob", repointed)
    assert "mid" in str(exc.value)


def test_pointing_a_flow_at_a_dataset_shared_more_widely_than_its_source_is_refused(
    store, perms, catalog
):
    """`restrict_output_to_author`'s early-out could not cover this, and must
    not try — leaving an administrator's widening alone is deliberate.

    Measured: `secret_ds` granted to alice only; `shared_report` an ordinary
    team dataset granted to alice and bob. Alice saved a flow named
    `shared_report` reading `secret_ds`. The early-out fired, the API answered
    `output_will_be_restricted: false` so nothing warned, and bob — 403 on
    `secret_ds` — read every row of it out of `shared_report`.
    """
    catalog.write("shared_report", pa.table({"x": [0]}))
    grant_to(store, "secret_ds", "alice")
    store.set_grants_for_dataset("shared_report", [
        {"subject_kind": "user", "subject": "alice",
         "can_view": True, "can_edit": True},
        {"subject_kind": "user", "subject": "bob",
         "can_view": True, "can_edit": True},
    ])
    flow = simple_flow(author="alice", name="shared_report")
    # Allowed by the build path: an admin may widen a flow's own output.
    check_flow_governance(store, perms, "alice", flow)
    # Refused when the author is *choosing* that output.
    with pytest.raises(FlowRefused) as exc:
        check_flow_governance(store, perms, "alice", flow, authoring=True)
    assert "bob" in str(exc.value) and "secret_ds" in str(exc.value)


def test_an_administrators_widened_grant_still_survives_a_rebuild(store, perms):
    """The case the early-out was written for, kept working: an admin who
    deliberately widens a flow's own output is not overruled by the next
    build."""
    grant_to(store, "secret_ds", "alice")
    flow = simple_flow(author="alice")
    store.set_grants_for_dataset("copy", [
        {"subject_kind": "role", "subject": "editor",
         "can_view": True, "can_edit": False},
    ])
    assert restrict_output_to_author(store, flow, "alice") is False
