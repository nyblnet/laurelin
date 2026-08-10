"""The three production redactors, attacked with the DSNs people actually have.

Every input in ``HOSTILE`` was run against ``federation.redacted_source``,
``engines._redact_uri``/``EngineConfig.redacted`` and
``connectors.redacted_config`` on the tree before ``core/redaction.py`` existed.
Ten of them came back with the credential intact, on routes an admin reads in a
browser:

    input                                    federation  engines  connectors
    postgresql://alice:pa/ss@db:5432/prod    LEAK        ok       LEAK
    postgresql://:hunter2@db:5432/prod       LEAK        LEAK     ok
    https://api.x.com/e.csv?api_key=SEKRET   LEAK        LEAK     LEAK
    Server=db;Uid=alice;Pwd=hunter2;         LEAK        LEAK     LEAK
    https://ghp_TOKEN@github.com/o/r.csv     LEAK        -        LEAK
    {"auth": {"password": "SEKRET"}}         LEAK        -        LEAK
    {"headers": {"X-Api-Key": "SEKRET"}}     LEAK        -        LEAK
    adbc...call_header.authorization: Bearer -           LEAK     -

The two rows the old regexes did get right are in the table below as well, so a
future rewrite has to keep them right.

These tests are the *whole* API surface of the redaction policy: what is masked,
what is withheld, and — in ``test_the_endpoint_disclosure_policy_is_unchanged``
— what is still deliberately disclosed while the product question about
``user@host:port/db`` is open.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.connectors import redacted_config
from laurelin.core import engines, federation, redaction
from laurelin.core.config import Workspace
from laurelin.core.redaction import MASK, WITHHELD

# (label, value, the substring that must never survive)
HOSTILE = [
    ("plain", "postgresql://alice:hunter2@db.internal:5432/prod", "hunter2"),
    ("password holds @", "postgresql://alice:pa@ss@db:5432/prod", "pa@ss"),
    ("password holds /", "postgresql://alice:pa/ss@db:5432/prod", "pa/ss"),
    ("password holds :", "postgresql://alice:pa:ss@db:5432/prod", "pa:ss"),
    ("password holds ?", "postgresql://alice:pa?ss@db:5432/prod", "pa?ss"),
    ("password holds #", "postgresql://alice:pa#ss@db:5432/prod", "pa#ss"),
    ("password holds //", "postgresql://alice:p//w@db:5432/prod", "p//w"),
    ("no username", "postgresql://:hunter2@db:5432/prod", "hunter2"),
    ("no username, / in pw", "postgresql://:h/2@db:5432/prod", "h/2"),
    ("bare userinfo token", "https://ghp_SEKRET@github.com/o/r.csv", "ghp_SEKRET"),
    ("query parameter", "https://api.x.com/e.csv?api_key=SEKRET", "SEKRET"),
    ("query on a DSN", "postgresql://db:5432/prod?password=SEKRET", "SEKRET"),
    ("fragment", "https://api.x.com/e.csv#token=SEKRET", "SEKRET"),
    ("odbc keyword form", "Server=db;Uid=alice;Pwd=hunter2;", "hunter2"),
    ("odbc braces", "Driver={x};Server=db;PWD={hun;ter2};", "hun;ter2"),
    ("jdbc wrapper", "jdbc:postgresql://db/prod?password=SEKRET", "SEKRET"),
    ("uppercase scheme", "POSTGRESQL://alice:hunter2@db/prod", "hunter2"),
    ("ipv6 host", "postgresql://alice:hunter2@[::1]:5432/prod", "hunter2"),
    ("multi-host", "postgresql://alice:hunter2@h1:5432,h2:5432/prod", "hunter2"),
    ("unicode password", "postgresql://alice:hüntér@db/prod", "hüntér"),
    ("at in the path", "postgresql://alice:hunter2@db/pro@d", "hunter2"),
    ("percent-encoded", "postgresql://alice:h%2Funter2@db/prod", "h%2Funter2"),
    ("mysql scheme", "mysql://alice:hunter2@db:3306/prod", "hunter2"),
    ("starrocks scheme", "starrocks://alice:hunter2@sr:9030/lau", "hunter2"),
    ("grpc scheme", "grpc+tls://alice:hunter2@trino:443", "hunter2"),
    ("s3 presigned", "s3://AKIAX:SEKRET@bucket/key.parquet", "SEKRET"),
]


def _flat(value) -> str:
    """Everything a response would carry, as one string to search."""
    return json.dumps(value, default=str, ensure_ascii=False)


# --------------------------------------------------------------- the three redactors

@pytest.mark.parametrize("label, dsn, secret", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_no_dsn_form_leaks_its_credential_through_a_federated_source(label, dsn, secret):
    red = federation.redacted_source({"type": "postgres", "url": dsn, "table": "events"})
    assert secret not in _flat(red)
    # The row survives: a source that renders as nothing at all is a bug report,
    # not a redaction.
    assert red["table"] == "events"


@pytest.mark.parametrize("label, dsn, secret", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_no_dsn_form_leaks_its_credential_through_a_connector_config(label, dsn, secret):
    assert secret not in _flat(redacted_config({"url": dsn, "table": "orders"}))


@pytest.mark.parametrize("label, dsn, secret", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_no_dsn_form_leaks_its_credential_through_an_engine_uri(label, dsn, secret):
    config = engines.EngineConfig(name="e", uri=dsn, options={})
    assert secret not in _flat(config.redacted())
    assert secret not in engines._redact_uri(dsn)


@pytest.mark.parametrize("label, dsn, secret", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_a_dsn_hidden_in_a_free_form_value_still_loses_its_credential(label, dsn, secret):
    """A postgres source's ``query`` is arbitrary SQL, and DuckDB's
    ``postgres_scan('<dsn>')`` puts a whole DSN inside it. Every one of the
    three redactors looked only at a key spelled ``url``, so this was disclosed
    in full even for the DSN forms they otherwise handled."""
    sql = f"SELECT * FROM postgres_scan('{dsn}', 'public', 't')"
    red = redacted_config({"url": "postgresql://h/db", "query": sql})
    if dsn.startswith(("postgresql://", "mysql://", "s3://", "grpc", "https://",
                       "starrocks://", "POSTGRESQL://")):
        assert secret not in _flat(red)


def test_the_endpoint_disclosure_policy_is_unchanged():
    """**The open product question, pinned.**

    Should a viewer see ``user@host:port/db``, or only the password redacted?
    Nobody has answered that, so this fix did not answer it either: on a DSN
    whose shape is known, exactly what was shown before is shown now. If the
    answer ever comes back "endpoints are not for viewers", this test is the one
    that has to change, deliberately — and ``export/secrets.py`` already
    contains the stricter policy to copy.
    """
    dsn = "postgresql://alice:hunter2@db.internal:5432/prod"
    for red in (
        federation.redacted_source({"url": dsn})["url"],
        redacted_config({"url": dsn})["url"],
        engines._redact_uri(dsn),
    ):
        assert red == "postgresql://alice:*****@db.internal:5432/prod"
        assert "alice" in red and "db.internal" in red and "5432" in red and "prod" in red


def test_a_dsn_with_no_credential_in_it_is_not_mangled():
    for red in (
        federation.redacted_source({"url": "starrocks://sr:9030/lau"})["url"],
        redacted_config({"url": "https://host/export.csv"})["url"],
        engines._redact_uri("grpc+tls://trino.internal:443"),
    ):
        assert MASK not in red


# ------------------------------------------------------------------ withholding

def test_a_value_whose_shape_we_cannot_read_is_withheld_whole():
    """Guessing where the secret sits in a format nobody here defined is what
    produced eight of the ten leaks. An ODBC keyword string has driver-specific
    quoting (``{}`` escapes, embedded ``;``) and driver-specific key spellings,
    so it does not travel at all."""
    odbc = "Driver={ODBC Driver 18};Server=db;Uid=alice;Pwd={hun;ter2};"
    assert federation.redacted_source({"url": odbc})["url"] == WITHHELD
    assert redacted_config({"url": odbc})["url"] == WITHHELD
    assert engines._redact_uri(odbc) == WITHHELD


def test_a_url_carrying_a_query_is_withheld_rather_than_guessed_at():
    """A query parameter's *name* is chosen by whoever built the URL —
    ``api_key``, ``sig``, ``X-Amz-Signature``, ``access_token``. Matching those
    names is the denylist that has already failed twice in this repo, so the
    whole value goes. The cost is that an innocent ``?format=csv`` is withheld
    too; that is the price of not guessing, and it is the one place this policy
    shows less than it did."""
    assert redacted_config({"url": "https://api.x.com/e.csv?api_key=SEKRET"})["url"] == WITHHELD
    assert redacted_config({"url": "https://api.x.com/e.csv?format=csv"})["url"] == WITHHELD


def test_a_withheld_value_is_a_visible_marker_and_never_a_blank():
    """A blank field reads as "nothing was configured" and gets re-entered; a
    marker reads as "this was withheld". ``EnginesSection.tsx`` renders
    ``e.uri`` straight into a table cell, so this is the difference between an
    operator seeing a policy and an operator filing a bug."""
    assert WITHHELD not in ("", None)
    assert WITHHELD != MASK  # "we removed the password" is a different claim
    assert "withheld" in WITHHELD


def test_nested_config_keeps_its_names_and_loses_every_value():
    """``redacted_config`` walked the top level plus ``headers`` and nothing
    else, so ``{"auth": {"password": "SEKRET"}}`` came back verbatim."""
    red = redacted_config({
        "type": "http",
        "auth": {"password": "SEKRET", "scheme": "basic"},
        "retry": {"policy": {"token": "SEKRET", "attempts": 3}},
        "hosts": ["postgresql://alice:SEKRET@db/prod"],
    })
    assert "SEKRET" not in _flat(red)
    assert red["retry"] == {"policy": {"token": WITHHELD, "attempts": WITHHELD}}
    assert red["type"] == "http"  # top-level scalars are still the config


def test_every_header_value_is_withheld_and_every_header_name_survives():
    """The name denylist missed ``X-Api-Key`` — ``api_?key`` does not match
    ``Api-Key`` — and would equally miss ``Cookie`` and
    ``Proxy-Authorization``. Values go wholesale instead; the names stay,
    because "this source sends an Authorization header" is the fact the
    operator is checking."""
    red = redacted_config({"headers": {
        "X-Api-Key": "SEKRET", "Cookie": "session=SEKRET",
        "Authorization": "Bearer SEKRET", "Accept": "text/csv",
    }})
    assert "SEKRET" not in _flat(red)
    assert sorted(red["headers"]) == ["Accept", "Authorization", "Cookie", "X-Api-Key"]
    assert set(red["headers"].values()) == {WITHHELD}


def test_every_engine_option_value_is_withheld_including_the_adbc_namespace():
    """``adbc.flight.sql.rpc.call_header.authorization`` contains none of
    ``password|secret|token|key|credential``, so ``GET /api/v1/engines`` served
    ``Bearer SEKRET`` verbatim. These options are ADBC's vocabulary, not ours,
    and name-matching over somebody else's namespace is a guess."""
    red = engines.EngineConfig(
        name="trino", uri="grpc+tls://trino:443",
        options={
            "adbc.flight.sql.rpc.call_header.authorization": "Bearer SEKRET",
            "adbc.flight.sql.rpc.call_header.x-tenant": "acme",
            "password": "hunter2",
        },
    ).redacted()
    assert "SEKRET" not in _flat(red) and "hunter2" not in _flat(red)
    assert set(red["options"].values()) == {WITHHELD}
    assert "adbc.flight.sql.rpc.call_header.authorization" in red["options"]


