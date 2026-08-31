"""Guards against a green run that didn't actually test anything.

A skipped test is invisible in a passing suite. That is fine locally — not
everyone has a PostgreSQL to point at — but in CI it is how an entire dialect,
or an entire optional feature, quietly stops being covered while the badge
stays green.

These tests only assert anything when ``CI`` is set, so they cost a local run
nothing.
"""

import importlib
import os
from pathlib import Path

import pytest

# Two detectors, either one arms the guards. GitHub Actions documents both
# CI=true and GITHUB_ACTIONS=true as default environment variables for every
# step; checking both means a job that overrides or unsets the generic CI
# (which other tools also read) cannot accidentally disarm every guard here.
in_ci = pytest.mark.skipif(
    os.environ.get("CI", "").lower() not in {"1", "true"}
    and os.environ.get("GITHUB_ACTIONS", "").lower() != "true",
    reason="guards apply to CI runs; local runs may legitimately skip suites",
)


@in_ci
def test_the_postgres_suite_is_enabled():
    """Multi-replica safety rests entirely on the Postgres path.

    It is the least-exercised part of the codebase precisely because it needs
    a server, so CI must never be allowed to skip it.
    """
    url = os.environ.get("LAURELIN_TEST_POSTGRES", "")
    assert url.startswith("postgresql://"), (
        "LAURELIN_TEST_POSTGRES is unset, so tests/test_postgres.py and "
        "tests/test_horizontal.py skipped. CI must run both dialects."
    )

    import psycopg  # the extra must be installed, not just declared

    with psycopg.connect(url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1


@in_ci
def test_the_object_store_suite_is_enabled():
    """The object-store connector tests are the only executions of the S3 data
    path against a real endpoint, and they skip without LAURELIN_TEST_S3.

    They did exactly that, invisibly, in every draft of this workflow until
    this guard existed: the variable appeared nowhere in ci.yml, so all eleven
    tests proven against live MinIO would have skipped while the badge stayed
    green.

    Reachability is asserted with the same S3FileSystem call the s3 fixture
    makes, because the failure mode it catches is specific: a MinIO declared
    as a GitHub `services:` container starts without the `server /data`
    arguments, prints help, and exits 0 — the port is closed, the fixture
    turns that into eleven skips, and nothing is red. Here it is a failure.
    """
    import urllib.parse

    from pyarrow import fs as pafs

    url = os.environ.get("LAURELIN_TEST_S3", "")
    assert url.startswith("http"), (
        "LAURELIN_TEST_S3 is unset, so every object-store connector test in "
        "tests/test_connectors.py skipped. CI must exercise the S3 path "
        "against a live endpoint."
    )

    # The gated tests must still exist, and still use the bucket this guard
    # probes — same pin style as the watermark guard above.
    source = Path(__file__).with_name("test_connectors.py").read_text()
    assert "test_object_store_sync_ingests_each_format_from_bucket" in source
    assert 'BUCKET = "laurelin-ingest-test"' in source

    parts = urllib.parse.urlsplit(url)
    filesystem = pafs.S3FileSystem(
        access_key=urllib.parse.unquote(parts.username or ""),
        secret_key=urllib.parse.unquote(parts.password or ""),
        endpoint_override=f"{parts.scheme}://{parts.hostname}:{parts.port}",
        scheme=parts.scheme,
        allow_bucket_creation=True,
    )
    # Raises OSError if the endpoint is dead — which is the point. The fixture
    # wraps this same call in pytest.skip; the guard must not.
    filesystem.create_dir("laurelin-ingest-test", recursive=True)


@in_ci
def test_the_webapp_harness_is_enabled():
    """The webapp-harness tests are the only executions of UI TypeScript in
    the whole suite, and they skip as a unit when node or the webapp's
    node_modules are absent.

    That was every CI draft's permanent state until the matrix job ran
    `npm ci`: 31 tests skipping green while no job executed a single line of
    the UI. Proven the usual way — a bundle corrupted into a guaranteed
    SyntaxError sailed through the package job's served-page checks, because
    serving a page and running its script are different claims. The package
    job now executes the shipped bundle in headless Chrome; this guard keeps
    the source-level harness from silently returning to zero coverage.
    """
    import shutil

    assert shutil.which("node"), (
        "node is not on PATH, so tests/test_charts_render.py and "
        "tests/test_explore_mount.py — the entire UI execution surface — "
        "skipped."
    )
    esbuild = (
        Path(__file__).resolve().parent.parent
        / "laurelin" / "ui" / "webapp" / "node_modules" / ".bin" / "esbuild"
    )
    assert esbuild.exists(), (
        "the webapp's node_modules are missing (`npm ci` did not run), so "
        "the 31 webapp-harness tests skipped and no CI job executed any UI "
        "TypeScript."
    )
    # The harness files must still exist and still contain the tests this
    # guard exists to keep running — same pin style as the watermark guard.
    pins = {
        "test_charts_render.py":
            "test_a_null_value_in_a_line_series_renders_as_a_gap_not_a_zero",
        "test_explore_mount.py":
            "test_the_explore_screen_first_mounts_without_throwing_before_any_query_resolves",
    }
    for filename, test_name in pins.items():
        source = Path(__file__).with_name(filename).read_text()
        assert test_name in source, f"{filename} no longer contains {test_name}"


@in_ci
def test_the_watermark_visibility_test_is_not_silently_skipped():
    """``edit_seq`` exists because Postgres makes identity values visible at
    COMMIT rather than at INSERT. That claim is only checked by a test that
    needs a real Postgres, so a run without one proves nothing about it."""
    assert os.environ.get("LAURELIN_TEST_POSTGRES", "").startswith("postgresql://"), (
        "tests/test_postgres.py::test_the_edit_watermark_survives_postgres_"
        "identity_visibility is the only check on the catch-up watermark's "
        "core assumption, and it skips without LAURELIN_TEST_POSTGRES."
    )
    source = Path(__file__).with_name("test_postgres.py").read_text()
    assert "test_the_edit_watermark_survives_postgres_identity_visibility" in source


@in_ci
def test_the_concurrency_races_are_not_silently_skipped():
    """The concurrency suite exists because 'follows from database semantics'
    had already shipped false claims here. Its worst findings were
    Postgres-only (the security-list lost update never reproduces on SQLite's
    single-writer lock), so a CI run without Postgres would show green while
    proving nothing about the races that actually widened access.

    Three pins, same philosophy as the watermark guard above: the environment
    must be present, the named tests must still exist, and the soak marker
    must not have been quietly deselected in addopts (CI runs plain pytest, so
    a `-m "not soak"` there would drop the loop-heavy tests from every run
    without a single skip line)."""
    assert os.environ.get("LAURELIN_TEST_POSTGRES", "").startswith("postgresql://"), (
        "tests/test_concurrency*.py parametrize on the dialect and skip their "
        "Postgres half without LAURELIN_TEST_POSTGRES — which is the half "
        "where the lost-update races actually happen."
    )
    pins = {
        # The Postgres-only union anomaly: two grant replaces ending wider
        # than either writer wrote.
        "test_concurrency.py":
            "test_concurrent_grant_replaces_end_as_one_writers_list_not_the_union",
        # The double fire: a stale due list must not yield a second run.
        "test_concurrency_coord.py":
            "test_a_schedule_fires_once_per_window_even_with_a_stale_due_list",
        # The IdP-facing lost update, removes included.
        "test_concurrency_exec.py":
            "test_concurrent_scim_patches_never_lose_a_member_change",
    }
    for filename, test_name in pins.items():
        source = Path(__file__).with_name(filename).read_text()
        assert test_name in source, f"{filename} no longer contains {test_name}"

    import tomllib

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_bytes().decode())
    addopts = pyproject["tool"]["pytest"]["ini_options"].get("addopts", "")
    assert "not soak" not in addopts, (
        "addopts deselects the soak marker, so the multi-round concurrency "
        "tests silently vanished from every default run"
    )


