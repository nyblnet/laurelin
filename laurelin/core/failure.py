"""A failure Laurelin wrote, in place of a failure a driver wrote.

**R1: third-party driver text is never persisted.** Where a driver or library
exception is caught, it is converted *at the catch site* into a
:class:`Failure` — an error class from a closed enum, a phase from a closed
enum, a subject in Laurelin's own namespace, and whatever bounded fields
Laurelin itself constructed. The driver's own prose goes to the server log and
nowhere else. **A credential cannot leak from a string we never stored.**

Why this module exists instead of a fourth matcher
--------------------------------------------------

Three adversarial rounds attacked ``core/redaction.py``'s free-text detection
and three rounds found criticals. Round 1: three redactors, three different
guesses about where a credential lives. Round 2: driver exceptions stored
verbatim on the ``sources/{name}/sync`` path reached a VIEWER via ``GET
/audit``. Round 3: the same leak on the scheduler path, which nobody had
checked, plus ``credential_in_free_text`` only understanding ``://`` — so libpq
conninfo, ODBC keyword strings and DuckDB ``CREATE SECRET`` bodies saved with
200 and a VIEWER read them off ``GET /dashboards``.

Each round widened a matcher and each round something walked around it, because
**finding a credential inside free text is not decidable**. libpq conninfo, ODBC
keyword strings, JDBC URLs, ``CREATE SECRET``, a driver's prose, and formats
nobody has enumerated yet are all valid places for a password to be, and no
regex closes that set.

So the question this module answers is not "does this string contain a
credential". It is "did Laurelin write this string". Every field below is one
of:

* a value from a **closed enum** that Laurelin defines (``code``, ``phase``,
  ``driver``);
* an f-string over a **Laurelin identifier** (``subject``, ``detail_ref``,
  ``at``);
* a ``host:port`` **rebuilt from Laurelin's own parse of Laurelin's own
  config** (``endpoint``) — userinfo is never in scope, which is strictly better
  than the tree's previous behaviour, where ``redaction.py`` documents having
  printed a *fabricated* hostname on the ambiguous-``@`` path;
* an ``int`` (``counters``), type-checked at construction, because an int cannot
  carry a password.

That leaves exactly two driver-derived fields, ``exc_class`` and
``vendor_code``, and **the gate on them is a shape constraint on a Python
identifier, not a credential matcher**. ``exc_class`` must match
``^[A-Za-z_][A-Za-z0-9_]{0,63}$``; ``vendor_code`` must match
``^[0-9A-Za-z]{1,10}$``. A libpq conninfo, an ODBC keyword string, a JDBC URL, a
``CREATE SECRET`` body and a PEM block all require at least one of ``:`` ``/``
``@`` ``=`` space ``-`` ``.`` — none of which those gates admit. **That is
decidable.** "Does this string contain a credential" is not. A value that fails
the gate is replaced with ``""``, never truncated: truncating a string you did
not understand is how you ship half a password.

Where the driver's real text goes, and who can read it
------------------------------------------------------

:meth:`Failure.from_exception` emits exactly one log record carrying the
exception and its traceback, tagged with ``detail_ref``. That is the only place
the driver's words exist.

**Who may read that is NOT bounded by any Laurelin role** — it is whoever can
read the server's log sink. ``SECURITY.md`` states this as a deployment
requirement. R1 *relocates* driver text from a browser-readable database column
to an operator-privilege artifact; it does not sanitize it. As a courtesy for a
sink Laurelin does not own, :meth:`Failure.log` substitutes out the secrets it
can name from the config in hand — but that is explicitly **not** a boundary,
and no test may assert that a bypass of it is a security failure.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
import socket
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, ClassVar, Optional

from pydantic import ConfigDict, field_validator

from laurelin.core.audience import Audience, AuthoredBy, Governed
from laurelin.core.roles import Role

log = logging.getLogger("laurelin.failure")


def _utcnow_iso() -> str:
    # Duplicated from `models.utcnow_iso` rather than imported: `models` imports
    # `Failure` for its own fields, so this module has to sit below it.
    return datetime.now(timezone.utc).isoformat()


class FailureCode(str, Enum):
    """Laurelin's vocabulary for why something failed. Closed, on purpose.

    Every member maps 1:1 to an action an operator would take — "rotate the
    credential" vs "open the firewall" vs "the table was dropped upstream" —
    which is what a driver's sentence was actually being read *for*.
    """

    CREDENTIAL_MALFORMED = "credential_malformed"
    ENDPOINT_UNRESOLVABLE = "endpoint_unresolvable"
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"
    ENDPOINT_TIMEOUT = "endpoint_timeout"
    AUTH_REJECTED = "auth_rejected"
    DATABASE_MISSING = "database_missing"
    PERMISSION_DENIED = "permission_denied"
    RELATION_MISSING = "relation_missing"
    COLUMN_MISSING = "column_missing"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    STATEMENT_INVALID = "statement_invalid"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    # A stored instruction — a dashboard panel, an object app's filters — no
    # longer matches the ontology or the data it was written against. First
    # party by construction: nothing outside Laurelin raises it.
    DEFINITION_STALE = "definition_stale"
    TRANSFORM_FAILED = "transform_failed"  # our own code raised
    EXPECTATION_FAILED = "expectation_failed"
    REMOTE_FAILED = "remote_failed"  # residual; read the log at detail_ref


class Phase(str, Enum):
    """Which step of an operation failed. Also closed, also ours."""

    parse = "parse"
    resolve = "resolve"
    connect = "connect"
    authenticate = "authenticate"
    select_db = "select_db"
    describe = "describe"
    execute = "execute"
    fetch = "fetch"
    write = "write"
    compile = "compile"
    plan = "plan"


# The drivers Laurelin itself calls. A `driver` outside this set is dropped to
# "" rather than stored: the field exists so an operator knows which library's
# log line to go read, and a value we did not put here is a value we did not
# construct.
DRIVERS = frozenset(
    {"", "psycopg", "mysql.connector", "duckdb", "chdb", "adbc_flightsql",
     "requests", "urllib", "pyarrow", "pyiceberg", "python"}
)

# Shape gates. Not credential matchers — see the module docstring. These admit a
# Python identifier and a short alphanumeric vendor code, and nothing else.
_EXC_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_VENDOR_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,10}$")
# Our own subjects: "source:crm_orders", "build_task:clean", "engine:trino".
_SUBJECT_RE = re.compile(r"^[a-z_]+:[A-Za-z0-9_.\-/]{0,128}$")
# host:port, rebuilt by us. Hostname or bracketed IPv6 literal.
_ENDPOINT_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._\-]+):[0-9]{1,5}$")


def _new_detail_ref() -> str:
    """A grep handle shared by the stored record and the log line."""
    return "err-" + hashlib.blake2s(uuid.uuid4().bytes, digest_size=6).hexdigest()



def safe_detail(
    exc: BaseException, *, phase: "Phase" = None, subject: str = "request"
) -> str:
    """An exception's message if Laurelin wrote it; a :class:`Failure` if not.

    The rule R1 states, as a one-liner a route can call. Most `except ValueError`
    blocks in the API are catching Laurelin's own validation and echoing the
    caller's own input back, which is fine — but several of them wrap a call
    that reaches a third-party library, and `pyarrow.lib.ArrowInvalid` **is** a
    `ValueError` while `ArrowKeyError` **is** a `KeyError`. One of those returned
    an operator's S3 warehouse credential to an editor as a 400 body.

    Using this everywhere costs nothing when the exception is ours and closes
    the class when it is not.
    """
    if is_first_party(exc):
        return first_party_message(exc)
    return Failure.from_exception(
        exc, phase=phase or Phase.execute, subject=subject
    ).render_brief()

# The directory Laurelin's own code lives in. Compared against the *filename of
# the frame that raised*, which is a fact about where a `raise` statement is
# written and cannot be spoofed by an exception's type or message.
_LAURELIN_ROOT = str(pathlib.Path(__file__).resolve().parent.parent) + os.sep


# Which library an exception came out of, keyed on the top-level package that
# defines its class. Closed, like `DRIVERS`: a module that is not here yields
# "" rather than a guess.
#
# `driver` exists so an operator knows whose log line to go read, and a closed
# set populated *wrongly* is worse than an empty one. Measured before this
# existed: `_driver_failure` hardcoded `driver="duckdb"`, so a ClickHouse
# registration that failed inside `chdb` was recorded as duckdb; and
# `_DRIVER_BY_TYPE` mapped the `http` connector to "requests" although
# `_pull_http` uses `urllib.request` and `requests` is never imported on that
# path. Both sent an operator to a log that does not exist.
_DRIVER_BY_MODULE = {
    "duckdb": "duckdb", "_duckdb": "duckdb",
    "chdb": "chdb",
    "psycopg": "psycopg",
    "mysql": "mysql.connector",
    "adbc_driver_flightsql": "adbc_flightsql", "adbc_driver_manager": "adbc_flightsql",
    "requests": "requests",
    "urllib": "urllib", "http": "urllib", "socket": "urllib",
    "pyiceberg": "pyiceberg",
    "pyarrow": "pyarrow",
}


def driver_of(exc: BaseException, default: str = "") -> str:
    """The library that defines this exception's class, as a `DRIVERS` value.

    A fact about a type's defining module — decidable, and first-party in the
    sense that matters: Laurelin decides the mapping, and an unmapped module
    yields ``default`` rather than a plausible-looking wrong answer.
    """
    root = (type(exc).__module__ or "").split(".")[0]
    return _DRIVER_BY_MODULE.get(root, default)


def first_party_message(exc: BaseException) -> str:
    """The message of an exception Laurelin raised. Check :func:`is_first_party`
    before calling this — it does no checking of its own."""
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)


def is_first_party(exc: BaseException) -> bool:
    """Did Laurelin's own code raise this, or did a library?

    R1 says a driver's prose is never returned or persisted, and catch sites
    honour that one at a time — but ``laurelin/api/app.py`` installs
    ``@app.exception_handler(ValueError)`` and ``(KeyError)``, which turn **any**
    uncaught exception of those types, from anywhere in the dependency tree,
    into a response body carrying that library's raw words. That is a standing
    bypass of R1 for every library Laurelin has not wrapped, and it was a
    confirmed disclosure: ``pyarrow.lib.ArrowInvalid`` subclasses ``ValueError``,
    so an EDITOR uploading a CSV to an Iceberg dataset read the operator's S3
    warehouse credential —

        {"detail": "Not a valid bucket name: 'AKIAICESENT:ICESENTINELKEY@icebucket'"}

    — out of a 400. ``pyarrow.lib.ArrowKeyError`` subclasses ``KeyError`` and
    does the same at 404.

    **Decided on the deepest traceback frame**, which is where the exception was
    actually raised. Not on the type: a third-party library can raise a bare
    ``ValueError`` and a first-party class can be defined anywhere. Not on
    ``__module__``: that is a property of the class, not of the raise. Frames
    are appended at the *head* as an exception propagates, so the deepest frame
    stays the original ``raise`` site even after a re-raise.

    A ``raise ValueError(str(exc))`` inside Laurelin does defeat this — the
    deepest frame is then ours. That is the pattern
    ``tests/test_ci_guards.py`` already forbids by AST, which is the right place
    for it: this function answers "who raised it", and that one answers "who
    wrote the message".
    """
    tb = exc.__traceback__
    if tb is None:
        # Never raised (or the traceback was stripped). Nothing vouches for it.
        return False
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame.f_code.co_filename.startswith(_LAURELIN_ROOT)


class Failure(Governed):
    """A structured, Laurelin-authored failure. Frozen; safe by construction.

    Serialized under :mod:`laurelin.core.serialize`, so a reader below
    ``editor`` receives only ``code`` and ``subject`` — enough to learn *that*
    their data is stale and *why in one word*, and nothing else.
    """

    model_config = ConfigDict(frozen=True)

    # An editor authors the builds, sources and schedules that fail, so an
    # editor is the level that may read the whole record. Viewers get the
    # PRESENTATION pair below.
    laurelin_author_role: ClassVar[Role] = Role.editor

    code: Annotated[FailureCode, Audience.PRESENTATION]
    # OUR namespace: "source:crm_orders", "build_task:clean", "engine:trino".
    subject: Annotated[str, Audience.PRESENTATION] = ""

    phase: Phase = Phase.execute
    # "host:port" from OUR parse of OUR config. Never userinfo — but the config
    # it is rebuilt from is ADMIN-authored, and this record is editor-authored,
    # so the field declares the higher level. Measured before this annotation
    # existed: an editor reading `GET /sources/{name}` — a route whose whole
    # point is that `config` is admin-only — got `secret-db.internal.corp:55999`
    # back inside `last_sync_failure`, because recursion into a nested model
    # widened instead of narrowing.
    endpoint: Annotated[str, AuthoredBy(Role.admin)] = ""
    driver: str = ""
    exc_class: str = ""  # type(exc).__name__ only
    vendor_code: str = ""  # exc.sqlstate / str(exc.errno)
    counters: dict[str, int] = {}
    detail_ref: str = ""  # "err-<12 hex>", also emitted on the log line
    at: str = ""

    # -- construction-time gates ------------------------------------------------
    #
    # Each of these is a *shape* rule over a value Laurelin either chose or is
    # about to store. A value that does not fit the shape is dropped, never
    # truncated: half of a string you did not understand is still half of
    # whatever was in it.

    @field_validator("subject")
    @classmethod
    def _check_subject(cls, v: str) -> str:
        return v if (v == "" or _SUBJECT_RE.match(v)) else ""

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return v if (v == "" or _ENDPOINT_RE.match(v)) else ""

    @field_validator("driver")
    @classmethod
    def _check_driver(cls, v: str) -> str:
        if v not in DRIVERS:
            raise ValueError(
                f"Unknown driver {v!r}: add it to failure.DRIVERS if Laurelin "
                "really calls it. The field names a library, not a message."
            )
        return v

    @field_validator("exc_class")
    @classmethod
    def _check_exc_class(cls, v: str) -> str:
        return v if _EXC_CLASS_RE.match(v) else ""

    @field_validator("vendor_code")
    @classmethod
    def _check_vendor_code(cls, v: str) -> str:
        return v if _VENDOR_CODE_RE.match(v) else ""

    @field_validator("counters")
    @classmethod
    def _check_counters(cls, v: dict) -> dict[str, int]:
        for key, value in v.items():
            # `bool` is an `int` in Python and would serialize as true/false;
            # that is fine. Anything else is a string somebody smuggled in.
            if not isinstance(value, int):
                raise TypeError(
                    f"counters[{key!r}] is {type(value).__name__}, not int. "
                    "Counters are integers so a limit message cannot carry a "
                    "substring of a driver's sentence."
                )
        return v

    # -- rendering --------------------------------------------------------------

    def render(self) -> str:
        """The operator-facing sentence, from a Laurelin-owned template table.

        Interpolates only :class:`Failure` fields, every one of which is gated
        above. There is no path by which a driver's words reach this string.
        """
        where = f" at {self.endpoint}" if self.endpoint else ""
        what = f" for {self.subject}" if self.subject else ""
        tail = []
        if self.driver:
            tail.append(self.driver)
        if self.exc_class:
            tail.append(self.exc_class)
        if self.vendor_code:
            tail.append(self.vendor_code)
        if self.detail_ref:
            tail.append(f"ref {self.detail_ref}")
        suffix = f" ({'/'.join(tail)})" if tail else ""
        body = _TEMPLATES.get(self.code, "The operation failed{what}{where}.")
        return body.format(what=what, where=where) + suffix

    def render_brief(self) -> str:
        """The same sentence with ``endpoint`` removed, for a reader below the
        level that authored the configuration.

        ``endpoint`` is the one field of a :class:`Failure` whose *value* comes
        from somebody's config rather than from a closed set: ``code``,
        ``phase`` and ``driver`` are enums, ``counters`` are ints, ``subject``
        and ``detail_ref`` are ours, and ``exc_class``/``vendor_code`` are shape
        -gated. So there is exactly one thing to drop, and dropping it is what
        lets the same sentence be shown to an editor who triggered a sync of an
        admin's connector. They still learn which subject failed, in which
        phase, with which code, and the ``detail_ref`` that finds the whole
        story in the operator's log.
        """
        what = f" for {self.subject}" if self.subject else ""
        tail = f" (ref {self.detail_ref})" if self.detail_ref else ""
        body = _TEMPLATES.get(self.code, "The operation failed{what}{where}.")
        return body.format(what=what, where="") + tail

    def viewer_projection(self) -> dict[str, str]:
        """What a reader below ``editor`` learns: their data is stale, and why
        in one word. Kept in step with the PRESENTATION annotations above."""
        return {"code": self.code.value, "subject": self.subject}

    def audit_projection(self) -> dict[str, str]:
        """A failure as an audit row's ``details`` may carry it.

        ``details`` is an open ``dict``, so :func:`serialize.dump` cannot reach
        inside it — a nested model there is a blob, not a record, and no
        annotation applies. That is precisely how an editor came to read an
        admin's ``endpoint`` off ``GET /audit``: the writer declared
        ``min_read_role=Role.editor`` and then put a whole ``Failure`` in the
        bag. So the *writer* projects, at the call site, and what goes in the
        bag is three fields that are safe by construction rather than by
        annotation.
        """
        return {
            "code": self.code.value,
            "subject": self.subject,
            "detail_ref": self.detail_ref,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json") | {"message": self.render()}

    # -- classification ---------------------------------------------------------

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        phase: Phase = Phase.execute,
        subject: str = "",
        driver: str = "",
        code: Optional[FailureCode] = None,
        endpoint: str = "",
        dsn: str = "",
        counters: Optional[dict[str, int]] = None,
        config: Any = None,
        preflight: bool = False,
    ) -> "Failure":
        """Convert a driver exception into a Laurelin failure, and log the original.

        ``code`` overrides classification (used where the catch site already
        knows more than a mapping table can). ``dsn``/``config`` are Laurelin's
        own configuration for the thing that failed, used to rebuild ``endpoint``
        and, on the log path only, to scrub known secrets out of the courtesy
        copy — see the module docstring on why that is not a boundary.
        """
        ref = _new_detail_ref()
        vendor = _vendor_code(exc)
        host_port = endpoint
        if not host_port and dsn:
            parsed = parse_endpoint(dsn)
            if parsed is not None:
                host_port = f"{parsed[0]}:{parsed[1]}"

        if code is None:
            code = _classify(exc, driver, vendor)
        if code is None and preflight and dsn:
            code, phase, host_port = probe_endpoint(dsn, fallback_endpoint=host_port)
        if code is None:
            code = FailureCode.REMOTE_FAILED

        failure = cls(
            code=code,
            phase=phase,
            subject=subject,
            endpoint=host_port or "",
            driver=driver if driver in DRIVERS else "",
            exc_class=type(exc).__name__,
            vendor_code=vendor,
            counters=dict(counters or {}),
            detail_ref=ref,
            at=_utcnow_iso(),
        )
        failure.log(exc, config=config)
        return failure

    def log(self, exc: Optional[BaseException] = None, config: Any = None) -> None:
        """Emit the one log record that carries the driver's real words.

        This is the only place the exception text exists after R1. The record
        is tagged with ``detail_ref`` so an operator finds the traceback behind
        any stored failure in one grep.
        """
        from laurelin.core import authoring_hints
        from laurelin.core.redaction import MASK

        text = ""
        if exc is not None:
            text = f"{type(exc).__name__}: {exc}"
            # Substitution only — deliberately NOT `redact_driver_text`, whose
            # withhold branch replaces the whole message with "***** (withheld)"
            # and leaves the operator with no diagnostic at all. That trade is
            # right for a browser and wrong here: this is the *log*, the one
            # place R1 leaves the driver's words on purpose, and `exc_info`
            # below carries the untouched traceback regardless — so withholding
            # the summary line would destroy the readable half and protect
            # nothing.
            #
            # Best effort, and explicitly NOT a boundary: the log sink is an
            # operator-privilege artifact either way (SECURITY.md). This exists
            # only so a config whose secrets Laurelin *does* hold is not
            # gratuitously copied into a SIEM. No test may assert that a bypass
            # of it is a security failure.
            for secret in authoring_hints.secrets_in_config(config):
                if len(secret) >= 3:
                    text = text.replace(secret, MASK)
        log.warning(
            "failure %s %s %s: %s",
            self.code.value, self.subject, self.detail_ref, text,
            exc_info=exc,
            extra={"detail_ref": self.detail_ref, "code": self.code.value,
                   "subject": self.subject, "endpoint": self.endpoint},
        )


# One sentence per code, owned by Laurelin. `{what}` and `{where}` are the only
# substitutions and both are gated fields.
_TEMPLATES: dict[FailureCode, str] = {
    FailureCode.CREDENTIAL_MALFORMED:
        "The connection string{what} could not be parsed, so nothing was sent to "
        "the remote system.",
    FailureCode.ENDPOINT_UNRESOLVABLE:
        "The host{what} could not be resolved{where}.",
    FailureCode.ENDPOINT_UNREACHABLE:
        "Nothing accepted a connection{where}{what}.",
    FailureCode.ENDPOINT_TIMEOUT:
        "The connection{where}{what} timed out.",
    FailureCode.AUTH_REJECTED:
        "Authentication was rejected{where}{what}; the credential may need "
        "rotating, or the database name may be wrong — see the log at the ref below.",
    FailureCode.DATABASE_MISSING:
        "The database named in the connection{what} does not exist{where}.",
    FailureCode.PERMISSION_DENIED:
        "The remote system refused the operation{what} for lack of privilege.",
    FailureCode.RELATION_MISSING:
        "The table{what} does not exist on the remote system{where}.",
    FailureCode.COLUMN_MISSING:
        "A column referenced{what} does not exist on the remote system.",
    FailureCode.SCHEMA_INCOMPATIBLE:
        "The remote schema{what} does not match what Laurelin expected.",
    FailureCode.DEFINITION_STALE:
        "The saved definition{what} refers to something that no longer exists; "
        "whoever can edit it can see which.",
    FailureCode.STATEMENT_INVALID:
        "The statement{what} was rejected as invalid by the remote system.",
    FailureCode.RESOURCE_EXHAUSTED:
        "The remote system ran out of budget{what} — narrow the query with a "
        "filter, an aggregate, or a smaller LIMIT.",
    FailureCode.TRANSFORM_FAILED:
        "Laurelin's own code raised while running{what}.",
    FailureCode.EXPECTATION_FAILED:
        "A declared expectation failed{what}.",
    FailureCode.REMOTE_FAILED:
        "The remote system failed{what}{where}; the details are in the server "
        "log at the ref below.",
}


# ---------------------------------------------------------------------------
# Mapping tables. One dict per driver, each a closed literal, every entry
# measured against a live server on this tree.
# ---------------------------------------------------------------------------

_PSYCOPG_SQLSTATE: dict[str, FailureCode] = {
    "42P01": FailureCode.RELATION_MISSING,
    "42703": FailureCode.COLUMN_MISSING,
    "42601": FailureCode.STATEMENT_INVALID,
    "42501": FailureCode.PERMISSION_DENIED,
    "57014": FailureCode.RESOURCE_EXHAUSTED,
    "3D000": FailureCode.DATABASE_MISSING,
    "28P01": FailureCode.AUTH_REJECTED,
}

# Measured against live StarRocks 9030 with mysql-connector 26.7.0. Two
# corrections to what was inherited: bad host is errno **2005**, not 2003 with a
# different exception class, and both arrive as a plain `DatabaseError` — the
# exception class does not discriminate.
_MYSQL_ERRNO: dict[int, FailureCode] = {
    1045: FailureCode.AUTH_REJECTED,
    2003: FailureCode.ENDPOINT_UNREACHABLE,
    2005: FailureCode.ENDPOINT_UNRESOLVABLE,
    5501: FailureCode.DATABASE_MISSING,
    5502: FailureCode.RELATION_MISSING,
    # 1064 is DELIBERATELY ABSENT. Measured, it is simultaneously syntax error,
    # unresolvable column *and* the memory-limit error — exactly the catch-all
    # `limits.starrocks_guard` already documents. A mapping that claims 1064
    # means "syntax" would be confidently wrong a third of the time.
}

_DUCKDB_CLASS: dict[str, FailureCode] = {
    "CatalogException": FailureCode.RELATION_MISSING,
    "ParserException": FailureCode.STATEMENT_INVALID,
    "BinderException": FailureCode.COLUMN_MISSING,
    "InvalidInputException": FailureCode.RELATION_MISSING,
    "OutOfMemoryException": FailureCode.RESOURCE_EXHAUSTED,
    "PermissionException": FailureCode.PERMISSION_DENIED,
    "ConversionException": FailureCode.SCHEMA_INCOMPATIBLE,
}


def _vendor_code(exc: BaseException) -> str:
    """The driver's own code for this failure, if it gave one.

    Gated by ``_VENDOR_CODE_RE`` on the way into the model, so a driver that
    puts prose in ``sqlstate`` contributes nothing rather than a fragment.
    """
    for attr in ("sqlstate", "errno", "pgcode"):
        value = getattr(exc, attr, None)
        if value is None:
            continue
        text = str(value)
        if _VENDOR_CODE_RE.match(text):
            return text
    return ""


def _classify(exc: BaseException, driver: str, vendor: str) -> Optional[FailureCode]:
    """Map a driver exception onto Laurelin's vocabulary, or None if unmapped.

    Unmapped is a normal outcome: the caller turns it into ``REMOTE_FAILED``
    plus a ``detail_ref``, which is safe and still actionable. Guessing would
    not be.
    """
    name = type(exc).__name__
    if driver == "psycopg":
        return _PSYCOPG_SQLSTATE.get(vendor)
    if driver == "mysql.connector":
        errno = getattr(exc, "errno", None)
        return _MYSQL_ERRNO.get(errno) if isinstance(errno, int) else None
    if driver == "duckdb":
        return _DUCKDB_CLASS.get(name)
    return None


# ---------------------------------------------------------------------------
# The connect-phase pre-flight
# ---------------------------------------------------------------------------

_DSN_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://")
_HOST_PORT_RE = re.compile(r"^(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._\-]+)(?::(?P<port>[0-9]+))?$")

_DEFAULT_PORTS = {
    "postgresql": 5432, "postgres": 5432, "mysql": 3306, "starrocks": 9030,
    "clickhouse": 8123, "grpc": 443, "grpc+tls": 443, "http": 80, "https": 443,
}


def parse_endpoint(dsn: str) -> Optional[tuple[str, int]]:
    """``(host, port)`` from Laurelin's own parse of a DSN, or None.

    The generalization of ``core/starrocks.parse_url``. It exists so
    ``Failure.endpoint`` is built from *our* reading of *our* config rather than
    scraped out of a driver's sentence, and so userinfo is never in scope.

    **Written by hand rather than as one regex, and not with
    ``urllib.parse.urlsplit``, for the reason ``core/redaction.py`` measured.**
    RFC 3986 says the authority ends at the first ``/`` — and real passwords
    contain ``/``, ``@`` and ``:``. A naive pattern reading
    ``postgresql://alice:pa/ss@db/prod`` returns the host ``alice``, and one
    reading ``postgresql://alice:pa@ss@db/prod`` returns ``ss``. Both are
    *fabricated* hostnames: not a masked value, a wrong one, presented as
    fact.

    So the rules here are the ones that module argues for:

    * the authority is what follows the **last** ``@``, because a password may
      contain earlier ones;
    * if the candidate userinfo contains ``/``, ``?`` or ``#``, the ``@`` may
      belong to a *path* (``https://h/exports/a@b.csv``) and this **refuses**
      rather than guessing — the caller turns None into
      ``CREDENTIAL_MALFORMED``, which is honest;
    * the result must parse as ``host[:port]`` or it is refused.

    Returning None costs a diagnostic. Returning a wrong host costs the
    operator's trust in every host this product prints.
    """
    text = str(dsn).strip()
    scheme_match = _DSN_SCHEME_RE.match(text)
    if not scheme_match:
        return None
    rest = text[scheme_match.end():]
    at = rest.rfind("@")
    if at != -1:
        userinfo = rest[:at]
        if any(ch in userinfo for ch in "/?#"):
            return None  # ambiguous: this '@' may sit inside a path
        rest = rest[at + 1:]
    cut = min((i for i in (rest.find(c) for c in "/?#") if i != -1), default=len(rest))
    host_port = _HOST_PORT_RE.match(rest[:cut])
    if not host_port:
        return None
    scheme = scheme_match.group("scheme").lower()
    port = host_port.group("port")
    return host_port.group("host"), int(port) if port else _DEFAULT_PORTS.get(scheme, 0)


def probe_endpoint(
    dsn: str, timeout: float = 3.0, fallback_endpoint: str = ""
) -> tuple[FailureCode, Phase, str]:
    """Classify a connect-phase failure by *making the calls ourselves*.

    Measured with psycopg 3.3.4 against live Postgres: ``sqlstate`` is ``None``
    on **every** connect failure — wrong password, a space in the password, an
    unknown database, a bad host, a refused port. All four are also
    ``OperationalError`` except the space-in-password case, which is
    ``ProgrammingError``. So on the connect path the driver offers no structured
    field at all, and classification has to be owned by Laurelin.

    Every exception caught below is raised by the **stdlib**, in response to a
    call **Laurelin** made. The classification is a first-party fact rather than
    a reading of somebody else's prose.

    Runs on the failure path only — one DNS lookup and one TCP connect, after
    something has already gone wrong.

    **No default-database retry probe**, deliberately. Reconnecting to the
    server's default database would separate DATABASE_MISSING from
    AUTH_REJECTED, at the cost of making a second authentication attempt with
    the operator's credential as a side effect of a *diagnostic* — which can
    trip account lockout and pollute the remote's auth log. On this path an
    unknown database is reported as AUTH_REJECTED, and that code's template says
    so.
    """
    parsed = parse_endpoint(dsn)
    if parsed is None:
        # The DSN never parsed, so the driver was never handed a string it could
        # quote back. This alone retires the space-in-password case that
        # defeated all three previous rounds.
        return FailureCode.CREDENTIAL_MALFORMED, Phase.parse, fallback_endpoint
    host, port = parsed
    endpoint = f"{host}:{port}"
    bare = host[1:-1] if host.startswith("[") else host
    try:
        socket.getaddrinfo(bare, port or None)
    except socket.gaierror:
        return FailureCode.ENDPOINT_UNRESOLVABLE, Phase.resolve, endpoint
    except Exception:  # noqa: BLE001 - any resolver failure is still a resolve failure
        return FailureCode.ENDPOINT_UNRESOLVABLE, Phase.resolve, endpoint
    try:
        socket.create_connection((bare, port), timeout).close()
    except (TimeoutError, socket.timeout):
        return FailureCode.ENDPOINT_TIMEOUT, Phase.connect, endpoint
    except ConnectionRefusedError:
        return FailureCode.ENDPOINT_UNREACHABLE, Phase.connect, endpoint
    except OSError:
        return FailureCode.ENDPOINT_UNREACHABLE, Phase.connect, endpoint
    # DNS resolved and TCP completed, so whatever failed happened after the
    # socket was up: the remote rejected us.
    return FailureCode.AUTH_REJECTED, Phase.authenticate, endpoint


def connect_failure(
    exc: BaseException,
    *,
    subject: str,
    driver: str,
    dsn: str,
    config: Any = None,
) -> Failure:
    """A connect-phase failure, classified by pre-flight when the driver can't.

    The driver's code is consulted first — mysql-connector *does* give a usable
    errno on connect (2003/2005/1045, measured) — and the pre-flight runs only
    when it gives nothing, which is every psycopg connect failure.
    """
    vendor = _vendor_code(exc)
    code = _classify(exc, driver, vendor)
    parsed = parse_endpoint(dsn)
    endpoint = f"{parsed[0]}:{parsed[1]}" if parsed else ""
    # The driver knew: it handed us a code from its own vocabulary, which means
    # the connection was up and a *statement* failed. Do not probe.
    phase = Phase.execute
    if code is None:
        code, phase, endpoint = probe_endpoint(dsn, fallback_endpoint=endpoint)
    return Failure.from_exception(
        exc, phase=phase, subject=subject, driver=driver, code=code,
        endpoint=endpoint, config=config,
    )