def test_the_secret_name_list_does_not_mask_a_field_for_containing_three_letters():
    """Unanchored, ``auth`` matches ``author`` and ``sig`` matches
    ``assigned_to``. Masking a field because its name happens to contain a
    substring is the imprecision that gets a redactor distrusted and then
    worked around, and the *shape* rules — not this list — are what make the
    module safe."""
    red = redacted_config({"author": "ann", "assigned_to": "bo", "auth": "x",
                           "signature": "x", "table": "orders"})
    assert red["author"] == "ann" and red["assigned_to"] == "bo"
    assert red["auth"] == MASK and red["signature"] == MASK


def test_a_config_that_is_not_an_object_is_withheld():
    """``SourceUpsertRequest.config`` is typed, but a row written by an older
    build or by hand is not. There are no keys to judge here, so there is
    nothing to disclose."""
    assert redaction.redact_mapping(["postgresql://a:SEKRET@h/db"]) == WITHHELD
    assert redaction.redact_mapping("postgresql://a:SEKRET@h/db") == WITHHELD
    assert redaction.redact_mapping(None) is None


def test_redaction_does_not_invent_values_for_the_boring_cases():
    assert redaction.redact_dsn("") == ""
    assert redaction.redact_dsn(None) is None
    assert redaction.redact_dsn(7) == 7
    assert redacted_config({}) == {}
    assert redacted_config({"batch_size": 500, "enabled": True, "since": None}) == {
        "batch_size": 500, "enabled": True, "since": None
    }


