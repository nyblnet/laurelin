"""One redactor for every credential-bearing value an API response carries.

**Measured, on this tree, before this module existed.** The three production
redactors were attacked with the inputs below; ten leaks reproduced
(``scratchpad/repro.py`` output, reproduced here verbatim because the numbers
are the argument):

    input                                    federation  engines  connectors
    postgresql://alice:pa/ss@db:5432/prod    LEAK        ok       LEAK
    postgresql://:hunter2@db:5432/prod       LEAK        LEAK     ok
    https://api.x.com/e.csv?api_key=SEKRET   LEAK        LEAK     LEAK
    Server=db;Uid=alice;Pwd=hunter2;         LEAK        LEAK     LEAK
    https://ghp_TOKEN@github.com/o/r.csv     LEAK        -        LEAK
    {"auth": {"password": "SEKRET"}}         LEAK        -        LEAK
    {"headers": {"X-Api-Key": "SEKRET"}}     LEAK        -        LEAK
    adbc...call_header.authorization: Bearer -           LEAK     -

Each of the three had its own regex, each regex was a slightly different guess
about where a credential lives, and every guess was wrong in a different place.
The fix is not a fourth regex. It is a rule about *when a value may be shown at
all*, applied in one place:

* **Known shape, locatable secret** — a ``scheme://userinfo@host`` DSN. Mask the
  credential and show the rest, which is exactly what these routes show today.
* **Anything else** — an ODBC keyword string, a URL carrying a query, a nested
  object, a driver option namespace. Withhold the whole value. Guessing where
  the secret is inside a format nobody here defined is what produced every row
  in that table.

Withheld values come back as ``WITHHELD``, a visible marker rather than an empty
string, so a viewer reads "this was withheld" instead of "this is blank, is that
a bug?" — ``EnginesSection.tsx`` renders ``e.uri`` straight into a table cell.

**THE OPEN PRODUCT QUESTION, stated so it stays visible.** Should a viewer see
``user@host:port/db`` at all, or only the password? Nobody has decided. This
module deliberately does **not** decide it: on a userinfo DSN it discloses
exactly what the tree disclosed before it — scheme, username, host, port, path —
and masks only the credential. ``laurelin/export/secrets.py`` answers the same
question the other way (it withholds the whole endpoint) because a file that
leaves the building is a different threat model from a response an admin reads
on a system they can already reach; that disagreement is intentional and is
argued in that module's docstring.

**The residual disclosure, plainly.** With this module in place, a viewer of an
admin route can still see:

* the username, host, port and database of any userinfo DSN — the open question
  above;
* the host and path of a URL with no query string, e.g. a webhook path of the
  form ``https://hooks.example.com/services/T00/B00/SECRET``, where the secret
  is *in the path* and nothing in the string says so;
* any credential an operator pasted into a free-form value that is not itself a
  URL and does not sit under a key that says it is one — a ``query`` holding
  ``SELECT ... 'postgres://a:p@h'`` is masked because the URL shape is
  recognisable inside it, but ``SELECT ... pwd='hunter2'``, or an ODBC keyword
  string filed under ``path``, is not, and this module does not pretend
  otherwise.

Closing the first two means answering the product question. The third is what
``export/pipeline_scan.py`` exists for, and it refuses rather than redacts.
"""

from __future__ import annotations

import re
from typing import Any

# Two distinct outcomes, and the difference matters to whoever reads the screen.
# MASK: we found the credential and removed it; everything else on screen is
# real. WITHHELD: we could not tell where the credential was, so nothing here is
# real. Collapsing them into one token would tell a viewer that a withheld ODBC
# string had "a password redacted", which is a claim we cannot make.
MASK = "*****"
WITHHELD = "***** (withheld)"

# A scheme followed by an authority. `://` and not `:` because `jdbc:` and
# `odbc:` wrap *another* DSN whose quoting rules are the wrapped driver's, which
# is precisely the case this module refuses to parse.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

