"""What a flow author is allowed to read, and what the flow's output inherits.

This module was written **stricter than the Python transform path,
deliberately** — measured on this tree at the time: an editor who could not
view ``secret_ds`` could write ``@sql_transform(query="SELECT * FROM s")``,
build it, and read every row of the copy — unfiltered, unmasked, and
world-readable. The whole point of the no-code builder is to make *every
analyst* an editor, and inheriting that hole would have handed it to everyone
in the business.

That Python-path gap is now closed for API-authored pipeline files:
``Builder._check_input_entitlement`` re-checks the file's recorded author
(``pipeline_authors``) against every input at every build, resolving the
author through this module's ``_author_user`` so the two paths cannot drift.
Flows remain stricter in one dimension: the mask check here is
column-granular, where a Python transform's touched columns are unknowable
and any masked input refuses. Disk-authored ``.py`` remains operator-trusted
— see ``MetadataStore.set_pipeline_author``.

Where this runs
---------------
Three places, and the third is the one that matters:

1. ``PUT /flows/{name}`` — the author gets a refusal as an authoring error.
2. ``POST /flows/preview`` — before compiling.
3. Inside ``Builder``, for ``kind == "flow"``, immediately before execution.

(3) is not redundant with (1). A scheduled build has no request user, and a
grant or a policy can be added *after* a flow was authored. The flow file
records its ``author`` and the build re-evaluates **that author's** rights, so
a flow whose author is later disabled or deleted stops building — fail-closed,
with the remedy (an admin reassigns the author) in the refusal.
"""

from __future__ import annotations

from typing import Iterable, Optional

from laurelin.core.models import Role, SubjectKind, User
from laurelin.core.permissions import confusable_identifier
from laurelin.transforms.flow_ir import FlowDef, FlowRefused


def referenced_columns(flow: FlowDef) -> set[str]:
    """Every column name the flow mentions anywhere, at any nesting depth.

    Used for the column-mask check. Soundness argument: a source node emits
    ``SELECT *``, so a source column reaches the flow's result only if its name
    survives to the result schema, or it was renamed (which mentions it), or it
    was read by an expression, key, sort or aggregate (which mentions it).
    Dropping a column — by ``select`` or by omission from an ``aggregate`` — is
    the only way it disappears, and dropping is safe. So "mentioned anywhere ∪
    present in the result" is a superset of "could have been read", which is
    the direction that has to be conservative.
    """
    names: set[str] = set()

    def walk_expr(e: dict) -> None:
        if e["t"] == "col":
            names.add(e["name"])
        elif e["t"] == "op":
            for a in e["args"]:
                walk_expr(a)

    for n in flow.nodes:
        p = n.params
        if n.kind == "filter":
            walk_expr(p["predicate"])
        elif n.kind == "select":
            names.update(p["columns"])
        elif n.kind == "rename":
            names.update(pair["from"] for pair in p["pairs"])
        elif n.kind == "derive":
            walk_expr(p["expr"])
        elif n.kind == "cast":
            names.add(p["column"])
        elif n.kind == "join":
            for k in p["keys"]:
                names.add(k["left"])
                names.add(k["right"])
        elif n.kind == "aggregate":
            names.update(p["group_by"])
            names.update(a["column"] for a in p["aggs"] if a["column"])
        elif n.kind == "dedupe":
            names.update(p["keys"])
            names.update(e["column"] for e in p["order_by"])
        elif n.kind == "sort":
            names.update(e["column"] for e in p["by"])
    for e in flow.expectations:
        if e.column:
            names.add(e.column)
    return names