# --------------------------------------------------------------- free-form text

def test_an_engine_error_message_never_echoes_the_credential():
    """``engine_routes`` ran ``_redact_uri`` over ``str(exc)`` — a URI redactor
    over a driver's prose. Whatever the driver quoted back that was not shaped
    like ``//user:pass@`` travelled."""
    text = ("Could not connect to engine 'trino' at "
            "grpc+tls://alice:hunter2@trino:443: Flight returned unauthenticated")
    out = redaction.redact_text(text)
    assert "hunter2" not in out
    assert "grpc+tls://alice:*****@trino:443" in out
    assert "Flight returned unauthenticated" in out


@pytest.mark.parametrize("text", [
    "unauthenticated: invalid token abc123SEKRET",
    'password authentication failed: "SEKRET"',
    "Server=db;Uid=alice;Pwd=SEKRET; is unreachable",
    "Authorization: Bearer SEKRET rejected",
    "api_key=SEKRET was not accepted",
])
def test_prose_that_still_looks_like_a_credential_is_withheld_whole(text):
    """No redactor in this tree covers free-form driver text, so the message
    goes rather than travelling half-read. The unredacted exception is logged
    server-side — see ``engine_routes.test_engine``."""
    assert redaction.redact_text(text) == WITHHELD


def test_the_masked_dsn_does_not_make_its_own_message_look_like_a_credential():
    """The credential scanner matches the *word*, so run over the whole string
    it would trip on the ``postgresql://`` this function just finished masking
    and withhold a message that is already safe. It sees only the prose."""
    out = redaction.redact_text("connect to postgresql://alice:hunter2@db/prod timed out")
    assert out != WITHHELD and "hunter2" not in out


