"""R1: a driver's words are never persisted, and a Failure cannot carry them.

Every test here states an invariant of ``laurelin/core/failure.py``. The point
of the module is that safety is a property of the *type*, not of a matcher —
so most of these assert on construction, not on a route.
"""

import json

import pytest

from laurelin.core.failure import (
    DRIVERS,
    Failure,
    FailureCode,
    Phase,
    connect_failure,
    parse_endpoint,
    probe_endpoint,
)

SENTINEL = "S3KRET_PASSWORD_9f21"


class _Boom(Exception):
    """A driver exception whose str() embeds a credential — which is what
    psycopg, mysql-connector and DuckDB all actually do."""


def _driver_exception(text: str = "") -> Exception:
    return _Boom(text or f'connection failed: password="{SENTINEL}" host=db')


# -- the core claim -----------------------------------------------------------

@pytest.mark.parametrize("phase", list(Phase))
def test_a_failure_never_carries_a_substring_of_the_driver_message(phase):
    """Fed an exception whose text is nothing but a credential, the serialized
    record contains none of it. Not a redacted version of it — none of it."""
    failure = Failure.from_exception(
        _driver_exception(), phase=phase, subject="source:crm", driver="psycopg",
    )
    blob = json.dumps(failure.as_dict())
    assert SENTINEL not in blob
    assert "password" not in blob.lower()


def test_every_field_of_a_failure_is_shape_constrained():
    """The two driver-derived fields are gated on the shape of a Python
    identifier and a short vendor code. A value that fails the gate is dropped
    to "", never truncated — half of a string you did not understand is still
    half of whatever was in it."""
    class Evil_at_host_pw(Exception):  # noqa: N801 - the name is the payload
        pass

    exc = Evil_at_host_pw("boom")
    exc.sqlstate = "password=x"  # a driver putting prose in a code field
    failure = Failure.from_exception(exc, subject="source:crm", driver="psycopg")
    # The class name is a legal identifier, so it survives — that is fine, it is
    # a Python identifier and cannot hold `:` `/` `@` `=` space or a quote.
    assert failure.exc_class == "Evil_at_host_pw"
    assert failure.vendor_code == "", "prose in sqlstate must be dropped whole"

    weird = Exception("x")
    weird.sqlstate = "42P01"
    assert Failure.from_exception(weird, driver="psycopg").vendor_code == "42P01"


@pytest.mark.parametrize("bad", [
    "postgresql://alice:pw@db:5432/prod",
    "not a subject",
    "Source:Crm",
    "source:" + "x" * 200,
])
def test_a_subject_outside_our_own_namespace_is_dropped(bad):
    assert Failure(code=FailureCode.REMOTE_FAILED, subject=bad).subject == ""


def test_endpoint_is_rebuilt_from_our_own_parse_and_never_carries_userinfo():
    """`endpoint` comes from Laurelin's reading of Laurelin's config, so
    userinfo is never in scope — strictly better than the tree's old behaviour,
    which `redaction.py` documents as having printed a *fabricated* hostname on
    the ambiguous-`@` path."""
    dsn = f"postgresql://alice:{SENTINEL}@db.internal:5432/prod"
    assert parse_endpoint(dsn) == ("db.internal", 5432)
    failure = Failure.from_exception(
        _driver_exception(), subject="source:crm", driver="psycopg", dsn=dsn,
    )
    assert failure.endpoint == "db.internal:5432"
    assert SENTINEL not in json.dumps(failure.as_dict())


@pytest.mark.parametrize("dsn,expected", [
    # The shapes core/redaction.py measured all three production redactors
    # getting wrong. A wrong host is worse than no host: it is a fabricated
    # fact, printed with the same confidence as a real one.
    ("postgresql://alice:pa/ss@db.internal:5432/prod", None),
    ("postgresql://alice:pa@ss@db.internal:5432/prod", ("db.internal", 5432)),
    ("https://reports.example.com/exports/summary@2024.csv", None),
    ("postgresql://db.internal/prod", ("db.internal", 5432)),
    ("starrocks://laurelin:x@127.0.0.1:9030/lau", ("127.0.0.1", 9030)),
    ("Server=db;Uid=alice;Pwd=hunter2;", None),  # ODBC: no scheme, refused
    ("", None),
])
def test_the_endpoint_parser_refuses_rather_than_fabricating_a_host(dsn, expected):
    assert parse_endpoint(dsn) == expected


def test_an_endpoint_we_did_not_build_is_dropped():
    assert Failure(
        code=FailureCode.REMOTE_FAILED, endpoint="alice:pw@db:5432"
    ).endpoint == ""