@in_ci
@pytest.mark.parametrize(
    "module, extra",
    [
        ("psycopg", "postgres"),
        ("saml2", "saml"),
        ("mcp", "mcp"),
        ("adbc_driver_flightsql", "engines"),
        ("croniter", "scheduler"),
        ("prometheus_client", "metrics"),
        ("pyiceberg", "iceberg"),
        ("chdb", "clickhouse"),
        ("mysql.connector", "starrocks"),
    ],
)
def test_optional_extras_are_installed(module, extra):
    """Every optional feature degrades gracefully when its dependency is
    missing — which means its tests skip just as gracefully.

    CI installs all of them, so a missing one here means the workflow's extras
    list drifted from pyproject and a feature is going untested.
    """
    importlib.import_module(module)


@in_ci
def test_the_starrocks_suite_runs_wherever_it_is_configured():
    """The StarRocks suites skip silently without a server, which is right for
    the matrix job and wrong for the job that exists to run them.

    So the guard is conditional on the DSN being set at all: if the workflow
    starts a StarRocks and points at it, the tests must actually reach it. A
    job that starts a 3.18 GB container and then skips every test is worse than
    one that does not run.
    """
    url = os.environ.get("LAURELIN_TEST_STARROCKS", "").strip()
    if not url:
        return  # the ordinary matrix job; the dialect unit tests still ran

    from laurelin.core import starrocks

    assert url.startswith("starrocks://"), (
        f"LAURELIN_TEST_STARROCKS is {url!r}; it must be a "
        "starrocks://user:password@host:port/database DSN"
    )
    assert starrocks.available(), (
        "LAURELIN_TEST_STARROCKS is set but the client is missing, so every "
        "StarRocks test skipped. Install the 'starrocks' extra."
    )
    con = starrocks.connect({"url": url})
    try:
        assert starrocks.run("SELECT 1 AS n", con=con).column("n").to_pylist() == [1]
    finally:
        con.close()