# The same shape, unanchored, for finding a DSN inside free text (a driver's
# exception message, a SQL string). Stops at whitespace and at the quotes and
# brackets that end a URL in prose or in code.
#
# **Every one of those terminators can also appear in a password**, so this
# regex alone cannot be trusted to have found the whole DSN — measured, a
# ``query`` of ``postgres_scan('postgresql://alice:pa,SEKRET@db/prod', ...)``
# matched only ``postgresql://alice:pa``, ``redact_dsn`` saw no ``@`` in that
# and returned it unchanged, and the entire DSN survived. Widening the
# terminator set cannot fix it: a password may contain any character, so there
# is no set that both ends the URL in prose and never appears in a credential.
# The answer is :func:`_free_form_is_truncated`, which detects that the match
# was cut short and withholds the whole value rather than masking a fragment.
_EMBEDDED_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>()\[\]{},;]+")

# What an authority looks like when it holds no credential: a host (a dotted
# name, or a bracketed IPv6 literal) and an optional numeric port. `alice:pa`
# fails it because `pa` is not a port, which is the whole point — that string
# is the *start* of a userinfo, not a complete host.
_HOST_RE = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._\-]+)(:[0-9]*)?")

# The union of the three regexes this module replaces, plus the names they each
# individually missed. It is a denylist, and a denylist is *not* what makes this
# module safe — the shape rules above are. This survives for two reasons: it
# masks a secret sitting under an obvious name in a config whose vocabulary is
# the caller's (``SourceUpsertRequest.config`` is ``dict[str, Any]``), and a
# response that says ``password: *****`` is more readable than one that says
# ``password: ***** (withheld)`` for the same field. Nothing depends on it
# being complete; the default for a container is withhold, not disclose.
#
# `key` on its own is here because federation and engines already matched it and
# `access_key`/`secret_key` are the names that turn up in an S3-backed source.
#
# `auth` and `sig` are anchored the way export/secrets.py anchors them: unbound
# they match `author` and `assigned_to`, and masking a field because its name
# contains three letters is the kind of imprecision that gets a redactor
# distrusted and then bypassed.
API_SECRET_KEY_RE = re.compile(
    r"password|passwd|secret|token|credential|authorization|(^|_)auth($|_)|"
    r"api[_-]?key|access[_-]?key|key|(^|_)sig(nature)?($|_)|^pwd$|^dsn$|"
    r"passphrase|^conn(ection)?_?str",
    re.I,
)


# Keys whose value the config *declares* to be an endpoint: `url`, `uri`, and
# the suffixed forms (`base_url`, `endpoint_url`, `jdbc_uri`) that
# export/secrets.py measured on real registrations. A value under one of these
# is held to the DSN rules, which means an unparseable one is withheld rather
# than shown — `{"url": "Server=db;Uid=alice;Pwd=hunter2;"}` is a config saying
# "this is my endpoint" about a string in a format we cannot read.
#
# `path` is deliberately *not* here: a file source's path is `/mnt/land/*.csv`
# far more often than it is a URL, and withholding every one of those would
# empty the Sources screen to protect nothing.
_URL_KEY_RE = re.compile(r"(^|_)(url|uri)$", re.I)


# A credential written as `keyword=value` instead of as a URL. Nothing above
# can see one: every shape rule in this module keys on `://`, and a libpq
# conninfo, an ODBC keyword string and a DuckDB `CREATE SECRET` have no scheme
# at all. They are not exotic formats — DuckDB's postgres and mysql extensions
# take a conninfo *verbatim* in `ATTACH` and `postgres_scan`, which is a
# dashboard panel's SQL, and ODBC takes `Driver={..};Pwd=..`, which is what an
# operator pastes into a source's `path`. Measured: five of these went through
# `credential_in_free_text` untouched and a plain VIEWER read the password out
# of `GET /dashboards`.
#
# **Why two regexes and not one.** Matching `password=` alone is the vocabulary
# test this module refuses elsewhere, and for a gate that returns 400 it would
# reject `WHERE password = :p`. A credential *string* is a password keyword
# **beside a connection keyword** — a host, a driver, a database. Both halves
# are required, and neither half matches a value that is quoted, because SQL
# quotes its literals and conninfo does not: `password = 'x'` in a WHERE clause
# has a `'` where these need a value character.
_KEYWORD_SECRET_RE = re.compile(
    r"(?:^|[\s;'\"(,{&?])(?:secret[_-]?key|api[_-]?key|access[_-]?key|"
    r"password|passwd|pwd|passphrase|secret|token|credential|auth)"
    r"\s*=\s*[^\s;'\"),]",
    re.I,
)
_KEYWORD_ENDPOINT_RE = re.compile(
    r"(?:^|[\s;'\"(,{&?])(?:hostaddr|hostname|host|server|driver|data\s?source|"
    r"dsn|dbname|database|catalog|account|endpoint|port|uid|username|user|"
    r"service_name)\s*=\s*[^\s;'\"),]",
    re.I,
)