def _author_user(store, author, what: str = "flow") -> User:
    """Resolve the flow's (or pipeline's) author to a live user, or refuse.

    ``author`` is either a ``User`` — the request path already authenticated
    one, and re-looking it up would be a chance to resolve to a *different*
    person — or a username, which is all the build path has, because a
    scheduled build has no request user.

    A flow builds *as its author*, so an author who no longer exists must not
    fall back to "no user": ``PermissionService`` treats ``None`` as fail-closed
    for row policy, but ``can_view_dataset(None, …)`` is a different question,
    and guessing here is how a governance hole gets built. Refuse, and put the
    remedy in the sentence.

    ``what`` names the artifact in the refusal ("flow" or "pipeline"). The
    Builder's input-entitlement check for API-authored Python/SQL transforms
    resolves *its* recorded author through this same function, deliberately:
    two resolutions of "who does this build run as" must not drift.
    """
    if isinstance(author, User):
        return author
    if not author:
        raise FlowRefused(
            f"This {what} has no recorded author, so Laurelin cannot work out "
            "whose read access its build should be checked against. Re-save "
            "it, or ask an administrator to set its author.",
            field="author",
        )
    found = store.get_user(author)
    if found is not None:
        return found
    if store.count_users() == 0:
        # A workspace with no user rows at all is one running without
        # authentication (`--no-auth`, the CLI, the tutorials). There is no
        # identity to check against and no ACL to enforce, so refusing here
        # would make flows unbuildable in exactly the mode where the whole
        # question is moot. Synthesised as an admin because that is what
        # `--no-auth` already grants every request.
        return User(id="", username=str(author) or "anonymous", role=Role.admin)
    raise FlowRefused(
        f"This {what}'s author {author!r} no longer exists, so its build "
        "cannot be checked against anyone's read access. Ask an "
        "administrator to reassign it to a current user.",
        field="author",
    )


def check_flow_sources(store, perms, author, flow: FlowDef) -> None:
    """View rights on every source dataset. **Must run before compilation.**

    Ordering is load-bearing. ``flow_compile.resolve_column`` names the
    available columns of a dataset in its refusal — deliberately, because that
    is the single most useful thing to tell an author who mistyped one — and
    those column names are a schema the caller may not be entitled to. Checking
    view rights first means the compiler is only ever pointed at datasets the
    author may read.
    """
    user = _author_user(store, author)
    who = user.username
    for dataset in flow.source_datasets():
        if store.get_dataset(dataset) is None:
            raise FlowRefused(
                f"This flow reads {dataset!r}, which does not exist in this "
                "workspace.",
                field="dataset",
            )
        if not perms.can_view_dataset(user, dataset):
            raise FlowRefused(
                f"{who!r} cannot read {dataset!r}, so this flow cannot use "
                "it as a source. Ask an administrator for access to that "
                "dataset, or use a different source.",
                field="dataset",
            )


def check_flow_output(store, perms, author, flow: FlowDef) -> None:
    """Write rights on the dataset this flow *replaces*, when it already exists.

    Nothing checked this, and two separate attacks came through the gap:

    * **Overwriting a dataset you cannot even read.** ``secret_ds`` granted to
      root alone; an ordinary editor PUT a flow named ``secret_ds`` whose only
      source was a dataset she could read, built it, and destroyed the
      contents of a dataset she still returns 403 on. The flow surface makes
      this a two-click gesture — a flow's name *is* its output dataset's name —
      where the Python path at least requires writing a file.

    * **Laundering a classification marking by re-pointing a source.** A flow
      ``mid`` read a `secret`-marked dataset, so ``mid`` and everything
      downstream inherited the marking. An uncleared editor could not name the
      marked dataset as a source (`check_flow_sources` refuses him), but he
      *could* edit the flow to read a public one instead: the build then
      replaced the lineage edge, `recompute_all_markings` saw no marked
      upstream, and every downstream dataset was declassified while still
      holding the classified rows. Requiring view rights on the flow's own
      output closes it, because a dataset carrying a marking he has no
      clearance for is one he cannot view.

    ``can_edit`` is required as well as ``can_view``: replacing a dataset's
    contents is a write, and it is the strictest of the two that ought to
    govern it.

    A dataset that does not exist yet is not checked — there is nothing to
    authorize, and `catalog._NAME_RE` plus the duplicate-producer rule already
    govern which names may be claimed.
    """
    if store.get_dataset(flow.output) is None:
        return
    user = _author_user(store, author)
    if perms.can_view_dataset(user, flow.output) and perms.can_edit_dataset(
        user, flow.output
    ):
        return
    raise FlowRefused(
        f"{user.username!r} cannot change the dataset {flow.output!r}, which "
        "already exists and which this flow would replace. Give the flow a "
        "different name, or ask an administrator for access to that dataset.",
        field="output",
    )