# ------------------------------------------------------------- live admin routes

def _admin(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="red")
    client = TestClient(create_app(ws))
    creds = {"username": "root", "password": "trustno1!"}
    client.post("/api/v1/auth/setup", json=creds)
    client.post("/api/v1/auth/login", json=creds)
    return client


@pytest.mark.parametrize("label, dsn, secret", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_no_credential_reaches_a_browser_through_the_sources_routes(
    tmp_path, label, dsn, secret
):
    """End to end on the live route, because that is where the leak was: the
    unit is only evidence if the response body agrees with it."""
    admin = _admin(tmp_path)
    created = admin.put("/api/v1/sources/s", json={
        "type": "file", "dataset": "d",
        "config": {"path": "/land/*.csv", "format": "csv", "url": dsn,
                   "headers": {"X-Api-Key": secret}},
    })
    assert created.status_code == 200, created.text
    assert secret not in created.text
    assert secret not in admin.get("/api/v1/sources").text
    assert secret not in admin.get("/api/v1/sources/s").text


def test_a_keyword_string_filed_under_a_key_that_does_not_claim_a_url_is_disclosed():
    """**A residual, pinned rather than assumed.**

    A value is held to the DSN rules when the config says it is an endpoint —
    ``url``, ``uri``, ``base_url`` — or when it is visibly URL-shaped. A driver
    blob parked under some other name is neither, and withholding every scalar
    that might one day be one would empty the Sources screen: a file source's
    ``path`` is ``/mnt/land/*.csv``, and that is the common case by a mile.

    So this is disclosed, and it is disclosed *knowingly*. Closing it means
    answering the same open question as ``user@host:port/db``: whether these
    routes should show operator-supplied values at all, or only the shape of
    them, as ``export/secrets.py`` does with its allowlist.
    """
    odbc = "Server=db;Uid=alice;Pwd=hunter2;"
    assert redacted_config({"note": odbc})["note"] == odbc
    # But the moment it looks like a URL, wherever it sits, it is redacted.
    assert "hunter2" not in _flat(
        redacted_config({"note": "postgresql://alice:hunter2@db/prod"})
    )


def test_no_credential_reaches_a_browser_through_the_engines_routes(tmp_path):
    admin = _admin(tmp_path)
    created = admin.put("/api/v1/engines/trino", json={
        "type": "flightsql", "uri": "grpc+tls://svc:hunter2@trino.internal:443",
        "options": {"adbc.flight.sql.rpc.call_header.authorization": "Bearer SEKRET"},
    })
    assert created.status_code == 200, created.text
    for body in (created.text, admin.get("/api/v1/engines").text):
        assert "hunter2" not in body and "SEKRET" not in body
    # And the endpoint policy is the same one the DSN rules state.
    assert created.json()["uri"] == "grpc+tls://svc:*****@trino.internal:443"


def test_an_engine_connectivity_failure_reports_without_disclosing(tmp_path):
    admin = _admin(tmp_path)
    admin.put("/api/v1/engines/trino", json={
        "type": "flightsql", "uri": "grpc+tls://svc:hunter2@127.0.0.1:1",
        "options": {},
    })
    r = admin.post("/api/v1/engines/trino/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "hunter2" not in r.text
    # Whichever way it went, the operator is told which one it was.
    assert body["withheld"] is (body["detail"] == WITHHELD)


def test_no_credential_reaches_a_browser_through_a_federated_dataset(tmp_path):
    """``_dump`` redacts ``DatasetInfo.source`` for every dataset route, and
    those routes are viewer-readable — this is the widest audience of the
    three."""
    admin = _admin(tmp_path)
    r = admin.put("/api/v1/datasets/remote/federate", json={
        "source": {"type": "postgres", "table": "public.events",
                   "url": "postgresql://:hunter2@127.0.0.1:1/prod"},
    })
    # Registration probes the server, so it fails here — what matters is that
    # neither the failure nor any later read carries the password.
    assert "hunter2" not in r.text
    assert "hunter2" not in admin.get("/api/v1/datasets").text


# --------------------------------------------------------------------- the UI

def test_the_built_ui_renders_a_withheld_value_as_withheld():
    """A withheld field that renders as an empty cell is indistinguishable from
    an unconfigured one, and the operator's next move is to re-enter the
    credential. The bundle has to carry the marker and an explanation."""
    bundle = Path(__file__).resolve().parents[1] / "laurelin/ui/static/index.html"
    text = bundle.read_text(encoding="utf-8")
    assert WITHHELD in text
    assert "could not be redacted safely" in text


# --------------------------------------------------- truncated embedded DSNs
#
# Everything below this line is the second round: the module above shipped, and
# an attacker went at it. These are the holes that were found in it.

# One character of a password is the whole difference. Each of these is a
# terminator in `_EMBEDDED_URL_RE`, so the match stopped before the `@` and
# `redact_dsn` was handed a fragment with no userinfo in it.
TRUNCATING = [
    ("comma", ","), ("space", " "), ("semicolon", ";"), ("quote", "'"),
    ("double quote", '"'), ("open paren", "("), ("close paren", ")"),
    ("open bracket", "["), ("close bracket", "]"), ("brace", "}"),
    ("angle", ">"), ("tab", "\t"), ("newline", "\n"),
]


@pytest.mark.parametrize("label, ch", TRUNCATING, ids=[t[0] for t in TRUNCATING])
def test_a_password_holding_a_url_delimiter_does_not_survive_in_a_free_form_value(
    label, ch
):
    """The ``query`` of a postgres source is arbitrary SQL naming a second DSN,
    and it is EDITOR-readable and printed in the Sources "From" column.

    ``test_a_dsn_hidden_in_a_free_form_value_still_loses_its_credential`` above
    claims to cover this shape and does — for passwords made of characters that
    happen not to end a URL in prose. Measured through the live route, six of
    these terminators shipped the password in full.
    """
    dsn = f"postgresql://alice:pa{ch}SEKRETTAIL@db.internal:5432/prod"
    sql = f"SELECT * FROM postgres_scan('{dsn}', 'public', 'events')"
    assert "SEKRETTAIL" not in _flat(redacted_config({"query": sql}))
    assert "SEKRETTAIL" not in _flat(federation.redacted_source({"query": sql}))


@pytest.mark.parametrize("label, ch", TRUNCATING, ids=[t[0] for t in TRUNCATING])
def test_a_password_holding_a_url_delimiter_does_not_survive_in_driver_prose(label, ch):
    """The same truncation defeated ``redact_text``'s *withhold* branch as
    well, and for a reason worth naming: stripping the cut-short match also
    strips the ``://`` that ``looks_like_a_credential``'s ``url_userinfo`` and
    ``dsn`` patterns key on. Neither branch fired, so the message went out
    whole — this is the ``POST /engines/{name}/test`` body."""
    text = (f"Could not connect at grpc+tls://svc:pa{ch}SEKRETTAIL@trino:443: "
            f"connection refused")
    assert "SEKRETTAIL" not in str(redaction.redact_text(text))


def test_a_credential_free_url_beside_an_unrelated_at_sign_is_still_shown():
    """The cost of the rule above, bounded.

    A free-form value is withheld when a match was cut short *and* an ``@``
    follows it. A complete authority — one whose ``/`` is inside the match and
    which parses as ``host[:port]`` — is not cut short, so an ``@`` elsewhere
    in the string (an email address in a WHERE clause) does not withhold it.
    Without that second half of the test the Sources screen would blank out on
    an ordinary query.
    """
    sql = "SELECT * FROM read_csv('https://x.example.com/a.csv') WHERE u = 'a@b.com'"
    assert redacted_config({"query": sql})["query"] == sql


def test_a_url_whose_path_holds_an_at_sign_is_withheld_not_given_a_fake_host():
    """**Two different endpoints must not render as one.**

    ``redact_dsn`` took the last ``@`` in the whole remainder, so a path
    containing one was read as userinfo: the host was not masked, it was
    *replaced*. Measured — these two URLs, differing only in host, both came
    back as ``https://*****@2024.csv``, and ``ui.tsx`` draws the "withheld"
    explanation only on an exact ``WITHHELD`` match, so an admin saw a
    plausible, fictitious endpoint with no sign anything had been removed.

    ``MASK`` promises the credential was found and the rest is real. Where that
    promise cannot be kept the answer is ``WITHHELD``, not a prettier lie.
    """
    prod = "https://reports.prod.example.com/exports/summary@2024.csv"
    stage = "https://reports.stage.example.com/exports/summary@2024.csv"
    assert redacted_config({"url": prod})["url"] == WITHHELD
    assert redacted_config({"url": stage})["url"] == WITHHELD
    # The realistic shapes: an S3 landing prefix keyed by email address, and a
    # date-tagged filename. Neither may put a fabricated bucket on screen.
    for url in ("s3://analytics-prod/landing/user@corp.com/daily.parquet",
                "https://data.example.com:8443/exports/report@2024.csv"):
        assert redacted_config({"url": url})["url"] == WITHHELD


def test_a_url_with_no_slash_before_its_at_sign_is_still_masked_not_withheld():
    """The other side of that boundary, so the fix cannot quietly become
    "withhold everything". These have no ``/`` between the scheme and the
    ``@``, so the authority is unambiguous and the endpoint policy holds."""
    assert redacted_config(
        {"url": "s3://AKIAX:SEKRET@bucket/key.parquet"}
    )["url"] == "s3://AKIAX:*****@bucket/key.parquet"
    assert redacted_config(
        {"url": "https://ghp_SEKRET@github.com/o/r.csv"}
    )["url"] == "https://*****@github.com/o/r.csv"


def test_a_password_equal_to_its_username_is_not_disclosed_by_the_username():
    """The one case where the acknowledged endpoint disclosure is not a
    hostname but a working credential.

    The policy pinned by ``test_the_endpoint_disclosure_policy_is_unchanged``
    shows the username and masks the password. When they hold the same string
    that policy prints the secret. Both slots go — this does not reopen the
    product question, it declines to answer it wrongly in the one case where
    the answer is a live password.
    """
    red = redacted_config({"url": "postgresql://hunter2:hunter2@db.internal:5432/prod"})
    assert "hunter2" not in _flat(red)
    assert red["url"] == "postgresql://*****@db.internal:5432/prod"


# ------------------------------------------------------- free-form driver text

def test_a_credential_a_driver_quotes_as_prose_is_substituted_from_the_config():
    """No redactor can find a credential in a third party's sentence. It does
    not have to: we issued the credential, and the config says so.

    Measured: psycopg rejects a password containing a space with ``unexpected
    spaces found in "SUPER SEKRET"`` — no ``password``, no ``://``, no shape at
    all. Every pattern in ``export/pipeline_scan.py`` reads it as innocent
    prose, and it was stored in ``sources.last_sync_error`` and served.
    """
    config = {"url": "postgresql://alice:SUPER SEKRET@db.internal:5432/prod"}
    text = 'ProgrammingError: unexpected spaces found in "SUPER SEKRET", use %20'
    out = str(redaction.redact_driver_text(text, redaction.secrets_in_config(config)))
    assert "SUPER SEKRET" not in out
    # The sentence survives: an operator who cannot tell what failed reads the
    # log instead, and this field exists so they do not have to.
    assert "unexpected spaces" in out


def test_a_credential_in_a_url_query_is_substituted_even_when_the_scheme_is_mangled():
    """chdb rewrites ``s3://k:pw@bucket/x`` to ``s3:/k:pw@bucket/x`` in its
    error text, which removes the ``://`` every shape rule in this module keys
    on. Substituting what the config says we handed over does not care."""
    config = {"path": "s3://AKIAX:SEKRETPW@bucket/x.parquet"}
    text = "Cannot stat file /srv/s3:/AKIAX:SEKRETPW@bucket/x.parquet: errno 2"
    out = str(redaction.redact_driver_text(text, redaction.secrets_in_config(config)))
    assert "SEKRETPW" not in out


def test_a_driver_message_that_still_holds_a_known_secret_is_withheld_whole():
    """The backstop. A secret too short to substitute out of a sentence without
    wrecking it is not a secret we may ship — the verify pass sees it survive
    and withholds the message rather than returning a partially-scrubbed one."""
    out = redaction.redact_driver_text("connection refused for ab", ["ab"])
    assert out == WITHHELD


def test_the_query_string_of_a_configured_url_counts_as_a_secret():
    config = {"url": "http://data.example.com/export.csv?api_key=SEKRET_TOKEN"}
    assert "SEKRET_TOKEN" in redaction.secrets_in_config(config)


# --------------------------------------------------- live routes, second round

def test_a_failed_sync_never_puts_the_password_in_the_source_row(tmp_path):
    """``config.url`` is masked and ``last_sync_error`` sat in the same JSON
    object, unredacted, on an EDITOR-readable route."""
    admin = _admin(tmp_path)
    secret = "SUPER SEKRET"
    admin.put("/api/v1/datasets/d", json={"name": "d"})
    admin.put("/api/v1/sources/s", json={
        "type": "postgres", "dataset": "d",
        "config": {"url": f"postgresql://alice:{secret}@db.internal:5432/prod",
                   "table": "events"},
    })
    failed = admin.post("/api/v1/sources/s/sync")
    assert failed.status_code == 502
    assert secret not in failed.text
    assert secret not in admin.get("/api/v1/sources").text
    assert secret not in admin.get("/api/v1/sources/s").text
    assert admin.get("/api/v1/sources/s").json()["last_sync_status"] == "failed"


def test_a_viewer_cannot_read_a_connector_password_out_of_the_audit_trail(tmp_path):
    """``GET /audit`` is VIEWER-gated and ``GET /sources`` is not, so a driver's
    exception dropped into an audit row is a strict privilege escalation over
    the redacted config beside it. Measured: the viewer read the password and
    got 403 on the source."""
    admin = _admin(tmp_path)
    secret = "SUPER SEKRET"
    admin.put("/api/v1/datasets/d", json={"name": "d"})
    admin.put("/api/v1/sources/s", json={
        "type": "postgres", "dataset": "d",
        "config": {"url": f"postgresql://alice:{secret}@db.internal:5432/prod",
                   "table": "events"},
    })
    admin.post("/api/v1/sources/s/sync")
    admin.post("/api/v1/users",
               json={"username": "vw", "password": "viewerpw1!", "role": "viewer"})
    viewer = TestClient(admin.app)
    viewer.post("/api/v1/auth/login",
                json={"username": "vw", "password": "viewerpw1!"})
    audit = viewer.get("/api/v1/audit")
    assert audit.status_code == 200
    assert secret not in audit.text
    # The escalation this closes, stated: the same user cannot read the source.
    assert viewer.get("/api/v1/sources").status_code == 403
    # And the trail still says what happened.
    assert any(e["action"] == "source_sync_failed" for e in audit.json())


def test_registering_a_federated_source_never_echoes_the_dsn_back(tmp_path):
    """DuckDB's postgres extension prefixes its IO Error with the whole
    connection string, and the 502 body was ``str(exc)`` — so the password went
    to the browser, the reverse proxy's log and the UI's error box, one line
    above a success path that calls ``redacted_source``."""
    admin = _admin(tmp_path)
    r = admin.put("/api/v1/datasets/f1/federated", json={
        "source": {"type": "postgres", "table": "public.t",
                   "url": "postgresql://alice:SEKRETPW@127.0.0.1:1/prod"},
    })
    assert r.status_code == 502
    assert "SEKRETPW" not in r.text
    # Still diagnostic: the operator must be able to tell a refused connection
    # from a missing table.
    assert "127.0.0.1" in r.text


def test_a_dashboard_panel_may_not_store_a_credential_a_viewer_would_read(tmp_path):
    """A panel's SQL is executed and round-trips through the editor's textarea,
    so it cannot be redacted on the way out — a mask returned here is saved
    over the real query on the next PUT. It is refused on the way in instead.

    ``GET /dashboards`` is VIEWER-gated; the identical DSN in a source's
    ``query`` is redacted at EDITOR. This was the same leak one level lower.
    """
    admin = _admin(tmp_path)
    r = admin.put("/api/v1/dashboards/d1", json={"title": "t", "panels": [
        {"id": "p1", "title": "p", "kind": "table",
         "sql": "SELECT * FROM postgres_scan("
                "'postgresql://alice:DASHSEKRET@db.internal:5432/prod','public','t')"}]})
    assert r.status_code == 400
    assert "DASHSEKRET" not in r.text
    assert admin.get("/api/v1/dashboards").json() == []
    # A panel with no credential in it is unaffected.
    ok = admin.put("/api/v1/dashboards/d2", json={"title": "t", "panels": [
        {"id": "p1", "title": "p", "kind": "table",
         "sql": "SELECT * FROM read_csv('https://data.example.com/a.csv')"}]})
    assert ok.status_code == 200, ok.text


def test_a_schedule_target_may_not_store_a_credential(tmp_path):
    """``export/secrets.py`` cites ``s3://k:SCHEDPASS10@bucket/t`` as a measured
    real case and the export path drops it; ``GET /schedules`` returned it."""
    admin = _admin(tmp_path)
    r = admin.put("/api/v1/schedules/s1", json={
        "trigger": "cron", "cron": "0 * * * *", "action": "build",
        "targets": ["s3://key:SCHEDSEKRET@bucket/t"]})
    assert r.status_code == 400
    assert "SCHEDSEKRET" not in r.text
    assert "SCHEDSEKRET" not in admin.get("/api/v1/schedules").text
    ok = admin.put("/api/v1/schedules/s2", json={
        "trigger": "cron", "cron": "0 * * * *", "action": "build",
        "targets": ["s3://bucket/t"]})
    assert ok.status_code == 200, ok.text


def test_the_audit_route_redacts_a_credential_no_writer_thought_to_redact(tmp_path):
    """The backstop, isolated from the writer that made it unnecessary.

    ``sync_source`` now redacts before it records, which is the fix. This test
    does not go through it: it writes an audit row directly, the way any future
    caller of ``log_audit`` — which takes an open ``dict`` — will. ``/audit`` is
    VIEWER-gated, so the next person to drop a driver's exception in there
    would reopen the same escalation.
    """
    from laurelin.core.db import MetadataStore

    ws = Workspace.init(tmp_path / "ws", name="red")
    MetadataStore(ws.metadata_path).log_audit(
        "something_failed",
        {"error": "connect to postgresql://alice:hunter2@db.internal/prod failed",
         "client_secret": "SEKRET"},
        actor="root",
    )
    client = TestClient(create_app(ws))
    creds = {"username": "root", "password": "trustno1!"}
    client.post("/api/v1/auth/setup", json=creds)
    client.post("/api/v1/auth/login", json=creds)

    audit = client.get("/api/v1/audit")
    assert audit.status_code == 200
    assert "hunter2" not in audit.text and "SEKRET" not in audit.text
    row = next(e for e in audit.json() if e["action"] == "something_failed")
    # The trail still says what happened and under which key.
    assert "client_secret" in row["details"]
    assert "db.internal" in row["details"]["error"]