def test_the_starrocks_job_runs_on_main_and_on_prs_that_touch_it():
    """It is not in the matrix — the image is a 1.92 GB pull — so nothing else
    would notice if it were deleted, renamed, or quietly gated back down to
    never.

    That last one is not hypothetical: the job's original gate was
    "workflow_dispatch or a PR label", which in practice meant the StarRocks
    integration suite had never run anywhere but a developer's machine. The
    gate must keep firing on push to main, AND on PRs whose diff touches
    StarRocks code — a main-only gate was measured to merge a broken prepared
    cursor green (the PR-event suite passed while the live-server selection
    failed 28 tests), leaving the red for whoever pushed next. The job must
    also keep pointing the tests at the container it pays to start.

    Runs everywhere, not just in CI: it reads the workflow file, so it can
    catch the regression before it merges.
    """
    import re

    import yaml

    root = Path(__file__).resolve().parent.parent
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"].get("starrocks")
    assert job is not None, "the StarRocks job has gone missing"

    condition = job.get("if", "")
    assert condition, (
        "the job lost its gate: a 1.92 GB pull would now run once per matrix "
        "trigger, which is the cost the dedicated job exists to avoid"
    )
    assert "github.event_name == 'push'" in condition and "refs/heads/main" in condition, (
        "the gate no longer fires on push to main, so the StarRocks "
        "integration suite runs only when someone remembers to ask — which is "
        "how it ran never, for seventeen commits"
    )
    assert "needs.changes.outputs.starrocks == 'true'" in condition, (
        "the gate no longer fires on PRs that touch StarRocks code, so a "
        "regression in the read path merges green and is first seen "
        "post-merge on main"
    )
    assert "changes" in (job.get("needs") or []), (
        "the job no longer depends on the `changes` job, so its path gate "
        "can never evaluate to true"
    )

    changes = workflow["jobs"].get("changes")
    assert changes is not None, (
        "the `changes` job has gone missing; the starrocks path gate reads "
        "its output"
    )
    changes_runs = " ".join(str(s.get("run", "")) for s in changes["steps"])
    assert "laurelin/core/starrocks" in changes_runs, (
        "the changes job no longer watches laurelin/core/starrocks.py, which "
        "is exactly the file whose regression was measured to merge green"
    )

    env = [s.get("env", {}) for s in job["steps"]]
    assert any("LAURELIN_TEST_STARROCKS" in e for e in env), (
        "the job would start a container and then skip every test"
    )

    # The job must select the single StarRocks guard, never this whole file:
    # CI=true arms every @in_ci guard, and that job has no Postgres, no MinIO
    # and only the starrocks extra — selecting the file was measured to fail
    # 12 guard tests before any StarRocks test ran.
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "test_ci_guards.py::test_the_starrocks_suite_runs_wherever_it_is_configured" in runs, (
        "the job no longer runs the guard that proves the container is reachable"
    )
    assert not re.search(r"test_ci_guards\.py(?!::)", runs), (
        "the job selects the whole guard file; with CI=true that arms the "
        "Postgres, S3 and extras guards its environment cannot satisfy"
    )