def check_flow_governance(
    store,
    perms,
    author,
    flow: FlowDef,
    output_columns: Optional[Iterable[str]] = None,
    authoring: bool = False,
) -> None:
    """The full check: view rights, then row policies, then column masks.

    ``output_columns`` is the compiled result schema. Pass it whenever it is
    available (it always is at PUT and at build time); omitting it makes the
    mask check fall back to "referenced only", which is *less* conservative, so
    the callers that can supply it must.

    ``authoring`` adds the checks that belong to *choosing* a flow's shape
    rather than to running it — currently just
    `check_output_not_wider_than_sources`, whose reasons for being an authoring
    check and not a build one are in its own docstring. `PUT` and eject pass
    it; the Builder does not.
    """
    check_flow_sources(store, perms, author, flow)
    check_flow_output(store, perms, author, flow)
    if authoring:
        check_output_not_wider_than_sources(store, flow)

    touched = referenced_columns(flow)
    if output_columns is not None:
        touched |= set(output_columns)

    for dataset in flow.source_datasets():
        policy = perms.dataset_policy(dataset)
        if policy is None:
            continue

        if policy.row_policy is not None:
            # Refused outright, whatever the flow projects. A transform's
            # output is a new dataset with no policy of its own, so *any* read
            # of a row-policied input launders its rows. There is no policy
            # algebra that survives an aggregate — "rows where region='us'"
            # has no meaning once the rows have been summed — and inventing
            # one silently would be worse than refusing.
            raise FlowRefused(
                f"{dataset!r} has a row policy, which controls which rows each "
                "person may see. A flow's result is a new dataset without that "
                "policy, so building one from it would hand every row to "
                "anyone who can read the result. Ask an administrator for a "
                "copy of this dataset you may read in full, or use a different "
                "source.",
                field="dataset",
            )

        folded_touched = {confusable_identifier(c): c for c in touched}
        for mask in policy.column_masks:
            # Matched on the fold, not with `in`, and `permissions.py` had
            # already paid for this lesson: `_reject_case_mismatch` exists
            # precisely so a mask authored as 'SSN' against a column 'ssn'
            # fails CLOSED rather than serving plaintext, and every read path
            # raises `PolicyRenderError` for one. This check compared with a
            # plain `in`, so it disagreed with `decide()` — and the disagreement
            # ran the wrong way. Measured: with a mask spelled 'SSN', 'ssn ' or
            # 'ｓsn' against a real column 'ssn', nobody but an admin could read
            # the dataset at all, and a flow copied it out verbatim into a
            # world-readable one. Two checks of the same question must share
            # one answer; this is that one.
            hit = folded_touched.get(confusable_identifier(mask.column))
            if hit is not None:
                raise FlowRefused(
                    f"Column {mask.column!r} of {dataset!r} is masked, and this "
                    f"flow reads it"
                    + (f" (as {hit!r})" if hit != mask.column else "")
                    + ". The flow's result would contain the real values with "
                    "no mask on them. Remove that column from the flow, or ask "
                    "an administrator for access to it.",
                    field="dataset",
                )


def _viewers(store, dataset: str) -> set[tuple[str, str]]:
    """The (kind, subject) pairs a dataset's grants let view it.

    Admins bypass grants entirely, so this is the whole population that a grant
    list decides about.
    """
    return {
        (g["subject_kind"], g["subject"])
        for g in store.grants_for_dataset(dataset)
        if g.get("can_view")
    }


def check_output_not_wider_than_sources(store, flow: FlowDef) -> None:
    """Refuse to *point* a flow at an output already shared beyond its sources.

    ``restrict_output_to_author`` cannot cover this case, and must not try. Its
    early-out — leave existing grants alone — is deliberate and right: a
    rebuild must not undo an administrator's decision to widen access to a
    flow's own output. The hole was that it read *any* pre-existing grant as
    that decision.

    Measured: ``secret_ds`` granted to alice only; ``shared_report`` an
    ordinary team dataset granted to alice and bob. Alice saved a flow named
    ``shared_report`` — a flow's name *is* its output dataset's name, so this
    is one text field — reading ``secret_ds``. The early-out fired, the API
    answered ``output_will_be_restricted: false`` so neither it nor the UI
    warned, and bob, who is 403 on ``secret_ds``, read every row of it. The
    grants being protected there were not an administrator's decision about a
    derived dataset; they were the ACL of whatever dataset the author chose to
    overwrite.

    So the two cases are separated by *when*, which is the thing that actually
    distinguishes them. Choosing the output is an authoring act, checked here,
    at ``PUT`` and at eject. Widening a flow's output afterwards is an
    administrator's act on a dataset whose provenance they can see, and is left
    alone — including by the build.
    """
    restricted_sources = [
        ds for ds in flow.source_datasets() if store.grants_for_dataset(ds)
    ]
    if not restricted_sources:
        return
    output_viewers = _viewers(store, flow.output)
    if not output_viewers:
        # No grants at all: `restrict_output_to_author` will narrow it to the
        # author at build time. That is the intended path, not a refusal.
        return
    for ds in restricted_sources:
        offenders = sorted(
            subject for kind, subject in output_viewers - _viewers(store, ds)
        )
        if offenders:
            raise FlowRefused(
                f"{flow.output!r} is already readable by "
                + ", ".join(repr(o) for o in offenders[:5])
                + (f" and {len(offenders) - 5} others" if len(offenders) > 5 else "")
                + f", who cannot read {ds!r}. Building this flow would hand "
                f"them its contents. Give the flow a different name, or ask an "
                f"administrator to reconcile the two datasets' access.",
                field="output",
            )


