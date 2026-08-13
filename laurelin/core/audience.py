"""Who a field was written *for*, declared on the field.

R2, stated once: **a field is readable by a principal iff the principal's
effective role is at least the field's audience role. The audience role is
``viewer`` iff the field is explicitly declared PRESENTATION; otherwise it is
the authoring role of the record type that contains it.** If you cannot write
it, you cannot read it.

Three deliberate choices, each of which is the reason a previous round leaked.

*Per field, not per route or per record.* Per route is what the tree had, and it
is why ``routes._dump`` special-cased exactly one model: ``DatasetInfo``. Per
record is unshippable, because ``DashboardInfo`` holds both ``title`` (a caption
written *for* the viewer) and ``panels[].sql`` (a query written for the
machine), and R2 applied to the whole record either leaks the query or deletes
the caption.

*OPERATIONAL is the default.* A field added tomorrow with no annotation is
withheld from anyone below the record's authoring role. New field ⇒ fails
closed. An author must opt *in* to viewer-visibility, and opting in is a line of
code somebody has to write and somebody else can grep for.

*``admin`` is the default authoring role.* A model added tomorrow that forgets
to declare one is admin-only. New model ⇒ fails closed.

**Two audiences, forever.** Three would be a vocabulary, and vocabularies are
what kept failing here: every round of this bug added a case to a classifier and
every round something walked around the case. There is no PRESENTATION-ish.

This module deliberately imports nothing from :mod:`laurelin.core.models` —
``models`` imports :class:`Audience` to annotate its fields, and the enforcement
lives in :mod:`laurelin.core.serialize`. The vocabulary sits at the bottom of
the dependency graph so a model can declare its audience without dragging in the
API layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from pydantic import BaseModel

from laurelin.core.roles import Role


class Audience(str, Enum):
    """The two audiences a field can have been written for."""

    # Authored FOR a lower-privileged reader: a caption, a name, a status, a
    # timestamp, a count. Something whose whole purpose is to be read by
    # somebody who cannot write it.
    PRESENTATION = "presentation"

    # Authored for the machine, or for the operator running it: a query, a
    # connection config, a filesystem path, a filter expression, a driver's
    # vendor code. The default, by omission.
    OPERATIONAL = "operational"


class Governed(BaseModel):
    """A model that crosses the API boundary and therefore declares an audience.

    ``laurelin_author_role`` is the privilege level that can *write* a record of
    this type. It defaults to ``admin`` so that a model which forgets to declare
    one discloses nothing below admin — the failure mode is "too little
    disclosed", which is the only kind that survives a round of attackers.

    Field audiences are plain ``Annotated`` metadata::

        class DashboardInfo(Governed):
            laurelin_author_role: ClassVar[Role] = Role.editor
            title: Annotated[str, Audience.PRESENTATION] = ""
            # anything not annotated is OPERATIONAL and is not serialized to a
            # reader below `laurelin_author_role`.
    """

    laurelin_author_role: ClassVar[Role] = Role.admin

    def laurelin_record_author_role(self) -> Role:
        """This *record's* author role. Class-level by default.

        Overridden by exactly one model, and for a reason worth stating.
        ``AuditEvent`` carries ``min_read_role``, a level the row's *writer*
        declared, and that column is the honest author role of that row's
        ``details``. Without this hook the two mechanisms answered the same
        question and the second silently cancelled the first: ``list_audit``
        chose which rows an editor could see, and then the serializer dropped
        ``details`` from every one of them because ``AuditEvent`` is class-level
        admin. Measured: all five ``min_read_role=Role.editor`` declarations in
        the tree were dead code, each carrying a comment asserting a disclosure
        that did not happen.
        """
        return type(self).laurelin_author_role


def record_author_role(model) -> Role:
    """The author role of one record — instance-aware, class as the fallback."""
    hook = getattr(model, "laurelin_record_author_role", None)
    if hook is None:
        return author_role(type(model))
    return hook()


@dataclass(frozen=True)
class AuthoredBy:
    """One field was written at a *higher* privilege than its record.

    A record type usually has a single author, and then ``laurelin_author_role``
    says everything. Two records in this tree do not, and both were confirmed
    disclosures:

    * ``DatasetInfo`` is editor-authored (an editor creates and describes a
      dataset) but ``DatasetInfo.source`` is written only by the three ADMIN
      registration routes. Measured on this tree: a plain editor read five live
      credentials out of it — a quoted libpq conninfo, a quoted ODBC keyword
      string, a colon-delimited form, a bare AWS key pair and a positional JDBC
      URL — because the only thing between them was ``redaction.redact_value``,
      the free-text matcher this whole change exists to stop depending on.
    * ``Failure`` is editor-authored (an editor owns the build that failed) but
      ``Failure.endpoint`` is rebuilt from an ADMIN-authored connector config.

    This is **not** a third audience. There are still exactly two audiences.
    It is the same ``Role`` vocabulary already used for records, applied at the
    granularity the data actually has, and it fails closed the same way: a field
    with no ``AuthoredBy`` inherits its record's author role.

    Declaring ``AuthoredBy`` on a ``PRESENTATION`` field is a contradiction — a
    caption for a viewer cannot also be admin-authored — and
    ``tests/test_audience.py`` rejects it.
    """

    role: Role


def author_role(model_cls: type) -> Role:
    """The role that can author this record type.

    ``admin`` for anything that has not declared — including every model that is
    not :class:`Governed`, and every model added tomorrow by somebody who has
    not read this file.
    """
    return getattr(model_cls, "laurelin_author_role", Role.admin)


def field_author_role(
    model_cls: type[BaseModel], name: str, record: Role | None = None
) -> Role:
    """The role that can *write* one field: its ``AuthoredBy``, else its record's.

    Never lower than the record's own author role. A field cannot be made more
    readable by annotating it — only less.
    """
    record = author_role(model_cls) if record is None else record
    field = model_cls.model_fields.get(name)
    if field is None:
        return record
    for meta in field.metadata:
        if isinstance(meta, AuthoredBy):
            return meta.role if meta.role.covers(record) else record
    return record


def field_audience(model_cls: type[BaseModel], name: str) -> Audience:
    """The declared audience of one field. OPERATIONAL unless it says otherwise."""
    field = model_cls.model_fields.get(name)
    if field is None:
        return Audience.OPERATIONAL
    for meta in field.metadata:
        if isinstance(meta, Audience):
            return meta
    return Audience.OPERATIONAL


def presentation_fields(model_cls: type[BaseModel]) -> list[str]:
    """Field names a reader below the record's authoring role may see."""
    return [
        name
        for name in model_cls.model_fields
        if field_audience(model_cls, name) is Audience.PRESENTATION
    ]