def test_no_test_file_disappears_without_editing_this_manifest():
    """Deleting an entire test file is invisible to everything else here.

    Measured: `rm tests/test_markings.py` (seven markings-governance tests)
    left the full guard file green, ruff green, and collection quietly
    dropping from 3517 to 3510 — no job, guard or lint reads test names or
    counts, so only the handful of files whose source other guards happen to
    read were protected. Every other file could vanish without a red line
    anywhere.

    So the suite's file list is pinned. Removing or renaming a test file now
    requires editing this manifest — one reviewable diff line instead of
    silence — and a new file must be added here too, because a manifest that
    only ever grows stale protects nothing. Runs everywhere, not just in CI:
    losing a test file locally deserves the same red.
    """
    expected = {
        "test_aggregations.py",
        "test_alerts.py",
        "test_analyses.py",
        "test_api.py",
        "test_approvals.py",
        "test_audience.py",
        "test_authoring.py",
        "test_auth.py",
        "test_bench.py",
        "test_build_governance.py",
        "test_build_leases.py",
        "test_catalog.py",
        "test_charts_render.py",
        "test_ci_guards.py",
        "test_clickhouse_governance.py",
        "test_clickhouse.py",
        "test_cli.py",
        "test_concurrency_coord.py",
        "test_concurrency_exec.py",
        "test_concurrency.py",
        "test_connectors.py",
        "test_dashboards.py",
        "test_dataset_acls.py",
        "test_delegated_compute.py",
        "test_dialects.py",
        "test_docs_vocabulary.py",
        "test_edit_log_prune.py",
        "test_expectations.py",
        "test_explore_api.py",
        "test_explore_mount.py",
        "test_export_governance_roundtrip.py",
        "test_export_secrets.py",
        "test_failure.py",
        "test_federation.py",
        "test_file_permissions.py",
        "test_flow_api.py",
        "test_flow_compile.py",
        "test_flow_governance.py",
        "test_flow_ir.py",
        "test_govern_sql_ui.py",
        "test_hardening.py",
        "test_health.py",
        "test_horizontal.py",
        "test_iceberg.py",
        "test_incremental.py",
        "test_incremental_transforms.py",
        "test_limits.py",
        "test_lineage_disclosure.py",
        "test_markings.py",
        "test_mcp_authoring.py",
        "test_mcp_governance.py",
        "test_mcp_presentation.py",
        "test_mcp.py",
        "test_migration_guide.py",
        "test_multiworkspace.py",
        "test_object_apps.py",
        "test_object_index.py",
        "test_object_ordering.py",
        "test_object_search.py",
        "test_object_store_governance.py",
        "test_object_store.py",
        "test_object_store_races.py",
        "test_observability.py",
        "test_oidc.py",
        "test_ontology_apps_ui.py",
        "test_ontology_authoring.py",
        "test_ontology_pushdown.py",
        "test_ontology.py",
        "test_operate_ui.py",
        "test_permissions.py",
        "test_pipelines_ux.py",
        "test_portability_surfaces.py",
        "test_postgres.py",
        "test_query.py",
        "test_redaction.py",
        "test_rls_pushdown.py",
        "test_rls.py",
        "test_saml.py",
        "test_scheduler.py",
        "test_scim.py",
        "test_starrocks_governance.py",
        "test_starrocks.py",
        "test_storage.py",
        "test_streaming_transforms.py",
        "test_text_agreement.py",
        "test_transforms.py",
        "test_tutorials.py",
        "test_ui_consistency.py",
        "test_ui_guidance.py",
        "test_ui_information_architecture.py",
        "test_ui_primitives.py",
        "test_uploads.py",
        "test_workspace_export.py",
        "test_workspace_import.py",
        "test_writeback.py",
    }
    actual = {p.name for p in Path(__file__).parent.glob("test_*.py")}

    missing = expected - actual
    assert not missing, (
        f"{sorted(missing)} are listed here but no longer exist. If the "
        "removal is intentional, delete the entry from this manifest in the "
        "same change — that is the reviewable line this guard exists to force."
    )
    unlisted = actual - expected
    assert not unlisted, (
        f"{sorted(unlisted)} are new test files not yet in this manifest. "
        "Add them, so their later disappearance is loud too."
    )


def test_the_python_floor_is_actually_tested():
    """``requires-python`` is a promise to everyone who pip-installs.

    Only running it proves it, so the floor in pyproject must appear in the CI
    matrix. This runs everywhere, not just in CI: raising the floor without
    touching the matrix — or dropping the oldest interpreter from the matrix
    without raising the floor — leaves a version we claim to support and never
    execute.
    """
    import tomllib

    import yaml

    root = Path(__file__).resolve().parent.parent
    floor = tomllib.loads((root / "pyproject.toml").read_bytes().decode())
    floor = floor["project"]["requires-python"]
    assert floor.startswith(">="), f"unexpected requires-python form: {floor!r}"
    floor_version = floor.removeprefix(">=").strip()

    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]

    assert floor_version in matrix, (
        f"pyproject advertises Python {floor_version}, but the CI matrix runs "
        f"{matrix}. The advertised floor is never executed."
    )
    assert min(matrix, key=_version_key) == floor_version, (
        f"CI's oldest interpreter is {min(matrix, key=_version_key)} but the "
        f"advertised floor is {floor_version} — one of them is wrong."
    )


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(part) for part in v.split("."))