# DuckDB's own secret syntax, which uses no `=` at all:
# `CREATE SECRET s (TYPE S3, KEY_ID 'AKIA...', SECRET '...')`. A statement that
# names a secret *is* a credential; there is nothing to weigh.
_SQL_SECRET_STMT_RE = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?(?:persistent\s+|temporary\s+|temp\s+)?secret\b",
    re.I,
)


def keyword_credential(value: Any) -> bool:
    """Whether `value` carries a credential in a ``keyword=value`` DSN format.

    See the regexes above for what counts and why both halves are required.
    This is the one place in the module that reads vocabulary rather than
    shape, and it is bounded to the case where the shape rules are structurally
    blind: a connection string with no ``://`` in it.
    """
    if not isinstance(value, str) or value == "":
        return False
    if _SQL_SECRET_STMT_RE.search(value):
        return True
    return bool(
        _KEYWORD_SECRET_RE.search(value) and _KEYWORD_ENDPOINT_RE.search(value)
    )


def redact_dsn(value: Any) -> Any:
    """Mask the credential in a DSN, or withhold the value whole.

    The parse is deliberately *not* ``urllib.parse.urlsplit``: that function
    obeys RFC 3986, which says the authority ends at the first ``/``, and real
    passwords contain ``/``. Measured — ``postgresql://alice:pa/ss@db:5432/prod``
    through ``connectors._redact_url_password`` (which did use urlsplit) came
    back with ``pa/ss`` intact, because urlsplit read the authority as
    ``alice:pa`` and everything after it as a path.

    So the boundary here is the **last** ``@``, not the first ``/`` — except
    when a ``/`` sits between the two, at which point the string is genuinely
    ambiguous and this function refuses to guess.

    **Why refusing, and not over-masking.** ``//alice:pa/ss@db/prod`` (a
    password holding ``/``) and ``//reports.example.com/exports/x@2024.csv``
    (an ``@`` in a path) are the same string shape; nothing distinguishes them.
    Taking the last ``@`` used to resolve both the same way, and on the second
    one the result was not merely over-masked, it was *false*: the host became
    ``2024.csv``. Measured — ``reports.prod.example.com`` and
    ``reports.stage.example.com`` both rendered as ``https://*****@2024.csv``,
    two different endpoints shown as one fictitious one, with nothing on screen
    saying a substitution had happened (``ui.tsx`` only draws the "withheld"
    explanation on an exact ``WITHHELD`` match). That breaks this module's
    contract for ``MASK`` — *we found the credential and removed it; everything
    else here is real* — so the ambiguous case is a ``WITHHELD``, which is the
    contract it actually satisfies. An ``@`` in a path is not exotic: S3 landing
    prefixes keyed by email address and date-tagged filenames both hit it.
    """
    if not isinstance(value, str) or value == "":
        return value
    match = _SCHEME_RE.match(value)
    if match is None:
        # Not a URL: an ODBC/JDBC keyword string (`Server=db;Uid=a;Pwd=p;`), a
        # bare hostname, a driver blob. These formats have their own quoting
        # (`{}` escapes, embedded `;`), the key spelling varies by driver
        # (`Pwd`, `PWD`, `Password`), and getting it wrong silently ships the
        # password — which is what all three redactors did with this input.
        return WITHHELD
    rest = value[match.end():]
    if "?" in rest or "#" in rest:
        # A query carries secrets under names chosen by whoever built the URL:
        # api_key, sig, X-Amz-Signature, access_token, auth. Matching those
        # names is the denylist that already failed twice here, so the value
        # goes whole. This costs disclosure on an innocent `?format=csv` — the
        # price of not guessing, and the one place this module shows *less*
        # than before.
        return WITHHELD
    at = rest.rfind("@")
    if at == -1:
        return value  # no userinfo: nothing to mask, and the endpoint is policy
    userinfo = rest[:at]
    if "/" in userinfo:
        # Ambiguous: this `@` may open a host or may sit inside a path. See the
        # docstring. Withholding is the only outcome that does not put a
        # fabricated hostname on an admin's screen.
        return WITHHELD
    colon = userinfo.find(":")
    if colon > -1 and userinfo[:colon] == userinfo[colon + 1:]:
        # Username and password are the same string. The disclosure policy
        # shows the username, so masking only the password slot leaves the
        # working credential in the response verbatim. Both slots go: this is
        # the one case where the acknowledged endpoint disclosure is not a
        # hostname but a live secret.
        return value[:match.end()] + MASK + rest[at:]
    if colon == -1:
        # Bare userinfo, no colon: `https://ghp_TOKEN@github.com/o/r.csv`. A
        # username and a bearer token are the same characters in the same slot
        # and nothing distinguishes them, so the slot is masked. The endpoint
        # after the `@` is untouched, which keeps the disclosure policy where it
        # was: this hides a username only in the case where there is no password
        # to hide it beside.
        return value[:match.end()] + MASK + rest[at:]
    return value[:match.end()] + userinfo[: colon + 1] + MASK + rest[at:]


