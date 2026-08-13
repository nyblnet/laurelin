"""A guess about whether an author just pasted a credential. **Not a boundary.**

This module is where ``credential_in_free_text``, ``redact_text``,
``redact_driver_text``, ``secrets_in_config`` and their helpers went when they
stopped protecting anything, and the move *is* the point: it makes the
demotion greppable, and it makes the guard trivial.

Read this first
---------------

**Nothing's confidentiality depends on anything in this file.** Three
adversarial rounds attacked this code and found criticals in all three, because
the question it tries to answer — "does this string contain a credential" — is
not decidable. libpq conninfo, ODBC keyword strings, JDBC URLs, DuckDB
``CREATE SECRET``, a driver's prose, and formats nobody has enumerated are all
valid places for a password to be. Each round widened a matcher; each round
something walked around it.

What replaced it:

============================================  =================================
Boundary that used to rest on the matcher     What it rests on now
============================================  =================================
Dashboard panel SQL → viewer                  ``Audience.OPERATIONAL`` in
                                              ``core/serialize.dump``; the
                                              field is not in the response
``/audit`` details → viewer                   ``audit_log.min_read_role``,
                                              default admin
``sources.last_sync_error`` → editor          ``core/failure.Failure``; there
                                              is no free text to scan
``schedules.last_error`` → editor             ``Failure``
``/engines/{n}/test`` detail → admin          ``Failure``
``/query`` 400 body → viewer                  ``Failure.render()``; the SQL is
                                              never echoed
``/builds`` task error → viewer               ``Failure`` +
                                              ``viewer_projection()``
============================================  =================================

``tests/test_redaction.py::test_the_authoring_hint_is_not_load_bearing``
monkeypatches :func:`credential_in_free_text` to ``lambda v: False`` and
:func:`redact_text` to the identity, and re-runs the whole leak battery. It
passes. If it ever stops passing, the design is broken, not the matcher.

What this module is still for
-----------------------------

Two things, both explicitly non-load-bearing:

1. **An authoring hint.** ``PUT /dashboards/{name}`` and
   ``PUT /schedules/{name}`` run :func:`credential_in_free_text` and, on a hit,
   save with **200** and attach a ``warnings`` entry the UI shows as a yellow
   banner. It used to be a 400. Once being wrong costs an editor a banner
   instead of a viewer a password, a false negative is a UX miss rather than a
   vulnerability — and being *blocking* would keep an undecidable test on the
   critical path of a legitimate save.

2. **A courtesy on the log path.** ``Failure.log`` scrubs the secrets it can
   name out of the copy it writes to the server log. The log sink is an
   operator-privilege artifact either way (see ``SECURITY.md``); this exists so
   a config whose secrets Laurelin *does* hold is not gratuitously copied into
   a SIEM. **No test may assert that a bypass of this is a security failure.**

Permitted callers, exhaustively: ``api/routes.py::upsert_dashboard``,
``api/schedule_routes.py::upsert_schedule``, and ``core/failure.py`` on the log
path. ``tests/test_audience.py`` asserts by AST that the list stays that short.
"""

from __future__ import annotations

import re
from typing import Any

from laurelin.core.redaction import (
    _EMBEDDED_URL_RE,
    _HOST_RE,
    _SCHEME_RE,
    API_SECRET_KEY_RE,
    MASK,
    WITHHELD,
    _free_form_is_truncated,
    _split_query,
    keyword_credential,
    redact_dsn,
)

_MIN_SCRUB = 3


def redact_text(text: Any) -> Any:
    """Redact free text that may quote a DSN — a driver's exception message.

    Two passes, because the two halves have different confidence levels. Every
    ``scheme://`` run is a shape this module can parse, so it is redacted in
    place and the surrounding sentence survives. What is left is prose written
    by somebody else's driver, where a credential has no shape at all: if it
    trips the export's high-recall scanner, the whole message goes.

    That scanner is reused rather than re-derived — it is the one in this tree
    that has been attacked (``export/pipeline_scan.py``), and its patterns match
    the *word*, not the value, precisely so a miss is unlikely. It is evaluated
    only on the non-URL remainder: run over the whole string it would trip on
    the ``postgresql://`` this function just finished masking and withhold a
    message that is already safe.

    The cost is real and is not hidden: ``password authentication failed for
    user "alice"`` is withheld whole. An operator who needs that text reads the
    server log, where the exception is logged unredacted at warning level; the
    response is the copy that reaches a browser.
    """
    if not isinstance(text, str) or text == "":
        return text
    from laurelin.export.pipeline_scan import looks_like_a_credential

    if _free_form_is_truncated(text):
        # First, because the truncation defeats the scanner below as well:
        # stripping the cut-short match also strips the `://` that
        # `looks_like_a_credential`'s url_userinfo and dsn patterns key on, so
        # neither branch fired and the message went out whole.
        return WITHHELD
    remainder = _EMBEDDED_URL_RE.sub(" ", text)
    if looks_like_a_credential(remainder):
        return WITHHELD
    return _EMBEDDED_URL_RE.sub(lambda m: str(redact_dsn(m.group(0))), text)


