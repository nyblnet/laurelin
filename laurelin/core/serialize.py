"""The one place a model becomes a response body — and the one place R2 is enforced.

``_dump`` was already the single serialization point for every route module
(``api/routes.py`` and the six routers that import it), which is why it is the
seam R2 goes through. It moved *out* of ``api/routes.py`` and into ``core`` for
one reason: left in the API layer it would protect the REST surface and nothing
else, and the MCP client, the CLI and the export writer all serialize the same
models.

How the role gets here
----------------------

A :class:`~contextvars.ContextVar`, set by ``AudienceMiddleware`` in
``laurelin/api/app.py`` and reset in a ``finally``. Verified on this tree: a
ContextVar set in an ``@app.middleware("http")`` **is** visible inside sync
route handlers — which is what every Laurelin handler is, they run in the
threadpool — and ``token.reset()`` restores the default between requests.
(Setting it in a dependency would *not* work: sync dependencies run in their own
threadpool context copy, and the set would not propagate to the handler.)

Why a ContextVar rather than threading ``request`` through 59 call sites:

    **The default is ``Role.viewer``, the lowest privilege.** A route that
    forgets to arrange anything still gets filtering. Forgetting redacts more,
    never less. A rule whose failure mode is "too little disclosed" is the only
    kind that survives rounds 2 and 3 of this bug.

Non-HTTP callers — ``laurelin/cli.py``, ``laurelin/export/`` — legitimately need
raw values, and say so with :func:`as_author`. That is a greppable opt-out, and
``tests/test_audience.py`` asserts by AST that it appears nowhere else. The CLI
is not a privilege crossing: possession of the workspace directory is the
credential there.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Iterator

from pydantic import BaseModel

from laurelin.core.audience import (
    Audience,
    field_audience,
    field_author_role,
    record_author_role,
)
from laurelin.core.roles import Role

# Lowest privilege by default: an unset context discloses the least.
_effective_role: ContextVar[Role] = ContextVar("laurelin_effective_role", default=Role.viewer)


def effective_role() -> Role:
    return _effective_role.get()


def set_effective_role(role: Role):
    """Set the serializing principal's role; returns the token to ``reset()``.

    Used by ``AudienceMiddleware``. Everything else should use
    :func:`as_author` so the escape hatch stays greppable.
    """
    return _effective_role.set(role)


def reset_effective_role(token) -> None:
    _effective_role.reset(token)


@contextmanager
def as_author(role: Role = Role.admin) -> Iterator[None]:
    """Serialize as if the caller could author the record — the explicit opt-out.

    Only legitimate off the HTTP path: the CLI and the export writer run with
    the operator's filesystem privileges and need the operational fields
    (a source's config, a panel's SQL) because those are the product. Every use
    is asserted by ``tests/test_audience.py``.
    """
    token = _effective_role.set(role)
    try:
        yield
    finally:
        _effective_role.reset(token)


def dump(model: BaseModel) -> dict:
    """A model as a response body, filtered to what this principal may read."""
    return dump_as(model, _effective_role.get())


def dump_as(model: BaseModel, role: Role, *, narrowed: bool = False) -> dict:
    """One model, filtered for ``role``.

    ``narrowed`` is set when this model is nested inside a record the reader
    could **not** author, and it forces the projection regardless of this
    model's own author role. That one flag is the fix for a confirmed
    disclosure: ``SourceInfo`` is admin-authored and ``Failure`` is
    editor-authored, so an editor reading ``GET /sources/{name}`` received a
    *projection* of the source — correctly withholding ``config`` — and then a
    **full** dump of the nested failure, whose ``endpoint`` is rebuilt from that
    same withheld config. Recursion widened instead of narrowing.

    The rule is now monotonic: **descending into a record can only ever
    disclose less.** A nested model with a stricter author role still narrows
    further, because ``dump_as`` re-evaluates its own author role on the way in.
    """
    full, fields = _projection(type(model), role, record_author_role(model), narrowed)
    raw = model.model_dump(mode="json", by_alias=True)
    out: dict[str, Any] = {}
    nested = not full
    for name, key in fields:
        value = getattr(model, name, None)
        out[key] = _value_as(value, role, narrowed=nested) if _holds_model(value) else raw.get(key)
    if full:
        _apply_dataset_source_rule(model, out, role)
    return out


@lru_cache(maxsize=None)
def _projection(
    cls: type[BaseModel], role: Role, author: Role, narrowed: bool
) -> tuple[bool, tuple[tuple[str, str], ...]]:
    """Which keys of ``cls`` this reader receives — computed once per shape.

    **This decides nothing.** It is the same four lines that used to sit inline
    in :func:`dump_as`, lifted out because their answer depends only on the
    arguments: a model class, the reader's role, the record's author role, and
    whether a parent already narrowed. No field *value* is consulted, which is
    what makes memoizing it safe — and ``tests/test_audience.py`` asserts the
    equivalence directly against the uncached rules rather than trusting that
    sentence.

    The cache is bounded in practice by its key space: ~25 governed classes x 3
    roles x 3 author roles x 2 booleans. It is keyed on the class object, which
    keeps a strong reference — fine, because every one of them is defined at
    module scope and lives for the process anyway.

    Why it exists: measured on this tree, deciding this per field per record
    was **68% of serialization time** — ``field_author_role`` re-scanned
    ``field.metadata`` and re-read ``cls.model_fields`` (a pydantic property,
    not a plain dict) once per field per record, and ``Role.covers`` resolved
    two dict lookups behind a property each time. Actual pydantic serialization
    was 7%. A list of 1 000 datasets cost 14.5 ms to project and 2.7 ms to
    serialize; see docs/SCALE.md, "What the audience projection costs".
    """
    full = not narrowed and role.covers(author)
    fields: list[tuple[str, str]] = []
    for name, field in cls.model_fields.items():
        if full:
            # A field authored above its record's level — DatasetInfo.source,
            # Failure.endpoint — is withheld even from a reader who can author
            # the record itself.
            if not role.covers(field_author_role(cls, name, author)):
                continue
        elif field_audience(cls, name) is not Audience.PRESENTATION:
            # Operational keys are **omitted**, not blanked: a blank `sql` is a
            # value a read-modify-write client will happily PUT back over the
            # real one.
            continue
        fields.append((name, field.alias or name))
    return full, tuple(fields)


def detail_for(failure, author_role_: Role = Role.admin) -> str:
    """A failure as an HTTP ``detail`` string, filtered like a field would be.

    ``HTTPException(detail=...)`` never passes through :func:`dump`, so R2 had
    no jurisdiction over it — and that was a confirmed disclosure.
    ``POST /sources/{name}/sync`` has no role dependency (its only gate is
    ``_require_dataset_edit``), so a plain editor, or a viewer holding an
    explicit ``can_edit`` grant on the backing dataset, triggered a sync of an
    ADMIN-authored connector and read the admin's endpoint out of the 502:

        "The host for source:crm could not be resolved at
         secret-db.internal.corp:55999. (psycopg/OperationalError/ref err-…)"

    The same editor reading the record the ordinary way gets no ``config`` at
    all. So the error path disclosed what the read path withheld.

    ``author_role_`` is the level that authored the *configuration this failure
    describes* — admin for a connector, engine or federated dataset. Above it,
    the operator sentence; below it, the brief one, which names the subject and
    the code and carries the ``detail_ref`` for someone who can read the log.
    """
    if _effective_role.get().covers(author_role_):
        return failure.render()
    return failure.render_brief()


def _value_as(value: Any, role: Role, *, narrowed: bool) -> Any:
    if isinstance(value, BaseModel):
        return dump_as(value, role, narrowed=narrowed)
    if isinstance(value, (list, tuple)):
        return [_value_as(v, role, narrowed=narrowed) for v in value]
    if isinstance(value, dict):
        return {k: _value_as(v, role, narrowed=narrowed) for k, v in value.items()}
    return value


# Types that cannot be, and cannot contain, a BaseModel. Matched by *exact*
# type, not isinstance: a `str` subclass — every `str`-valued Enum in models.py
# is one — is deliberately not in here and falls through to the full check.
# Widening this set to cover subclasses would be the bug; it is a fast path, and
# a fast path that answers a question differently from the slow one is a hole.
_NEVER_HOLDS_MODEL = frozenset({str, int, float, bool, type(None)})


def _holds_model(value: Any) -> bool:
    if type(value) in _NEVER_HOLDS_MODEL:
        # 41% of serialization time after the projection was memoized, because
        # most fields of most records are a string or a number and each one
        # still paid three isinstance() calls to establish it. Exact-type first.
        return False
    if isinstance(value, BaseModel):
        return True
    if isinstance(value, (list, tuple)):
        return any(_holds_model(v) for v in value)
    if isinstance(value, dict):
        return any(_holds_model(v) for v in value.values())
    return False


def _apply_dataset_source_rule(model: BaseModel, out: dict, role: Role) -> None:
    """``DatasetInfo.source`` reaches **admin only**, and is still redacted there.

    It used to reach an editor, redacted by ``redaction.redact_mapping`` — and
    that was the last place in the tree where somebody's confidentiality rested
    on the free-text matcher. Measured: an editor read a quoted libpq conninfo,
    a quoted ODBC keyword string, a colon-delimited credential, a bare AWS key
    pair and a positional JDBC URL straight out of ``GET /datasets/{name}``,
    because ``keyword_credential`` returned ``False`` for all five. Adding a
    quote after ``password=`` defeated the boundary.

    ``source`` is ``AuthoredBy(Role.admin)``, so ``dump_as`` has already dropped
    the key for anyone below admin before this function is reached — and that is
    deliberately the **only** mechanism. A second role check here would be
    defence in depth in name and a duplicate answer in practice, which is the
    exact shape of two defects this round: ``min_read_role`` filtering rows
    while the serializer independently dropped their contents, and a nested
    ``Failure`` re-widening what its parent had narrowed. When two mechanisms
    answer one question, reverting either leaves the tests green and nobody
    learns which one was load-bearing.

    What an editor gets instead is ``DatasetInfo.source_descriptor``, which
    Laurelin builds from an allowlist of shape keys. ``redacted_source``
    survives here as a courtesy to the admin who *can* read it — "the person who
    typed it" and "the person reading this screen" are not necessarily the same
    admin — and it is explicitly **not** a boundary: no test may assert that
    bypassing it is a security failure.
    """
    if type(model).__name__ != "DatasetInfo" or not out.get("source"):
        return
    from laurelin.core.federation import redacted_source

    out["source"] = redacted_source(getattr(model, "source", {}))
