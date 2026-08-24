"""Fine-grained ontology access control.

Each object type has a list of grants. The model is deliberately simple and
predictable:

- **Admins** always have full access (bypass).
- If an object type has **no grants**, it inherits the global RBAC default:
  any authenticated user may *view* it, and ``editor``+ may *edit* (apply
  actions on) it. This keeps existing workspaces working unchanged.
- If an object type has **any grants**, it is locked to that allowlist: a user
  may view/edit only if some grant matches them (by ``everyone``, their role,
  a group they belong to, or their username) with the needed capability. A
  global editor with no matching grant is denied — that is the point of adding
  grants. ``can_edit`` implies ``can_view``.

Grants can thus both *restrict* (hide a type from most users) and *elevate*
(let a specific viewer edit one type), all per object type.

**Scope — important.** These grants gate the *ontology layer* only. They do NOT
secure the backing dataset: a user denied an object type can still read the same
rows through ``/api/v1/query`` and ``/api/v1/datasets/{name}/rows`` (any viewer
can query any dataset). Ontology grants are a presentation/authoring control,
not data confidentiality. Dataset-level access control (and thereby true
data-hiding) is a separate, larger piece of work (see docs/ROADMAP.md, WS8).
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pyarrow as pa
import pyarrow.compute as pc

from laurelin.core.db import MetadataStore
from laurelin.core.dialects import CLICKHOUSE, DUCKDB, SqlDialect  # noqa: F401
from laurelin.core.models import (
    ColumnMask,
    DatasetPolicy,
    Grant,
    MaskMode,
    Role,
    RowPolicy,
    SubjectKind,
    User,
)


@dataclass(frozen=True)
class PolicyPlan:
    """How to enforce a dataset policy for one user on one scan.

    ``lazy`` plans push a filter and/or a projection into the Parquet scan;
    non-lazy plans materialize the table and call ``apply`` (the exact path,
    used where a rule has no Arrow equivalent).
    """

    lazy: bool
    filter: Any = None
    projection: Optional[dict] = None
    apply: Optional[Callable] = None


@dataclass(frozen=True)
class PolicyDecision:
    """What a policy does for one user on one dataset — engine-independent."""

    denies_all: bool = False
    row_column: Optional[str] = None
    allowed_values: Optional[list[str]] = None
    masks: list = field(default_factory=list)  # [(column, MaskMode)]

    @property
    def applies(self) -> bool:
        return self.denies_all or self.row_column is not None or bool(self.masks)


# Kept as a name because it is the DuckDB quoter and always was; it is not a
# general-purpose one. Anything targeting another engine must go through that
# engine's dialect — see laurelin/core/dialects.py for the measured reason.
_q = DUCKDB.quote


class PolicyRenderError(RuntimeError):
    """A policy could not be applied *exactly*.

    Raised instead of returning something approximate, because the only thing
    worse than refusing a read is serving one whose policy was rounded off.
    """


def _confusable(name: str) -> str:
    """The form two identifiers must share to be "the same name, mistyped".

    Case is the classic one (``SSN`` for ``ssn``). Trailing whitespace and
    compatibility-equivalent characters are the same mistake wearing a
    different hat: ``'ssn '`` and ``'ｓsn'`` both read as ``ssn`` to whoever
    wrote the policy, and both currently sail past a case-only comparison into
    a plaintext read.
    """
    return unicodedata.normalize("NFKC", name).strip().casefold()


#: Public name for the same fold, used by `laurelin.transforms.flow_compile`
#: and `flow_governance`.
#:
#: Those two need "would a query engine, or a person, treat these two
#: identifiers as the same name?" and they must answer it *identically to this
#: module*, because a divergence is a laundering hole rather than an
#: inconsistency. Measured on this tree, before the flow compiler used it: a
#: mask on `pay` and a flow deriving a column called `PAY` produced a built
#: dataset holding real salaries, because the compiler compared names with
#: Python `in` (case-sensitive) while DuckDB resolves identifiers
#: case-insensitively and takes the first match.
#:
#: DuckDB's own fold is ASCII-only (measured: it treats `pay`/`PAY` as one name
#: but `à`/`À`, `σ`/`Σ` and `K`/`K` as two). This fold is strictly *more*
#: aggressive than that, which is the safe direction: it can refuse a flow the
#: engine would have run, and never the reverse.
confusable_identifier = _confusable


def _reject_case_mismatch(column: str, available) -> None:
    """Refuse a mask whose column name differs from a real one only in typo.

    A mask naming a column the dataset does not have is skipped, and that is
    right: a genuinely dropped column must not deny the whole dataset, or
    schema evolution becomes a denial of service. But a mask authored as
    ``SSN`` against a column ``ssn`` is not a dropped column — it is a typo
    that silently masks nothing and serves the plaintext. ClickHouse made this
    acute (its identifiers are case-sensitive; ``REGION`` is error 47) but the
    hole was never dialect-specific: the Arrow path no-ops on it too.

    An exact hit short-circuits, which is not a micro-optimisation: casefold is
    not injective, so ``'ß'.casefold() == 'ss'``. Folding first and looking the
    result up in a dict let a *real* column named ``ss`` be reported as a typo
    for a neighbouring ``ß`` — refusing a read that both ``decide()`` and the
    Arrow renderer serve correctly.
    """
    if column in available:
        return
    near = [c for c in available if _confusable(c) == _confusable(column)]
    if near:
        raise PolicyRenderError(
            f"Column mask names {column!r} but the dataset has "
            + " / ".join(repr(c) for c in near)
            + ". Identifier case, whitespace and unicode form all matter; fix "
            "the policy rather than serve the column unmasked."
        )


def _reject_unportable_text(
    role: str, column: str, arrow_type, dialect: SqlDialect, portable
) -> None:
    """Refuse to compare or digest a value whose text this engine spells its own way.

    Row policies and hash masks are both defined on the *text* of a value: the
    Arrow renderer compares ``pc.cast(col, string)`` against the allowlist and
    digests ``str(value)``. ``toString`` and ``CAST AS VARCHAR`` are neither of
    those functions, and where they differ the divergence is not cosmetic — it
    changes which rows a user sees. Measured, one dataset, one user, one policy
    value ``'1.1'`` on a ``decimal(12,2)`` column: the Arrow renderer returned
    0 rows and the ClickHouse renderer returned 2, another tenant's.

    So the renderer refuses rather than approximating, and says which mask
    modes *are* exact everywhere — ``null`` and ``redact`` need no rendering at
    all, so they remain available on any column of any type.
    """
    if arrow_type is not None and portable(arrow_type):
        return
    if arrow_type is not None and pa.types.is_nested(arrow_type):
        # Worth its own sentence: this is not "engines disagree", it is "the
        # reference cannot express this policy at all", so there is no correct
        # answer for any renderer to be measured against. Same wording as the
        # Arrow path's refusal so all three read alike.
        raise PolicyRenderError(
            f"Column {column!r} has type {arrow_type}, which has no text form, "
            f"so a {role} on it cannot mean anything definite. Point the "
            f"{role} at a scalar column."
        )
    if arrow_type is None:
        detail = (
            f"the type of {column!r} is unknown to the renderer, so there is no "
            "way to tell whether it does"
        )
    else:
        detail = f"{dialect.name} does not, for a column of type {arrow_type}"
    raise PolicyRenderError(
        f"A {role} on {column!r} compares the column as text, which only means "
        f"the same thing if this engine renders it the way Arrow does — and "
        f"{detail}. Refusing the read rather than enforcing a different policy "
        "here than the row API enforces. Mask modes 'null' and 'redact' are "
        "exact on every engine and every type."
    )


@dataclass(frozen=True)
class SqlPolicy:
    """A decision compiled to SQL: a projection list and a WHERE clause.

    ``select_list`` masks in place; ``dialect.assemble`` wraps it around a scan
    expression. The dialect travels *with* the policy so a caller cannot pair a
    ClickHouse-rendered policy with a DuckDB statement, which would be a
    plausible-looking query and a real leak.
    """

    select_list: str
    where: str
    params: list
    dialect: SqlDialect = DUCKDB

    @classmethod
    def render(
        cls,
        decision: PolicyDecision,
        columns: list[str],
        dialect: SqlDialect = DUCKDB,
        column_types: Optional[dict] = None,
    ) -> "SqlPolicy":
        """Compile ``decision`` for ``dialect``.

        ``column_types`` maps column name to its Arrow type and is **required**
        whenever the decision compares or digests a value as text — a row
        filter or a hash mask. Without it the renderer cannot tell whether this
        engine spells the value the way the Arrow reference does, and an
        allowlist compared against a different spelling is a different policy.
        Null and redact masks need no schema: ``NULL`` is ``NULL`` and ``'***'``
        is ``'***'`` on every engine.
        """
        if decision.denies_all:
            return cls(select_list="*", where="FALSE", params=[], dialect=dialect)

        # Masks compose in policy order rather than last-one-wins. Collapsing
        # them to a dict meant `redact` then `hash` digested the *plaintext*
        # while the Arrow renderer digested '***' — sha256 of the very secret
        # the first mask was there to remove.
        masked: dict[str, list] = {}
        for column, mode in decision.masks:
            masked.setdefault(column, []).append(mode)

        if not columns:
            if decision.applies:
                # Column discovery failed (unreachable source, empty DESCRIBE).
                # The old code joined an empty list and fell back to "*", which
                # is an *unmasked* read of a dataset that has masks pending —
                # fail open, in the one place that must not.
                raise PolicyRenderError(
                    "Cannot render the policy: the dataset's columns could not "
                    "be determined, so masks and filters cannot be placed. "
                    "Refusing the read."
                )
            return cls(select_list="*", where="TRUE", params=[], dialect=dialect)

        # decide() already rejects this for policies it reads; repeated here so
        # a hand-built PolicyDecision cannot slip past it.
        for col in masked:
            _reject_case_mismatch(col, columns)

        parts = []
        for col in columns:
            modes = masked.get(col)
            quoted = dialect.quote(col)
            if not modes:
                parts.append(quoted)
                continue
            expr = quoted
            # The type the *expression* has now, which is not the column's once
            # a mask has rewritten it: redact yields a string, and a hash over
            # a string is portable whatever the column started as.
            expr_type = None if column_types is None else column_types.get(col)
            for mode in modes:
                if mode == MaskMode.null:
                    expr = dialect.null_mask(expr)
                elif mode == MaskMode.redact:
                    expr = dialect.redact_mask(expr)
                    expr_type = pa.string()
                else:  # hash — must reproduce the table path's digest exactly
                    _reject_unportable_text(
                        "hash mask", col, expr_type, dialect,
                        dialect.hash_text_matches_arrow,
                    )
                    expr = dialect.hash_mask(expr)
                    expr_type = pa.string()
            parts.append(f"{expr} AS {quoted}")

        where, params = "TRUE", []
        if decision.row_column is not None:
            _reject_unportable_text(
                "row policy",
                decision.row_column,
                None if column_types is None else column_types.get(decision.row_column),
                dialect,
                dialect.row_key_matches_arrow,
            )
            # NULL is never IN a set, so null policy values are excluded —
            # the same fail-closed behavior as the Arrow renderer.
            if dialect.binds_values:
                values = ", ".join(
                    dialect.placeholder(i) for i, _ in enumerate(decision.allowed_values)
                )
                params = list(decision.allowed_values)
            else:
                # No binding channel on this engine; literal() is the single
                # audited place where a policy value becomes SQL text.
                values = ", ".join(dialect.literal(v) for v in decision.allowed_values)
            column = dialect.to_text(dialect.quote(decision.row_column))
            where = f"{column} IN ({values})"

        return cls(
            select_list=", ".join(parts), where=where, params=params, dialect=dialect
        )


class PermissionService:
    def __init__(self, store: MetadataStore):
        self.store = store

    # -- groups ---------------------------------------------------------------

    def _user_groups(self, username: str) -> set[str]:
        return {g.lower() for g in self.store.groups_for_user(username)}

    # -- grant matching -------------------------------------------------------

    def _subject_matches(
        self, kind: SubjectKind, subject_norm: str, user: User, groups: set[str]
    ) -> bool:
        if kind == SubjectKind.everyone:
            return True
        if kind == SubjectKind.role:
            return subject_norm == user.role.value
        if kind == SubjectKind.user:
            return subject_norm == user.username.lower()
        if kind == SubjectKind.group:
            return subject_norm in groups
        return False

    def _grant_matches(self, grant: Grant, user: User, groups: set[str]) -> bool:
        return self._subject_matches(
            grant.subject_kind, grant.normalized_subject(), user, groups
        )

    def _evaluate(self, user: Optional[User], grants: list[Grant]) -> tuple[bool, bool]:
        """Core rule shared by ontology and dataset grants: admin bypass; no
        grants -> global RBAC default; any grant -> allowlist."""
        if user is None:
            return (False, False)
        if user.role == Role.admin:
            return (True, True)
        if not grants:
            return (True, user.role.covers(Role.editor))
        groups = self._user_groups(user.username)
        can_view = can_edit = False
        for g in grants:
            if not self._grant_matches(g, user, groups):
                continue
            if g.can_edit:
                can_edit = True
                can_view = True
            elif g.can_view:
                can_view = True
        return (can_view, can_edit)

    # -- object-type (ontology) permissions -----------------------------------

    def _grants(self, object_type: str) -> list[Grant]:
        return [Grant(**g) for g in self.store.grants_for_type(object_type)]

    def permission(self, user: Optional[User], object_type: str) -> tuple[bool, bool]:
        """Return ``(can_view, can_edit)`` for ``user`` on the ONTOLOGY grants of
        ``object_type`` (not composed with the backing dataset — use
        ``object_type_permission`` for the effective access)."""
        return self._evaluate(user, self._grants(object_type))

    def can_view(self, user: Optional[User], object_type: str) -> bool:
        return self.permission(user, object_type)[0]

    def can_edit(self, user: Optional[User], object_type: str) -> bool:
        return self.permission(user, object_type)[1]

    # -- dataset permissions --------------------------------------------------

    def _dataset_grants(self, dataset: str) -> list[Grant]:
        return [Grant(**g) for g in self.store.grants_for_dataset(dataset)]

    def _has_clearance(self, user: Optional[User], dataset: str) -> bool:
        """Mandatory access control: a non-admin must hold clearance for every
        (effective, lineage-propagated) marking on the dataset. Admins/superadmins
        are the data stewards and bypass markings (so they can't lock themselves
        out); markings gate editors and viewers."""
        if user is None:
            return False
        if user.role == Role.admin:
            return True
        # Explicit ∪ effective in one read: an explicit marking whose
        # propagation (recompute) hasn't run yet must already deny — the gap
        # between the two route statements is otherwise an enforcement hole
        # (tests/test_concurrency_exec.py::test_a_marking_denies_the_moment_
        # its_write_returns).
        needed = set(self.store.get_enforced_markings(dataset))
        if not needed:
            return True
        return needed <= set(self.store.get_clearances(user.username))

    def dataset_permission(self, user: Optional[User], dataset: str) -> tuple[bool, bool]:
        # Discretionary ACL, then MANDATORY markings override (deny if uncleared).
        view, edit = self._evaluate(user, self._dataset_grants(dataset))
        if not self._has_clearance(user, dataset):
            return (False, False)
        return (view, edit)

    def can_view_dataset(self, user: Optional[User], dataset: str) -> bool:
        return self.dataset_permission(user, dataset)[0]

    def can_edit_dataset(self, user: Optional[User], dataset: str) -> bool:
        return self.dataset_permission(user, dataset)[1]

    def viewable_datasets(self, user: Optional[User], names: list[str]) -> set[str]:
        return {n for n in names if self.can_view_dataset(user, n)}

    # -- composed object-type access ------------------------------------------

    def object_type_permission(
        self, user: Optional[User], object_type: str, backing_dataset: str
    ) -> tuple[bool, bool]:
        """Effective access to an object type = the ontology grant composed with
        the backing dataset's access. You must be able to view the backing
        dataset to view its objects (objects ARE the dataset rows), so this
        closes the gap where an ontology grant alone left the data readable via
        the dataset/query APIs. Ontology edit still needs ontology edit rights,
        but also requires view (which requires dataset view)."""
        dv, _ = self.dataset_permission(user, backing_dataset)
        ov, oe = self.permission(user, object_type)
        view = dv and ov
        edit = view and oe
        return (view, edit)

    # -- row-level security & column masking ----------------------------------

    def dataset_policy(self, dataset: str) -> Optional[DatasetPolicy]:
        raw = self.store.get_dataset_policy(dataset)
        if not raw:
            return None
        return DatasetPolicy(
            dataset=dataset,
            row_policy=raw.get("row_policy"),
            column_masks=raw.get("column_masks", []),
        )

    def apply_table_policy(
        self, user: Optional[User], dataset: str, table: "pa.Table"
    ) -> "pa.Table":
        """Filter rows and mask columns of ``table`` for ``user`` per the dataset's
        policy. Admins (and no-policy datasets) pass through unchanged. This is the
        single choke point applied by the row API, the SQL workbench, and ontology
        object materialization — so RLS can't be bypassed through any read path."""
        if user is None:
            return table.slice(0, 0)
        if user.role == Role.admin:
            return table
        policy = self.dataset_policy(dataset)
        if policy is None:
            return table
        groups = self._user_groups(user.username)
        if policy.row_policy is not None:
            table = self._filter_rows(table, policy.row_policy, user, groups)
        for mask in policy.column_masks:
            table = self._mask_column(table, mask, user, groups)
        return table

    def has_dataset_policy(self, dataset: str) -> bool:
        return self.store.get_dataset_policy(dataset) is not None

    def row_policy_fn(self, user: Optional[User], dataset: str):
        """A ``(table)->table`` filter for the row API, or None when nothing needs
        filtering (admin, or the dataset has no policy) so the caller can fast-path."""
        if user is None or user.role == Role.admin:
            return None
        if not self.has_dataset_policy(dataset):
            return None
        return lambda t: self.apply_table_policy(user, dataset, t)

    def query_policy_fn(self, user: Optional[User]):
        """A ``(dataset, table)->table`` filter for the SQL workbench / ontology,
        or None for admins (no filtering)."""
        if user is not None and user.role == Role.admin:
            return None
        return lambda ds, t: self.apply_table_policy(user, ds, t)

    def per_dataset_policy_fn(self, user: Optional[User]):
        """A ``(dataset) -> Optional[(table)->table]`` resolver for the SQL
        workbench: None per dataset means no filtering is needed, so the
        catalog can register it as a lazy (out-of-core) scan instead of
        materializing it through the policy."""
        if user is not None and user.role == Role.admin:
            return lambda ds: None

        def for_dataset(ds: str):
            if user is None:
                return lambda t: t.slice(0, 0)  # fail closed
            if not self.has_dataset_policy(ds):
                return None
            return lambda t: self.apply_table_policy(user, ds, t)

        return for_dataset

    # -- pushdown planning ------------------------------------------------------
    #
    # The policy engine above operates on materialized tables, which costs
    # 3-4x at multi-million-row scale because it defeats scan pushdown. Where a
    # policy can be expressed as an Arrow filter + projection, it is applied
    # *inside* the Parquet scan instead: same enforcement, no materialization.
    # Anything not expressible (currently: hash masking, which has no Arrow
    # compute equivalent) reports "materialize" and takes the exact path.

    def arrow_policy_fn(self, user: Optional[User]):
        """A ``(dataset, arrow_schema) -> plan`` resolver, where plan is:

        * ``None`` — no filtering needed; scan the dataset as-is.
        * ``PolicyPlan(lazy=True, filter=expr|None, projection=dict|None)`` —
          push this filter/projection into the scan.
        * ``PolicyPlan(lazy=False, apply=fn)`` — materialize and call ``fn``.
        """
        if user is not None and user.role == Role.admin:
            return lambda ds, schema: None

        def plan_for(dataset: str, schema: "pa.Schema"):
            if user is None:
                return PolicyPlan(lazy=True, filter=pc.scalar(False))  # fail closed
            policy = self.dataset_policy(dataset)
            if policy is None:
                return None
            return self._plan(policy, dataset, schema, user)

        return plan_for

    def decide(self, dataset: str, columns: set[str], user: Optional[User]) -> "PolicyDecision":
        """Resolve *what* a policy does for this user, independent of how it
        will be executed.

        Splitting the decision from its rendering is what keeps enforcement
        honest across execution engines: there is one place that reads the
        policy and resolves subjects, and the Arrow and SQL renderers below
        are pure translations of its output. A new engine adds a renderer, not
        a second interpretation of the rules.
        """
        if user is None:
            return PolicyDecision(denies_all=True)
        if user.role == Role.admin:
            return PolicyDecision()
        policy = self.dataset_policy(dataset)
        if policy is None:
            return PolicyDecision()

        groups = self._user_groups(user.username)
        row_column: Optional[str] = None
        allowed_values: Optional[list[str]] = None

        if policy.row_policy is not None:
            rp = policy.row_policy
            if rp.column not in columns:
                return PolicyDecision(denies_all=True)  # fail closed
            allowed: set[str] = set()
            for rule in rp.rules:
                if self._subject_matches(
                    rule.subject_kind, rule.normalized_subject(), user, groups
                ):
                    allowed.update(str(v) for v in rule.values)
            if not allowed:
                return PolicyDecision(denies_all=True)
            row_column, allowed_values = rp.column, sorted(allowed)

        masks: list[tuple[str, MaskMode]] = []
        for mask in policy.column_masks:
            if mask.column not in columns:
                _reject_case_mismatch(mask.column, columns)
                continue
            if any(
                self._subject_matches(ex.subject_kind, ex.normalized_subject(), user, groups)
                for ex in mask.exempt
            ):
                continue  # exempt: real value
            masks.append((mask.column, mask.mode))

        return PolicyDecision(
            row_column=row_column, allowed_values=allowed_values, masks=masks
        )

    def _plan(self, policy, dataset: str, schema: "pa.Schema", user: User):
        """Render a decision as an Arrow filter + projection (managed data)."""
        decision = self.decide(dataset, set(schema.names), user)
        if decision.denies_all:
            return PolicyPlan(lazy=True, filter=pc.scalar(False))
        if not decision.applies:
            return None

        filter_expr = None
        if decision.row_column is not None:
            # NULLs are never "in" the set, so they are excluded — matching the
            # fill_null(False) fail-closed behavior of the table path.
            field = pc.field(decision.row_column)
            row_type = schema.field(decision.row_column).type
            if pa.types.is_nested(row_type):
                # Same refusal as the table path: a nested column has no text
                # form, so there is nothing for the allowlist to mean. Caught
                # here rather than as a cast failure deep inside the scan.
                raise PolicyRenderError(
                    f"Column {decision.row_column!r} has type {row_type}, which "
                    "has no text form, so a row policy on it cannot mean "
                    "anything definite. Point the row policy at a scalar column."
                )
            if row_type != pa.string():
                # Compare on the string rendering, exactly as the table path
                # does. This costs row-group pruning, but only for non-string
                # policy columns (tenant/region keys are normally strings).
                field = field.cast(pa.string())
            filter_expr = field.isin(decision.allowed_values)

        projection = None
        for column, mode in decision.masks:
            if mode == MaskMode.hash:
                # Arrow compute has no sha256, so hashing stays on the exact
                # table path. (The SQL renderer *can* express it — see
                # sql_policy_fn.)
                return PolicyPlan(
                    lazy=False,
                    apply=lambda t: self.apply_table_policy(user, dataset, t),
                )
            if projection is None:
                projection = {n: pc.field(n) for n in schema.names}
            projection[column] = (
                pc.scalar(None).cast(schema.field(column).type)
                if mode == MaskMode.null
                else pc.scalar("***")
            )

        if filter_expr is None and projection is None:
            return None
        return PolicyPlan(lazy=True, filter=filter_expr, projection=projection)

    # -- SQL rendering (federated data) -------------------------------------------

    def sql_policy_fn(self, user: Optional[User], dialect: SqlDialect = DUCKDB):
        """A ``(dataset, columns) -> SqlPolicy`` renderer for engines that speak
        SQL rather than Arrow — tables scanned in place.

        The same decision drives it, so a row policy is the same rule whether
        it filters a local Parquet scan, a remote Iceberg table or a ClickHouse
        one. ``dialect`` chooses only how it is *spelled*; the default keeps
        every existing caller on DuckDB unchanged.

        The returned renderer takes an optional per-call dialect, because one
        ``sql_policy_for`` is handed to a catalog that may hold datasets read
        by different engines. The reader asks for its own dialect and refuses
        the read if what comes back is rendered for another one. It also passes
        ``column_types`` — a name→Arrow-type map — without which a row filter
        or a hash mask is a refusal, because neither can be shown to mean the
        same thing on this engine as it does on the Arrow path.
        """

        def render(
            dataset: str,
            columns: list[str],
            for_dialect: Optional[SqlDialect] = None,
            column_types: Optional[dict] = None,
        ) -> "SqlPolicy":
            decision = self.decide(dataset, set(columns), user)
            return SqlPolicy.render(
                decision,
                columns,
                dialect=for_dialect or dialect,
                column_types=column_types,
            )

        return render


    def _filter_rows(
        self, table: "pa.Table", rp: RowPolicy, user: User, groups: set[str]
    ) -> "pa.Table":
        if rp.column not in table.column_names:
            return table.slice(0, 0)  # fail closed if the policy column is missing
        allowed: set[str] = set()
        for rule in rp.rules:
            if self._subject_matches(rule.subject_kind, rule.normalized_subject(), user, groups):
                allowed.update(str(v) for v in rule.values)
        if not allowed:
            return table.slice(0, 0)  # policy present but no rule grants this user rows
        try:
            col_as_str = pc.cast(table.column(rp.column), pa.string())
        except pa.lib.ArrowNotImplementedError as exc:
            # A list/struct/map column has no string form, so the reference
            # implementation cannot express this policy at all — and a policy
            # nothing validates is one the SQL renderers would be enforcing
            # unchecked. Refuse in the reference too, with a message that says
            # what is wrong instead of an Arrow cast error.
            raise PolicyRenderError(
                f"Column {rp.column!r} has type "
                f"{table.schema.field(rp.column).type}, which has no text form, "
                "so a row policy on it cannot mean anything definite. Point the "
                "row policy at a scalar column."
            ) from exc
        mask = pc.is_in(col_as_str, value_set=pa.array(sorted(allowed), pa.string()))
        # NULLs in the policy column are never "in" the set -> excluded (fail closed).
        mask = pc.fill_null(mask, False)
        return table.filter(mask)

    def _mask_column(
        self, table: "pa.Table", mask: ColumnMask, user: User, groups: set[str]
    ) -> "pa.Table":
        if mask.column not in table.column_names:
            # Same rule as decide(): a dropped column is fine, a case typo is
            # a refusal. Kept here too so the exact table path — which does not
            # go through decide() — cannot be the one that serves plaintext.
            _reject_case_mismatch(mask.column, table.column_names)
            return table
        for ex in mask.exempt:
            if self._subject_matches(ex.subject_kind, ex.normalized_subject(), user, groups):
                return table  # this user is exempt -> see the real value
        idx = table.column_names.index(mask.column)
        col = table.column(mask.column)
        n = len(col)
        if mask.mode == MaskMode.null:
            new = pa.nulls(n, type=col.type)
            field = table.schema.field(idx)
        elif mask.mode == MaskMode.redact:
            new = pa.array(["***"] * n, pa.string())
            field = pa.field(mask.column, pa.string())
        else:  # hash
            vals = col.to_pylist()
            new = pa.array(
                [
                    None if v is None else hashlib.sha256(str(v).encode()).hexdigest()[:16]
                    for v in vals
                ],
                pa.string(),
            )
            field = pa.field(mask.column, pa.string())
        return table.set_column(idx, field, new)

    # -- validation for the management API ------------------------------------

    def validate_grants(self, grants: list[Grant]) -> None:
        """Reject grants that reference unknown roles/groups or malformed rows.
        Raises ValueError with a clear message."""
        for g in grants:
            if not (g.can_view or g.can_edit):
                raise ValueError(
                    f"Grant for {g.subject_kind.value}:{g.subject!r} grants nothing "
                    "(set can_view and/or can_edit)"
                )
            if g.subject_kind == SubjectKind.everyone:
                continue
            subj = g.subject.strip()
            if not subj:
                raise ValueError(f"Grant of kind {g.subject_kind.value} needs a subject")
            if g.subject_kind == SubjectKind.role:
                if subj.lower() not in {r.value for r in Role}:
                    raise ValueError(f"Unknown role in grant: {subj!r}")
            elif g.subject_kind == SubjectKind.group:
                if not self.store.group_exists(subj):
                    raise ValueError(f"Unknown group in grant: {subj!r}")
            elif g.subject_kind == SubjectKind.user:
                if self.store.get_user(subj) is None:
                    raise ValueError(f"Unknown user in grant: {subj!r}")