def test_a_driver_we_do_not_call_is_refused_rather_than_stored():
    """`driver` names a library so an operator knows whose log line to read. A
    value nobody put in DRIVERS is a value nobody constructed."""
    with pytest.raises(ValueError, match="Unknown driver"):
        Failure(code=FailureCode.REMOTE_FAILED, driver="pass=word")
    for name in DRIVERS:
        Failure(code=FailureCode.REMOTE_FAILED, driver=name)  # no raise


def test_counters_are_integers_so_a_limit_message_cannot_carry_a_string():
    """StarRocks reports memory exhaustion as prose: "Memory of Query<id>
    exceed limit. try consume:…". Extract the numbers; never keep the
    sentence."""
    Failure(
        code=FailureCode.RESOURCE_EXHAUSTED,
        counters={"used_bytes": 8 << 30, "limit_bytes": 4 << 30},
    )
    with pytest.raises((TypeError, ValueError)):
        Failure(code=FailureCode.RESOURCE_EXHAUSTED,
                counters={"used": "Memory of Query exceed limit " + SENTINEL})


def test_a_failure_is_frozen():
    """Nothing may edit a record after its gates have run."""
    failure = Failure(code=FailureCode.REMOTE_FAILED, subject="source:crm")
    with pytest.raises(Exception):
        failure.subject = "source:other"


# -- classification -----------------------------------------------------------

def test_a_malformed_dsn_is_classified_before_the_driver_ever_sees_it():
    """The space-in-password case that defeated all three previous rounds.

    Measured with psycopg 3.3.4: a password containing a space produces
    `ProgrammingError` and a message quoting the password back — and quoting it
    back *re-escaped*, which is why substring-substitution redaction failed.
    Laurelin parses the DSN itself, so the driver is never handed a string it
    can quote."""
    code, phase, _endpoint = probe_endpoint("this is not a dsn at all")
    assert code is FailureCode.CREDENTIAL_MALFORMED
    assert phase is Phase.parse


def test_the_connect_preflight_separates_auth_rejection_from_an_unreachable_endpoint():
    """The distinction psycopg cannot give us.

    Measured on this tree: `sqlstate` is None on **every** psycopg connect
    failure — wrong password, space in password, unknown database, bad host,
    refused port — and all four are `OperationalError` except the
    space-in-password case, which is `ProgrammingError`. Neither the code nor
    the class discriminates, so Laurelin makes the calls itself and reads the
    *stdlib's* answer.
    """
    # A port nothing listens on, on a host that certainly resolves.
    code, phase, endpoint = probe_endpoint("postgresql://u:p@127.0.0.1:1/db")
    assert code is FailureCode.ENDPOINT_UNREACHABLE
    assert phase is Phase.connect
    assert endpoint == "127.0.0.1:1"

    code, phase, _ = probe_endpoint(
        "postgresql://u:p@no-such-host.invalid:5432/db"
    )
    assert code is FailureCode.ENDPOINT_UNRESOLVABLE
    assert phase is Phase.resolve


def test_the_preflight_never_runs_when_the_driver_already_classified_it():
    """A driver that hands us a code from its own vocabulary was *connected*;
    a statement failed. Probing then would misreport the phase and cost a DNS
    lookup for nothing."""
    exc = _driver_exception()
    exc.sqlstate = "42P01"  # UndefinedTable, measured
    failure = connect_failure(
        exc, subject="source:crm", driver="psycopg",
        dsn="postgresql://u:p@127.0.0.1:1/db",
    )
    assert failure.code is FailureCode.RELATION_MISSING
    assert failure.phase is Phase.execute


@pytest.mark.parametrize("sqlstate,expected", [
    ("42P01", FailureCode.RELATION_MISSING),
    ("42703", FailureCode.COLUMN_MISSING),
    ("42601", FailureCode.STATEMENT_INVALID),
    ("42501", FailureCode.PERMISSION_DENIED),
    ("3D000", FailureCode.DATABASE_MISSING),
    ("99999", FailureCode.REMOTE_FAILED),  # unmapped is a normal outcome
])
def test_measured_postgres_sqlstates_map_to_our_vocabulary(sqlstate, expected):
    exc = _driver_exception()
    exc.sqlstate = sqlstate
    assert Failure.from_exception(exc, driver="psycopg").code is expected