def _free_form_is_truncated(text: str) -> bool:
    """Whether a ``scheme://`` run inside `text` was cut short of its credential.

    ``_EMBEDDED_URL_RE`` ends a match at whitespace, quotes and brackets. A
    password may contain every one of those, so a match that stops before the
    ``@`` hands :func:`redact_dsn` a fragment with no userinfo in it, which it
    correctly leaves alone — and the credential ships. Measured through the
    live sources route: six passwords differing only in one character (``,``
    ``;`` space ``'`` ``)`` ``]``) all survived in full, in a ``query`` field
    an editor reads and the UI prints in the "From" column.

    A match is trusted only when its authority is *complete and free of
    credentials*, which needs both halves:

    * **Complete** — the match contains the ``/`` that ends the authority, or
      it runs to the end of the string. Otherwise the authority may continue
      past the cut. (``postgresql://alice`` cut from ``alice,SEKRET@db/prod``
      looks like a perfectly good host on its own.)
    * **Credential-free** — the authority parses as ``host[:port]``.
      ``alice:pa`` does not, because ``pa`` is not a port; it is the beginning
      of a userinfo whose ``@`` lies beyond the cut.

    **A match that already contains an ``@`` is not exempt**, and assuming it
    was is the defect this paragraph replaces. The old code short-circuited on
    ``"@" in run`` — "the authority is inside the match, `redact_dsn` handles
    it" — which is only true if that ``@`` is the *last* one. A password may
    hold a terminator too: ``postgres://alice:p@ss word@db/prod`` matches only
    as far as ``postgres://alice:p@ss``, and ``redact_dsn`` dutifully masks to
    the ``@`` inside the fragment and ships ``ss word`` — most of the password,
    under a ``*****`` that positively asserts the credential was removed.
    Measured on every terminator in the regex class. So a match carrying a
    userinfo gets the same question as one that carries none: could the
    credential continue past the cut?

    An ``@`` that sits inside *another* matched run is not evidence of that —
    it belongs to that URL, not to this one — so the lookahead skips them. A
    message naming two DSNs still has both masked rather than being withheld
    whole.

    When an untrusted match is followed by a stray ``@``, the caller withholds
    the whole value. The false positive that buys — a string holding both a
    truncated-looking URL and an unrelated ``@`` — costs a display value the
    operator can still read from their own config. Under-masking costs them the
    password.
    """
    matches = list(_EMBEDDED_URL_RE.finditer(text))
    spans = [(m.start(), m.end()) for m in matches]

    def stray_at_after(start: int) -> bool:
        return any(
            char == "@"
            and not any(begin <= index < end for begin, end in spans)
            for index, char in enumerate(text[start:], start)
        )

    for match in matches:
        run = match.group(0)
        scheme = _SCHEME_RE.match(run)
        if scheme is None:  # pragma: no cover - the regex guarantees one
            continue
        after_scheme = run[scheme.end():]
        authority = after_scheme.split("/", 1)[0]
        if "@" in authority:
            # A userinfo is inside the match, but it may not be all of one.
            if stray_at_after(match.end()):
                return True
            continue
        complete = "/" in after_scheme or match.end() == len(text)
        if complete and _HOST_RE.fullmatch(authority):
            continue
        if stray_at_after(match.end()):
            return True
    return False


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


