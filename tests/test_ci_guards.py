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

in_ci = pytest.mark.skipif(
    os.environ.get("CI", "").lower() not in {"1", "true"},
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


def test_the_starrocks_job_is_opt_in_and_still_exists():
    """It is not in the matrix — the image is 3.18 GB — so nothing else would
    notice if it were deleted or renamed."""
    import yaml

    root = Path(__file__).resolve().parent.parent
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"].get("starrocks")
    assert job is not None, "the opt-in StarRocks job has gone missing"
    assert "if" in job, "it must stay opt-in rather than joining every run"
    env = [s.get("env", {}) for s in job["steps"]]
    assert any("LAURELIN_TEST_STARROCKS" in e for e in env), (
        "the job would start a container and then skip every test"
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