@pytest.mark.parametrize("errno,expected", [
    (1045, FailureCode.AUTH_REJECTED),
    (2003, FailureCode.ENDPOINT_UNREACHABLE),
    (2005, FailureCode.ENDPOINT_UNRESOLVABLE),
    (5501, FailureCode.DATABASE_MISSING),
    (5502, FailureCode.RELATION_MISSING),
])
def test_measured_starrocks_errnos_map_to_our_vocabulary(errno, expected):
    """Two corrections to what was inherited, both measured against live
    StarRocks 9030 with mysql-connector 26.7.0: a bad host is errno **2005**,
    not 2003 with a different class, and both arrive as a plain `DatabaseError`
    — the exception class does not discriminate."""
    exc = _driver_exception()
    exc.errno = errno
    assert Failure.from_exception(exc, driver="mysql.connector").code is expected


def test_starrocks_1064_is_deliberately_unmapped():
    """Measured, 1064 is simultaneously syntax error, unresolvable column and
    the memory-limit error. A table claiming it means "syntax" would be
    confidently wrong a third of the time; REMOTE_FAILED plus a detail_ref is
    honest and still actionable."""
    exc = _driver_exception()
    exc.errno = 1064
    assert Failure.from_exception(exc, driver="mysql.connector").code is (
        FailureCode.REMOTE_FAILED
    )


# -- what a reader gets -------------------------------------------------------

def test_the_rendered_sentence_interpolates_only_gated_fields():
    failure = Failure(
        code=FailureCode.AUTH_REJECTED, phase=Phase.authenticate,
        subject="source:crm_orders", endpoint="db.example.com:5432",
        driver="psycopg", exc_class="OperationalError", detail_ref="err-abc123",
    )
    rendered = failure.render()
    assert "db.example.com:5432" in rendered
    assert "source:crm_orders" in rendered
    assert "err-abc123" in rendered


def test_a_viewer_projection_says_that_and_why_in_one_word_and_nothing_else():
    failure = Failure(
        code=FailureCode.AUTH_REJECTED, subject="source:crm",
        endpoint="db.example.com:5432", vendor_code="28P01",
    )
    assert failure.viewer_projection() == {
        "code": "auth_rejected", "subject": "source:crm"
    }


def test_the_detail_ref_is_on_the_record_and_on_the_log_line(caplog):
    """One grep from a stored failure to the traceback behind it. That ref is
    the whole compensation for not storing the driver's sentence."""
    with caplog.at_level("WARNING", logger="laurelin.failure"):
        failure = Failure.from_exception(
            _driver_exception(), subject="source:crm", driver="psycopg",
        )
    assert failure.detail_ref.startswith("err-")
    assert any(failure.detail_ref in r.getMessage() for r in caplog.records)


def test_the_driver_text_reaches_the_log_and_only_the_log(caplog):
    """R1 *relocates* driver text; it does not sanitize it. The server log is
    an operator-privilege artifact — SECURITY.md states that as a deployment
    requirement, because a log shipped to a wide-readership SIEM widens the
    audience of every credential an operator has pasted into a connector."""
    with caplog.at_level("WARNING", logger="laurelin.failure"):
        failure = Failure.from_exception(
            _driver_exception(), subject="source:crm", driver="psycopg",
        )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "connection failed" in logged, (
        "the log line must stay readable — it is the operator's only diagnostic"
    )
    assert any(r.exc_info for r in caplog.records), "the traceback must be attached"
    assert SENTINEL not in json.dumps(failure.as_dict())


def test_the_log_scrubs_secrets_it_can_name_without_destroying_the_message():
    """The courtesy pass, and its limits, stated as a test rather than a hope.

    A secret Laurelin holds in the config is substituted out of the *summary*
    line. A secret it does not hold is not, and `exc_info` carries the untouched
    traceback either way. **This is not a boundary** — SECURITY.md documents the
    log sink as an operator-privilege artifact — and no assertion here treats a
    bypass as a security failure.
    """
    import logging as _logging

    records = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = _logging.getLogger("laurelin.failure")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        Failure.from_exception(
            _driver_exception(), subject="source:crm", driver="psycopg",
            config={"url": f"postgresql://alice:{SENTINEL}@db:5432/prod"},
        )
    finally:
        logger.removeHandler(handler)
    line = "\n".join(records)
    assert SENTINEL not in line
    assert "connection failed" in line