# Below this length a credential cannot be substituted out of a sentence
# without wrecking it — masking every "a" in a message to hide a one-character
# password produces something worse than useless. Tokens shorter than this are
# handled by withholding the whole message instead.
_MIN_SCRUB = 3


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


def redact_value(value: Any) -> Any:
    """One scalar of a config, as it may be shown.

    A string that *is* a URL gets the DSN rules. A string that merely quotes one
    keeps its own text and has the quoted DSN masked — a SQL ``query`` naming a
    ``postgres_scan('postgres://a:p@h/db')`` leaked in full from every one of
    the three redactors, because none of them looked at a value whose key was
    not spelled ``url``.

    A string that is a ``keyword=value`` connection string is withheld whole.
    It has no ``://`` for any rule here to key on, and masking one means
    knowing a driver's quoting rules — which is what :func:`redact_dsn` refuses
    to do for the same format under a ``url`` key.
    """
    if keyword_credential(value):
        return WITHHELD
    if isinstance(value, str) and _SCHEME_RE.match(value):
        return redact_dsn(value)
    if isinstance(value, str) and "://" in value:
        if _free_form_is_truncated(value):
            return WITHHELD
        return _EMBEDDED_URL_RE.sub(lambda m: str(redact_dsn(m.group(0))), value)
    return value


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


def _split_query(rest: str) -> tuple[str, str, str]:
    """`rest` split at the first ``?`` or ``#``, whichever comes first."""
    match = re.search(r"[?#]", rest)
    if match is None:
        return rest, "", ""
    return rest[: match.start()], match.group(0), rest[match.end():]


def withhold_values(value: Any) -> Any:
    """Keep the key names of a nested structure, withhold every value in it.

    Applied to anything below the top level of a config, and to an engine's
    driver options. The reason is the same in both places and it is not that
    these particular objects are dangerous: it is that their key vocabulary
    belongs to somebody else. ``redacted_config`` walked ``headers`` with a
    name denylist and shipped ``{"X-Api-Key": "SEKRET"}``; ``EngineConfig``
    walked ``options`` with a different one and shipped
    ``adbc.flight.sql.rpc.call_header.authorization: Bearer SEKRET``. Neither
    name was on anybody's list, and neither ever would be.

    Names survive because the operator needs to see the shape they configured —
    that a source sends an ``Authorization`` header at all is the fact they are
    checking. A list becomes a single marker: its elements have no names, so
    there is no shape to preserve.
    """
    if isinstance(value, dict):
        return {key: withhold_values(sub) for key, sub in value.items()}
    return WITHHELD


def redact_mapping(config: Any) -> Any:
    """A config object as an API response may carry it.

    Top level only is disclosed, and only scalars: a secret-named key is masked,
    a URL-shaped value is DSN-redacted, any other scalar is shown as it is
    stored, and anything nested keeps its names and loses its values.
    """
    if not isinstance(config, dict):
        # Not an object: there are no keys to judge and no shape to preserve.
        return WITHHELD if config not in (None, "") else config
    out: dict[str, Any] = {}
    for key, value in config.items():
        if API_SECRET_KEY_RE.search(str(key)):
            out[key] = MASK
        elif isinstance(value, (dict, list, tuple)):
            out[key] = withhold_values(value)
        elif _URL_KEY_RE.search(str(key)) and isinstance(value, str):
            out[key] = redact_dsn(value)
        else:
            out[key] = redact_value(value)
    return out
