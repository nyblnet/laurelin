"""Data expectations.

The property that matters is *when* they run. A version is published by
inserting its manifest row, so expectations are checked against the written
Parquet parts before that insert — meaning a failing output is never visible
to anyone, rather than being visible and then retracted.

So the tests to care about are: the bad version does not exist, the parts do
not linger, and the last good version is still what readers see.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.transforms import (
    Builder,
    Input,
    Output,
    TransformRegistry,
    accepted_values,
    expect,
    expression,
    not_null,
    row_count,
    transform,
    unique,
    use_registry,
)


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="exp")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("src", pa.table({
        "id": pa.array([1, 2, 3, 4], type=pa.int64()),
        "status": ["open", "shipped", "open", "returned"],
    }))
    return ws, store, catalog


def build(env, registry) -> tuple:
    ws, store, catalog = env
    builder = Builder(ws, catalog, store, registry)
    build_id = store.create_build(targets=[]).id
    return builder.execute(build_id, None, None), store.get_build(build_id)


def registry_with(fn_builder) -> TransformRegistry:
    reg = TransformRegistry()
    with use_registry(reg):
        fn_builder()
    return reg


# -- passing -----------------------------------------------------------------

def test_a_satisfied_expectation_publishes_normally(env):
    def declare():
        @expect(not_null("id"), unique("id"), row_count(min=1))
        @transform(output=Output("out"), src=Input("src"))
        def good(src):
            return src

    _, info = build(env, registry_with(declare))
    task = info.tasks[0]
    assert task.status.value == "succeeded"
    assert env[2].read("out").num_rows == 4
    assert [r["passed"] for r in task.expectations] == [True, True, True]
    assert [r["expectation"] for r in task.expectations] == [
        "not_null(id)", "unique(id)", "row_count(min=1, max=None)",
    ]


# -- failing -----------------------------------------------------------------

@pytest.mark.parametrize("expectation, produce, expect_measured", [
    (not_null("id"), lambda: pa.table({"id": pa.array([1, None, 3], type=pa.int64())}), 1),
    (unique("id"), lambda: pa.table({"id": pa.array([1, 1, 1], type=pa.int64())}), 2),
    (row_count(min=10), lambda: pa.table({"id": pa.array([1], type=pa.int64())}), 1),
    (row_count(max=2), lambda: pa.table({"id": pa.array([1, 2, 3], type=pa.int64())}), 3),
    (accepted_values("id", [1, 2]),
     lambda: pa.table({"id": pa.array([1, 2, 9], type=pa.int64())}), 1),
    (expression("positive", "id > 0"),
     lambda: pa.table({"id": pa.array([1, -1, -2], type=pa.int64())}), 2),
])
def test_a_violated_expectation_fails_the_build(env, expectation, produce, expect_measured):
    ws, store, catalog = env

    def declare():
        @expect(expectation)
        @transform(output=Output("out"), src=Input("src"))
        def bad(src):
            return produce()

    _, info = build(env, registry_with(declare))
    task = info.tasks[0]
    assert task.status.value == "failed"
    # R1: an expectation's `message` is prose an EDITOR wrote in a pipeline
    # file, so it is not stored on the failure. What is stored is the code and
    # a count; the per-check results (with their messages) stay on
    # `task.expectations`, which is OPERATIONAL and editor-only.
    assert task.failure is not None
    assert task.failure.code.value == "expectation_failed"
    assert task.failure.counters["failed_expectations"] >= 1

    failed = [r for r in task.expectations if not r["passed"]]
    assert len(failed) == 1
    assert failed[0]["measured"] == expect_measured

    # The point of the whole design: the bad version was never published.
    assert store.get_version("out", None) is None
    with pytest.raises(KeyError):
        catalog.read("out")


def test_a_failure_leaves_the_previous_version_readable(env):
    """A bad build must not damage what was already there."""
    ws, store, catalog = env
    catalog.write("out", pa.table({"id": pa.array([7, 8], type=pa.int64())}))

    def declare():
        @expect(row_count(min=100))
        @transform(output=Output("out"), src=Input("src"))
        def bad(src):
            return pa.table({"id": pa.array([1], type=pa.int64())})

    _, info = build(env, registry_with(declare))
    assert info.tasks[0].status.value == "failed"
    assert store.get_version("out", None).version == 1
    assert catalog.read("out").column("id").to_pylist() == [7, 8]


def test_a_failed_build_leaves_no_orphaned_parts(env):
    """The parts were written before the check ran, so they have to be cleaned
    up — otherwise every failing build leaks a file."""
    ws, store, catalog = env
    before = set(catalog.storage.list_keys("out"))

    def declare():
        @expect(not_null("id"))
        @transform(output=Output("out"), src=Input("src"))
        def bad(src):
            return pa.table({"id": pa.array([None], type=pa.int64())})

    build(env, registry_with(declare))
    assert set(catalog.storage.list_keys("out")) == before


# -- severity ----------------------------------------------------------------

def test_a_warning_records_but_publishes(env):
    """Not every rule should stop a pipeline. A `warn` is recorded exactly like
    an error and differs only in whether it halts the build."""
    def declare():
        @expect(row_count(min=100, severity="warn"), not_null("id"))
        @transform(output=Output("out"), src=Input("src"))
        def noisy(src):
            return src

    _, info = build(env, registry_with(declare))
    task = info.tasks[0]
    assert task.status.value == "succeeded"
    assert env[2].read("out").num_rows == 4

    warned = [r for r in task.expectations if not r["passed"]]
    assert len(warned) == 1
    assert warned[0]["severity"] == "warn"
    assert "at least 100" in warned[0]["message"]


# -- streaming and incremental ------------------------------------------------

def test_expectations_apply_to_a_streaming_transform(env):
    """The check is SQL over the written parts, so it works without
    materializing what streaming just avoided materializing."""
    def declare():
        @expect(not_null("id"))
        @transform(output=Output("out"), streaming=True, src=Input("src"))
        def streamed(src):
            for batch in src:
                yield pa.table({"id": pa.array([None] * batch.num_rows,
                                               type=pa.int64())})

    _, info = build(env, registry_with(declare))
    assert info.tasks[0].status.value == "failed"
    assert env[1].get_version("out", None) is None


def test_expectations_apply_to_an_incremental_append(env):
    """An incremental build appends, so a bad delta must not be appended to a
    good dataset."""
    ws, store, catalog = env

    def declare():
        @expect(not_null("id"))
        @transform(output=Output("out"), incremental=True, src=Input("src"))
        def incr(src):
            return src.select(["id"])

    reg = registry_with(declare)
    _, info = build(env, reg)
    assert info.tasks[0].status.value == "succeeded"
    assert catalog.read("out").num_rows == 4

    # A second run whose delta violates the expectation.
    catalog.append("src", pa.table({
        "id": pa.array([None], type=pa.int64()), "status": ["open"],
    }))
    _, info2 = build(env, reg)
    assert info2.tasks[0].status.value == "failed"
    assert catalog.read("out").num_rows == 4, "the good rows must survive"


# -- declaration errors -------------------------------------------------------

def test_expect_below_transform_is_rejected(env):
    """Decorator order matters, and getting it wrong would silently skip every
    check — so it fails at import time with a message that says which way up."""
    with pytest.raises(ValueError, match="above @transform"):
        with use_registry(TransformRegistry()):
            @transform(output=Output("out"), src=Input("src"))
            @expect(not_null("id"))
            def wrong(src):
                return src


def test_row_count_needs_a_bound():
    with pytest.raises(ValueError, match="min= or max="):
        row_count()


def test_accepted_values_needs_values():
    with pytest.raises(ValueError, match="at least one"):
        accepted_values("status", [])


def test_expectations_survive_a_column_named_with_a_quote(env):
    """Column names reach SQL as identifiers; a quote in one must not end the
    identifier."""
    def declare():
        @expect(not_null('we"ird'))
        @transform(output=Output("out"), src=Input("src"))
        def quoted(src):
            return pa.table({'we"ird': pa.array([None], type=pa.int64())})

    _, info = build(env, registry_with(declare))
    assert info.tasks[0].status.value == "failed"
    assert [r["passed"] for r in info.tasks[0].expectations] == [False]


def test_accepted_values_binds_its_values_instead_of_escaping_them():
    """REVERT `accepted_values` to the quote-doubling IN-list and the value
    appears in `exp.sql` instead of `exp.params`.

    This was a second SQL-generation surface with its own escaper —
    `"'" + str(v).replace("'", "''") + "'"` — evaluated on the build
    connection. A flow author reaches this function through the builder's
    `accepted_values` check, which is why it stopped being allowed to
    interpolate.
    """
    import duckdb
    import pyarrow as pa

    from laurelin.transforms.expectations import accepted_values, check

    hostile = "'); DROP TABLE t; --"
    exp = accepted_values("status", ["ok", hostile])

    assert hostile not in exp.sql
    assert exp.sql.count("?") == 2
    assert list(exp.params) == ["ok", hostile]

    # …and it still evaluates correctly, hostile value included.
    con = duckdb.connect()
    con.register("t", pa.table({"status": ["ok", hostile, "bad"]}))
    results = check(con, [exp], "out")
    con.close()
    assert results[0]["measured"] == 1  # only "bad" violates


# -- what the BUILD says the cause was ---------------------------------------

def test_a_build_reports_the_cause_its_only_failed_task_recorded(env):
    """The build-level `Failure.code` was hard-coded `TRANSFORM_FAILED`.

    Measured on the Builds page: an expanded failed row stacked two
    contradictory diagnoses. The build-level one, first and louder — "The
    pipeline's code raised — fix the code it names" — sat directly above the
    task-level one, "A data expectation failed. The transform ran and its
    output did not meet a declared expectation." A novice reads the first and
    goes hunting for a bug in SQL that is correct: the DATA was wrong, not the
    code. Health had it right all along, which is how the contradiction was
    visible at all.

    The rule, and it is deliberately minimal: one code when the failed tasks
    agree, the generic code when they do not. No new `MIXED` enum member — a
    word no task ever recorded has no business on the row.
    """
    def declare():
        @expect(accepted_values("status", ["open", "shipped"]))
        @transform(output=Output("out"), src=Input("src"))
        def picky(src):
            return src

    _, info = build(env, registry_with(declare))
    assert info.status.value == "failed"
    assert [t.failure.code.value for t in info.tasks] == ["expectation_failed"]
    assert info.failure is not None
    assert info.failure.code.value == "expectation_failed", (
        "the build must report the cause its only failed task recorded"
    )
    # The subject stays the build; only the CODE is inherited.
    assert info.failure.subject == f"build:{info.id}"
    assert info.failure.counters == {"failed_tasks": 1}


def test_a_build_whose_failed_tasks_disagree_keeps_the_generic_cause(env):
    """Two tasks fail for genuinely different reasons, so there is no single
    true cause and the generic code is the honest answer."""
    def declare():
        @expect(accepted_values("status", ["open", "shipped"]))
        @transform(output=Output("out_a"), src=Input("src"))
        def picky(src):
            return src

        @transform(output=Output("out_b"), src=Input("src"))
        def raiser(src):
            raise RuntimeError("boom")

    _, info = build(env, registry_with(declare))
    assert info.status.value == "failed"
    codes = {t.failure.code.value for t in info.tasks}
    assert len(codes) == 2, codes
    assert info.failure.code.value == "transform_failed"