def test_the_driver_field_names_the_library_that_actually_raised():
    """`driver` exists so an operator knows whose log line to go read, and a
    closed set populated *wrongly* is worse than an empty one.

    Measured: `_driver_failure` hardcoded `driver="duckdb"` and also serves the
    ClickHouse and StarRocks registration routes, so a chdb `RuntimeError` was
    filed under duckdb; and the `http` connector was mapped to "requests"
    although `_pull_http` uses `urllib.request` and `requests` is never imported
    on that path. Both sent an operator to a log that does not exist.

    Decided on the raising class's defining module — a fact, and one Laurelin
    maps itself, so an unmapped library yields the caller's default rather than
    a plausible-looking wrong answer.
    """
    import urllib.error

    import duckdb

    from laurelin.core.failure import driver_of

    assert driver_of(duckdb.Error("x"), "psycopg") == "duckdb"
    assert driver_of(urllib.error.URLError("x"), "requests") == "urllib"
    # Unmapped: the caller's default, not a guess.
    assert driver_of(RuntimeError("x"), "duckdb") == "duckdb"
    assert driver_of(RuntimeError("x")) == ""

    # ...and the two production converters actually consult it. Asserting only
    # on `driver_of` would leave both call sites free to keep hardcoding, which
    # is precisely what they were doing.
    from laurelin.api.routes import _driver_failure

    f = _driver_failure(
        urllib.error.URLError("boom"), {"type": "parquet"}, "dataset:x"
    )
    assert f.driver == "urllib", (
        "`_driver_failure` hardcoded duckdb and also serves the ClickHouse and "
        "StarRocks registration routes, so a chdb failure was filed under "
        "duckdb and the ref pointed at a log line that does not exist"
    )

    from laurelin.connectors.connectors import _sync_failure
    from laurelin.core.models import SourceInfo

    source = SourceInfo(name="feed", type="http", dataset="d", created_by="root",
                        config={"url": "https://example.invalid/a.csv"})
    # Two mechanisms, and each is asserted with the input only *it* can answer,
    # so reverting either one is visible. (Feeding both an urllib error would
    # let them cover for each other — which is how the first version of this
    # test passed with the defect restored.)
    #
    # Only the per-type map can answer this: nothing maps a bare RuntimeError.
    assert _sync_failure(RuntimeError("boom"), source).driver == "urllib", (
        "the `http` connector was mapped to `requests`, which `_pull_http` "
        "never imports — it uses `urllib.request`"
    )
    # Only `driver_of` can answer this: the map would say urllib for an http
    # source, and the fact is that duckdb raised.
    assert _sync_failure(duckdb.Error("boom"), source).driver == "duckdb"


def test_is_first_party_is_decided_by_where_the_raise_is_written():
    """Not by the exception's type — a library can raise a bare `ValueError`,
    and a subclass of `ValueError` can be defined anywhere. `pyarrow`'s
    `ArrowInvalid` is a `ValueError` and its `ArrowKeyError` is a `KeyError`,
    which is how an editor came to read an operator's S3 credential out of a
    400 body."""
    import pyarrow as pa

    # Raised inside `laurelin/connectors/connectors.py`, which is the shape that
    # matters: an ordinary first-party 400. Raising from *this* file would not
    # test anything — tests are not under `laurelin/`, and a passing assertion
    # here would have meant the check was accepting everything.
    from laurelin.connectors.connectors import validate_source
    from laurelin.core.failure import first_party_message, is_first_party

    try:
        validate_source("not_a_connector", {})
    except ValueError as exc:
        assert is_first_party(exc)
        assert "not_a_connector" in first_party_message(exc)

    try:
        pa.compute.cast(pa.array(["not a number"]), pa.int64())
    except ValueError as exc:
        assert isinstance(exc, ValueError)
        assert not is_first_party(exc), (
            "a pyarrow exception must not be mistaken for one of ours just "
            "because it inherits from a builtin"
        )

    # No traceback at all vouches for nothing.
    assert not is_first_party(ValueError("never raised"))


def test_render_brief_drops_the_endpoint_and_keeps_the_handle():
    """`endpoint` is the one field of a Failure whose value comes from somebody's
    config rather than from a closed set, so it is the one thing to drop when
    the reader is below the level that authored that config."""
    from laurelin.core.failure import Failure, FailureCode, Phase

    f = Failure(
        code=FailureCode.ENDPOINT_UNRESOLVABLE, phase=Phase.resolve,
        subject="source:crm", endpoint="secret-db.internal.corp:55999",
        driver="psycopg", exc_class="OperationalError", detail_ref="err-abc123",
    )
    assert "secret-db.internal.corp" in f.render()
    brief = f.render_brief()
    assert "secret-db.internal.corp" not in brief
    assert "source:crm" in brief and "err-abc123" in brief
    # And the audit projection is narrower still: no driver, no class.
    assert f.audit_projection() == {
        "code": "endpoint_unresolvable", "subject": "source:crm",
        "detail_ref": "err-abc123",
    }
