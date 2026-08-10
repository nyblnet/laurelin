"""Secrets posture for an export: omit by key allowlist, never redact.

**Measured, on this tree, before this module existed.** All three production
redactors were attacked with nine inputs; eight leaks reproduced:

    input                                    federation  engines  connectors
    postgresql://alice:hunter2@db:5432/prod  ok          ok       ok
    ...:p@sswd@db...                         ok          LEAK     ok
    ...:p/w@db...                            LEAK        ok       LEAK
    postgresql://:hunter2@db...              LEAK        LEAK     ok
    https://api.../e.csv?api_key=SEKRET      LEAK        LEAK     LEAK
    Server=db;Uid=alice;Pwd=hunter2;         LEAK        LEAK     LEAK
    adbc...call_header.authorization: Bearer LEAK                 -
    {"auth": {"password": "SEKRET"}}         -           -        LEAK
    {"headers": {"X-Api-Key": "SEKRET"}}     -           -        LEAK

That is not a bug list, it is the shape of the approach. A denylist over
free-form values fails *invisibly*: every miss above produced output that looks
redacted. An allowlist over keys fails the other way — a future
``webhooks.signing_key`` defaults to absent, which surfaces at import as a
missing field somebody fixes.

This module therefore does **not** import those three redactors. Reusing a
best-effort API courtesy as a security control is the trap; they are a
different product with a different threat model, and §"why the export is
stricter" below explains why the two must be allowed to disagree.

**The export is stricter than the API on purpose.** The API keeps
``user@host:port/db`` and ``tests/test_connectors.py:57`` enforces that — right
for a response an admin reads on a live system they can already reach, wrong
for a file. ``postgresql://svc_laurelin@pg-prod-3.internal:5432/crm`` in an
emailed tarball is an internal network map plus a valid username. The export
withholds the entire endpoint: host, port, user, database and password. The
manifest still records the source's *name*, which the operator already knows,
so nothing about recognizability is lost.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from laurelin.export.manifest import Withheld

# The union of all three existing regexes, plus every key that identifies an
# endpoint. Endpoint keys are here because a hostname is the half of a DSN the
# API is allowed to keep and a file is not.
#
# This regex is a *denylist* and is deliberately no longer what guards the two
# open-vocabulary config columns — see NON_SECRET_SHAPE_KEYS. It survives
# because a denylist is still the right shape for audit_log.details_json, whose
# keys are first-party and enumerable, and because naming the obvious secrets
# keeps the withheld report readable.
SECRET_KEY_RE = re.compile(
    r"password|secret|token|key|credential|authorization|api_?key|"
    r"^url$|^uri$|^dsn$|^host$|^hostname$|^endpoint$|^account$|^user$|"
    r"^username$|^pwd$|^passphrase$|^private_key$|^auth$|^conn(ection)?_?str",
    re.I,
)

# Named separately so a reader can see the endpoint half at a glance; the regex
# above is what actually runs.
SECRET_KEYS = (
    "password", "secret", "token", "key", "credential", "authorization",
    "api_key", "apikey", "url", "uri", "dsn", "host", "hostname", "endpoint",
    "account", "user", "username", "pwd", "passphrase", "private_key", "auth",
)

# **The allowlist that actually guards datasets.source_json and
# sources.config_json.** Every other key is nulled, whatever it is called.
#
# Measured, against the denylist this replaced: a source configured through the
# real front door with `base_url`, `endpoint_url`, `hosts`,
# `bootstrap_servers`, `connection` and `path` shipped all six verbatim, each
# carrying a password, while the manifest positively certified that the row's
# endpoint had been withheld. `SourceUpsertRequest.config` is `dict[str, Any]`
# (source_routes.py:39), so the key vocabulary is the caller's, not ours — and
# a denylist over somebody else's vocabulary is a list of the names we happened
# to think of. `path` alone is required by three of federation's four source
# types (federation.py:64-84), and `_REMOTE_PREFIXES` blesses `s3://` and
# `https://`, which is exactly where a presigned signature lives.
#
# These names are here because they describe the *shape* of the registration —
# which table, in which format — and not where it lives or how to log in.
# Anything not on this list defaults to absent, which surfaces at import as a
# needs-credentials refusal somebody fixes.
NON_SECRET_SHAPE_KEYS = (
    "type", "table", "format", "catalog", "database", "schema", "namespace",
    "mode", "query", "cursor_column", "batch_size", "branch", "snapshot_id",
)

# How to put each withheld field back. Asserted by a test against the app's
# route table, so a renamed route breaks the checklist rather than the checklist
# quietly pointing nowhere.
RESUPPLY = {
    "sources": ("connector sync", "PUT /api/v1/sources/{name}  or  Admin -> Sources"),
    "engines": ("delegated query execution", "PUT /api/v1/engines/{name}  or  Admin -> Engines"),
    "datasets": ("scanning the table at its source",
                 "POST /api/v1/datasets/{name}/source  or  Datasets -> Register"),
    "users": ("password sign-in",
              "POST /api/v1/users  or  Admin -> Users (or re-federate via OIDC/SAML)"),
    "api_tokens": ("API authentication", "POST /api/v1/tokens  or  Admin -> Tokens"),
}


# Audit details are the one JSON column whose *content* is the product: an
# audit trail with its subjects nulled out is not an audit trail. So it cannot
# take the allowlist above — but it can take a denylist, because unlike a
# connector config its keys are first-party. Every `log_audit` call in the tree
# uses one of: username, name, slug, via, schedule, reason, workspace, targets,
# source, object_type, engine, dashboard, app, actor, dataset.
#
# Which is what decides the two disagreements with SECRET_KEY_RE:
#
# * The endpoint keys ARE nulled here now. No first-party call passes `url` or
#   `host`, so nulling them costs the trail nothing — and a row that did carry
#   one leaked in full before this line changed.
# * `user`/`username`/`account` are NOT nulled. In a connector config `user` is
#   half a DSN; in an audit row it is the subject, and removing it would delete
#   the record while protecting nothing the same row's `actor` already says.
#
# The free-text keys are nulled for the same reason `sources.last_sync_error`
# is dropped wholesale: `auth_routes.py:340` logs `{"reason": str(exc)}` from
# an OIDC failure, and a token-endpoint URL with a client_secret in it lands
# there verbatim. No redactor in this tree covers free-form driver text.
AUDIT_KEY_RE = re.compile(
    r"password|secret|token|key|credential|authorization|api_?key|"
    r"^url$|^uri$|^dsn$|^host$|^hostname$|^endpoint$|^conn(ection)?_?str|"
    r"^error$|^reason$|^message$|^details?$|^exception$|^traceback$|"
    r"^stderr$|^stdout$",
    re.I,
)


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(str(key)))


def _walk(value: Any, path: str, out: list[str], matcher=None) -> Any:
    """Null every secret-keyed leaf, recursively, keeping the key.

    Recursive because ``connectors.redacted_config`` walks only the top level
    plus ``headers`` — measured to pass ``{"auth": {"password": "SEKRET"}}``
    through verbatim. Keys survive so the operator can see the shape they have
    to re-fill; only values die.
    """
    matcher = matcher or SECRET_KEY_RE
    if isinstance(value, dict):
        result = {}
        for key, sub in value.items():
            here = f"{path}.{key}" if path else str(key)
            if matcher.search(str(key)):
                result[key] = None
                out.append(here)
            else:
                result[key] = _walk(sub, here, out, matcher)
        return result
    if isinstance(value, list):
        return [_walk(v, f"{path}[{i}]", out, matcher) for i, v in enumerate(value)]
    return value


def _allowlist(value: Any, out: list[str], allowed: tuple[str, ...]) -> Any:
    """Keep only allowlisted keys holding scalars. Null everything else.

    Scalars only, even for an allowlisted key: a nested object or array under
    ``table`` is not a table name, it is somewhere for an endpoint to hide, and
    the whole point of the allowlist is that it does not have to guess.
    """
    if not isinstance(value, dict):
        # A config that is not an object has no keys to allowlist, so there is
        # nothing here that can be judged. Withhold it whole.
        out.append("*")
        return None
    result: dict[str, Any] = {}
    for key, sub in value.items():
        if str(key).lower() in allowed and isinstance(sub, (str, int, float, bool, type(None))):
            result[key] = sub
        else:
            result[key] = None
            out.append(str(key))
    return result


def strip_json_column(
    table: str, row: str, column: str, raw: Any, *,
    null_every_value: bool = False, matcher=None,
    allowed: Optional[tuple[str, ...]] = None,
) -> tuple[Any, list[Withheld]]:
    """Strip a ``*_json`` column, returning the new text and what was withheld.

    ``null_every_value`` is for ``engines.options_json``: those options are
    handed straight to the driver as ``db_kwargs`` (engines.py:113) and the key
    ``adbc.flight.sql.rpc.call_header.authorization`` is measured to pass
    ``engines._SECRET_KEY_RE`` untouched. Key-name matching cannot be trusted on
    a namespace somebody else defines, so the whole value space goes.
    """
    if raw in (None, ""):
        return raw, []
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        # Unparseable JSON in a secret-bearing column: withhold it entirely
        # rather than ship bytes nothing inspected.
        return "{}", [withheld_field(table, row, column, "unparseable JSON in a secret column")]

    hits: list[str] = []
    if null_every_value and isinstance(parsed, dict):
        cleaned = {k: None for k in parsed}
        hits = list(parsed)
    elif allowed is not None:
        cleaned = _allowlist(parsed, hits, allowed)
    else:
        cleaned = _walk(parsed, "", hits, matcher)

    withheld = [withheld_field(table, row, f"{column}.{h}") for h in hits]
    # sort_keys=False: this is a re-serialization of a value we already
    # rewrote, so byte-faithfulness is gone either way; insertion order at
    # least keeps a diff readable.
    return json.dumps(cleaned), withheld


def withheld_field(table: str, row: str, field: str, reason: str = "credential") -> Withheld:
    required_for, resupply = RESUPPLY.get(table, ("", ""))
    return Withheld(
        table=table, row=row, field=field, reason=reason,
        required_for=required_for, resupply=resupply,
    )


def strip_secrets(
    table: str, row_key: str, column: str, value: Any
) -> tuple[Any, list[Withheld]]:
    """Apply the export's secret posture to one column of one row.

    Returns the value that travels and the positive record of what did not.
    Columns with no secret content return unchanged with an empty list, so a
    caller can pipe every column through this without special-casing.
    """
    if table == "engines" and column == "uri":
        if not value:
            return value, []
        # Empty string, not NULL: the column is NOT NULL, and an import that
        # cannot insert the row would lose the engine's *existence* as well as
        # its endpoint. The manifest says the endpoint was withheld; the row
        # says the engine was there.
        return "", [withheld_field(table, row_key, "uri")]
    if table == "engines" and column == "options_json":
        return strip_json_column(table, row_key, column, value, null_every_value=True)
    if table in ("sources", "datasets") and column in ("config_json", "source_json"):
        return strip_json_column(
            table, row_key, column, value, allowed=NON_SECRET_SHAPE_KEYS
        )
    if table == "audit_log" and column == "details_json":
        return _strip_audit_details(row_key, column, value)
    return value, []


def _strip_audit_details(row_key: str, column: str, value: Any) -> tuple[Any, list[Withheld]]:
    """Null the credential-shaped keys, then withhold the row if what is left
    still looks like a credential.

    Two mechanisms because neither alone is honest here. The key denylist keeps
    the trail readable, and it is safe precisely because audit keys are
    first-party. But `log_audit` takes `dict[str, Any]` and the details of a
    future action are not enumerable today, so a second, deliberately
    high-recall pass reads what is about to travel and withholds the whole
    object when it trips. Withholding rather than refusing, because unlike a
    pipeline file an audit row is machine-written history the operator cannot
    go and fix.
    """
    from laurelin.export.pipeline_scan import json_values, looks_like_a_credential

    cleaned, withheld = strip_json_column(
        "audit_log", row_key, column, value, matcher=AUDIT_KEY_RE
    )
    if cleaned and any(looks_like_a_credential(v) for v in json_values(cleaned)):
        return "{}", withheld + [withheld_field(
            "audit_log", row_key, column,
            "audit details still looked like a credential after key stripping",
        )]
    return cleaned, withheld