def restrict_output_to_author(store, flow: FlowDef, author: str) -> bool:
    """Grant the flow's output to its author alone, if any source is restricted.

    Returns whether a grant was written.

    **Not the union of the inputs' grants.** With inputs A (granted to alice)
    and B (granted to bob), the union is {alice, bob} — and alice would gain
    access to B's data through a dataset she had no rights to. "Derived from
    something restricted ⇒ restricted to the person who derived it" is the only
    rule here that fails closed and can be explained in one sentence in the UI.

    Existing grants are never replaced: a rebuild must not undo an
    administrator's decision to widen access to a flow's output. The case that
    early-out used to *also* wave through — pointing a flow at somebody else's
    already-shared dataset — is refused at authoring time instead, by
    `check_output_not_wider_than_sources`.
    """
    if store.grants_for_dataset(flow.output):
        return False
    restricted = any(
        store.grants_for_dataset(ds) for ds in flow.source_datasets()
    )
    if not restricted:
        # Parity with today: a flow over unrestricted inputs produces an
        # unrestricted output.
        return False
    store.set_grants_for_dataset(
        flow.output,
        [{
            "subject_kind": SubjectKind.user.value,
            "subject": author,
            "can_view": True,
            "can_edit": True,
        }],
    )
    return True


def output_will_be_restricted(store, flow: FlowDef) -> bool:
    """Whether building this flow would restrict its output to the author.

    Read-only; the UI shows this on the header so the consequence is visible
    before the build rather than discovered after it.
    """
    if store.grants_for_dataset(flow.output):
        return False
    return any(store.grants_for_dataset(ds) for ds in flow.source_datasets())


def masked_columns_for(perms, user: Optional[User], dataset: str) -> set[str]:
    """Columns of ``dataset`` that are masked *for this caller*.

    Preview runs as the requesting user and therefore sees masks applied:
    ``redact`` renders as the string ``'***'``, so ``sum(masked_col)`` errors or
    returns nonsense for exactly the authors who most need the preview. The
    route uses this to say "this column is masked for you; the build will see
    the real values" instead of surfacing a DuckDB binder error.

    ``PermissionService`` exposes only ``arrow_policy_fn`` / ``sql_policy_fn``,
    neither of which answers this question, so it is answered here from
    ``decide()`` — the same resolver both renderers use, so this cannot drift
    into a second interpretation of the rules.
    """
    if user is not None and user.role == Role.admin:
        return set()
    policy = perms.dataset_policy(dataset)
    if policy is None:
        return set()
    columns = {m.column for m in policy.column_masks}
    if not columns:
        return set()
    probe = set(columns)
    if policy.row_policy is not None:
        # `decide()` fails closed — denies everything, masks included — when
        # the row-policy column is absent from the projection it is asked
        # about. Right for a read; wrong for this question, which is only
        # "which columns are masked". Measured through `/explore/preview` (the
        # first caller that reaches a row-policied dataset: `/flows/preview`
        # refuses those outright before ever asking): a dataset with a row
        # policy AND a redact mask served `***` in the rows while
        # `masked_columns` said nothing was masked, so the UI offered the
        # masked column in its measure pickers. Include the row column in the
        # probe so the decision is about the masks.
        probe.add(policy.row_policy.column)
    decision = perms.decide(dataset, probe, user)
    return {column for column, _mode in decision.masks}