def secrets_in_config(config: Any) -> list[str]:
    """Every credential value `config` handed to a driver, longest first.

    This exists because **no redactor can find a credential in a third party's
    prose** — but we do not have to find it, we issued it. Measured: psycopg
    rejects a password containing a space with ``unexpected spaces found in
    "SUPER SEKRET"``, a sentence with no ``password``, no ``://`` and no shape
    of any kind; :func:`redact_text` reads it as innocent prose and every
    pattern in ``export/pipeline_scan.py`` agrees. What defeats it is knowing
    that ``SUPER SEKRET`` is this source's password, which the config says.

    Collected: values under a secret-sounding key, both halves of any
    ``userinfo`` (the "username" of an ``s3://`` URL is an access key id), and
    every query-parameter value of every URL — ``?api_key=...`` is where the
    http connector's credential lives, and chdb rewrote ``s3://`` to ``s3:/``
    in its error text, which is enough to hide the URL from every shape rule
    here.

    Longest first so that scrubbing a password never leaves a shorter token
    that is a prefix of it sitting unscrubbed inside the mask.
    """
    found: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            found.add(value)

    def from_url(value: str) -> None:
        match = _SCHEME_RE.match(value)
        if match is None:
            return
        rest = value[match.end():]
        authority = rest.split("?", 1)[0].split("#", 1)[0]
        at = authority.rfind("@")
        if at > -1:
            for half in authority[:at].split(":"):
                add(half)
            add(authority[:at])
        if "?" in rest or "#" in rest:
            query = rest.split("?", 1)[-1]
            add(query)
            for pair in re.split(r"[&;#]", query):
                add(pair.split("=", 1)[-1] if "=" in pair else pair)

    def walk(value: Any, secret_key: bool) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                walk(sub, bool(API_SECRET_KEY_RE.search(str(key))))
        elif isinstance(value, (list, tuple)):
            for sub in value:
                walk(sub, secret_key)
        elif isinstance(value, str):
            if secret_key:
                add(value)
            if "://" in value:
                from_url(value)

    walk(config, False)
    return sorted(found, key=len, reverse=True)


def redact_driver_text(text: Any, secrets: Any = None) -> Any:
    """A third-party driver's message, on its way into a response body.

    Three passes, weakest assumption last:

    1. **Substitute what we issued.** Every string in `secrets` is replaced
       wherever it appears. This is the only pass that can catch a credential
       a driver quoted as bare prose.
    2. **Redact what has a shape.** :func:`redact_text` masks embedded DSNs and
       withholds the message whole if the remaining prose still trips the
       export scanner.
    3. **Verify.** If any known secret survived both — the driver re-encoded
       it, or it was too short to substitute safely — the message is withheld
       rather than shipped. A credential we know we gave out and can still see
       in the text is not a message we may return.

    The unredacted exception belongs in the server log, where the operator can
    reach it and a browser cannot. Callers log it before calling this.
    """
    if not isinstance(text, str) or text == "":
        return text
    tokens = [s for s in (secrets or []) if isinstance(s, str) and s]
    out = text
    for token in sorted(tokens, key=len, reverse=True):
        if len(token) >= _MIN_SCRUB:
            out = out.replace(token, MASK)
    out = redact_text(out)
    if out is WITHHELD or not isinstance(out, str):
        return WITHHELD
    if any(token in out for token in tokens):
        return WITHHELD
    return out


def credential_in_free_text(value: Any) -> bool:
    """Whether `value` carries a connection credential this module can point at.

    Deliberately *not* ``pipeline_scan.looks_like_a_credential``: that one
    matches the word, which is right for a warning an operator reads and wrong
    for a gate that returns 400 — it would reject a panel selecting a column
    named ``password_hash``.

    **Also deliberately not ``value != redact_value(value)``**, which is what
    this was, and which asked the wrong question in both directions.

    * It said *no* to every credential format that is not a URL, because
      ``redact_value`` only acts on ``://`` strings. A libpq conninfo in a
      dashboard panel's ``ATTACH`` passed the gate and a VIEWER read the
      password — the exact leak this gate exists to stop, one syntax over.
    * It said *yes* to every value ``redact_dsn`` withholds *because it is
      ambiguous*, which is not the same claim as "a credential is in here". A
      public CSV with ``?format=csv`` and an S3 prefix keyed by an email
      address were both refused with a 400 saying they embed a credential,
      with no override and no way to author the panel at all.

    So this asks its own question, and it has three answers:

    * a ``keyword=value`` connection string (:func:`keyword_credential`);
    * a URL whose authority holds a userinfo — including the ``/``-in-password
      case, where the ``@`` falls after the first ``/`` and the first segment
      is therefore not a hostname;
    * a URL whose query names a parameter from :data:`API_SECRET_KEY_RE`, or
      one truncated in a way that hides a userinfo.

    An ``@`` after a first segment that *does* parse as ``host[:port]`` is a
    path, not a credential: ``s3://reports/exports/alice@example.com/x.parquet``
    is authorable, and ``postgresql://alice:pa/ss@db/prod`` is not.
    """
    if not isinstance(value, str) or value == "":
        return False
    if keyword_credential(value):
        return True
    if _free_form_is_truncated(value):
        return True
    for match in _EMBEDDED_URL_RE.finditer(value):
        scheme = _SCHEME_RE.match(match.group(0))
        if scheme is None:  # pragma: no cover - the regex guarantees one
            continue
        rest = match.group(0)[scheme.end():]
        head, _, query = _split_query(rest)
        first = head.split("/", 1)[0]
        if "@" in first:
            return True
        if "@" in head and not _HOST_RE.fullmatch(first):
            return True
        for pair in re.split(r"[&;#]", query):
            if pair and API_SECRET_KEY_RE.search(pair.split("=", 1)[0]):
                return True
    return False