# --------------------------------------------------------------------------- export

def test_every_metadata_table_is_classified_by_the_export():
    """A 28th metadata table must not default to travelling, or to not.

    Cross-checked by parsing ``db.py::_SCHEMA`` rather than against a list kept
    here, because a guard that compares one hand-written list to another is
    tautological. Set membership, never a count: SQLite carries 34 tables at
    runtime (the 27 declared plus object_search, five FTS5 shadows and
    sqlite_sequence) and PostgreSQL carries none of those, so any count would
    be wrong on one backend.

    Runs everywhere, not only in CI: it needs no server and the failure it
    catches — a new secret-bearing column shipping unclassified — is one nobody
    should be able to merge locally either.
    """
    import re

    from laurelin.core import db
    from laurelin.export.manifest import TABLE_POLICY

    declared: dict[str, set[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", db._SCHEMA, re.S
    ):
        table, body = match.group(1), match.group(2)
        columns = set()
        for line in body.split("\n"):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            token = line.split()[0]
            if token.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}:
                continue
            # The dialect renders this as a Postgres identity column named
            # `seq` and as nothing at all on SQLite; either way the export has
            # to have an opinion about it.
            columns.add("seq" if token == "{{SEQ_COL}}" else token)
        declared[table] = columns

    assert declared, "the schema parser matched nothing; it has drifted from db.py"

    missing = set(declared) - set(TABLE_POLICY)
    assert not missing, (
        f"{sorted(missing)} exist in db.py::_SCHEMA and are not classified in "
        "laurelin/export/manifest.py::TABLE_POLICY. Decide whether each one is "
        "portable, derived or ephemeral — the default must never be silence."
    )
    stale = set(TABLE_POLICY) - set(declared)
    assert not stale, f"{sorted(stale)} are classified but no longer exist"

    for table, columns in sorted(declared.items()):
        spec = TABLE_POLICY[table]
        classified = set(spec.columns) | set(spec.drop_columns)
        unclassified = columns - classified
        assert not unclassified, (
            f"{table}.{sorted(unclassified)} are neither carried nor explicitly "
            f"dropped. An unclassified column defaults to absent, which is safe "
            f"and silent; say so in TABLE_POLICY so the next reader knows why."
        )
        invented = classified - columns
        assert not invented, f"{table} classifies columns it does not have: {sorted(invented)}"

        # A typo in either of these is a silently disabled control, not a
        # crash: a misspelled conflict_key stops comparing anything, which is
        # how a merge went back to aborting on a raw driver IntegrityError, and
        # a misspelled scan_column stops looking for credentials in a value
        # that still travels.
        for column in spec.conflict_key:
            assert column in columns, f"{table}.conflict_key names {column!r}, which does not exist"
        for column in spec.scan_columns:
            assert column in spec.columns, (
                f"{table}.scan_columns names {column!r}, which this table does not carry"
            )


@in_ci
def test_the_export_roundtrip_runs_on_postgres():
    """The workspace round trip is the only check on two Postgres-only facts.

    ``audit_log.id`` renders as ``BIGINT GENERATED ALWAYS AS IDENTITY`` and
    rejects an explicit insert; ``object_edits.seq`` renders as ``GENERATED BY
    DEFAULT``, which accepts one and leaves the sequence un-advanced so the
    next natural insert collides. Neither failure is reachable from SQLite, so
    a run without a server proves nothing about either.
    """
    assert os.environ.get("LAURELIN_TEST_POSTGRES", "").startswith("postgresql://"), (
        "tests/test_workspace_import.py and "
        "tests/test_export_governance_roundtrip.py both parametrize on the "
        "dialect and skip the Postgres half without LAURELIN_TEST_POSTGRES."
    )
    for name in ("test_workspace_import.py", "test_export_governance_roundtrip.py"):
        source = Path(__file__).with_name(name).read_text()
        assert 'params=["sqlite", "postgres"]' in source, (
            f"{name} no longer runs on both dialects"
        )
