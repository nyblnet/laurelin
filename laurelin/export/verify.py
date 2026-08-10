"""Prove a reconstructed workspace governs identically. This is the product.

A tarball helper has a download button. A portability guarantee has a proof you
can look at — so the round trip is checked by recomputing, for every
(principal, dataset) pair, what the workspace actually *answers*, and comparing
digests.

Three enforcement paths are captured, not one, because a round trip can
preserve one renderer and break another:

* ``apply_table_policy`` — the exact, materialized reference;
* ``arrow_policy_fn`` (``_plan``) — the pushdown path, which takes a different
  branch for hash masks (``permissions.py:600``);
* ``sql_policy_fn`` rendered to DuckDB and executed — the path every
  scanned-at-source dataset uses.

Cells are hashed over Arrow **types plus values**, not values alone: a ``null``
mask preserves the column's Arrow type while a ``redact`` mask turns it into
string (``permissions.py:701``), and both can round-trip to the same Python
values, so a value-only comparison would call two different masks equal. See
``_result_digest`` for why raw IPC bytes — the obvious way to get types into a
digest — turned out to be the wrong medium.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Optional

import pyarrow as pa

from laurelin.core.db import MetadataStore
from laurelin.core.models import User
from laurelin.core.permissions import PermissionService, PolicyRenderError

# A cell that could not be computed records *why*, and the reason is part of
# the digest. "Refused" and "returned nothing" must never hash the same.
ERROR_PREFIX = "error:"


def _principal_key(user: Optional[User]) -> str:
    return user.username if user is not None else "anonymous"


def _row_digests(table: pa.Table) -> list[str]:
    """One digest per row, sorted, forming a comparable *multiset*.

    The whole-result digest can only ever say "same" or "different". The
    invariant this module exists to police is directional — an import may
    narrow and may never widen — and "the destination returned one row more" is
    not expressible as a digest comparison. Digesting rows individually makes
    containment mean what it says: an extra row at the destination fails it,
    a missing row does not.

    Duplicates are deliberately kept (see ``_multiset_gain``): under a mask two
    distinct source rows can present identically, so collapsing to a set would
    hide an extra row that happens to look like one already there.

    Sorted rather than positional because the three enforcement paths are free
    to return the same rows in different orders; ordering is checked separately
    by the whole-result digest.
    """
    return sorted(
        hashlib.sha256(repr(sorted(row.items())).encode()).hexdigest()
        for row in table.to_pylist()
    )


def _visible_digests(table: pa.Table) -> list[str]:
    """The set of (column, value) pairs this principal can actually read.

    This is the "same masked cells" half of the claim, and it catches the
    failure that row containment alone would miss: a destination that returns
    exactly the source's rows but with a mask *dropped*. Under the source's
    redact mask the pair is ``('ssn', '***')``; unmasked it is ``('ssn', '111')``
    — a pair that is not in the source's set, so containment fails.

    NULLs are excluded, so a ``null`` mask contributes nothing and can only ever
    shrink this set. That is the correct direction: revealing a value that was
    nulled adds a pair and is caught.
    """
    pairs = set()
    for row in table.to_pylist():
        for column, value in row.items():
            if value is None:
                continue
            pairs.add(hashlib.sha256(repr((column, value)).encode()).hexdigest())
    return sorted(pairs)


def _result_digest(table: pa.Table) -> str:
    """Hash a result's Arrow **types** and values, and nothing physical.

    The type half is load-bearing and is the reason this is not just
    ``to_pylist()``: a ``null`` mask preserves the column's Arrow type while a
    ``redact`` mask turns it into string (``permissions.py:701``), and both can
    round-trip to the same Python values. Digest the values alone and the two
    masks compare equal.

    Raw Arrow IPC bytes were the first attempt and are wrong in the other
    direction. Measured on this fixture: the materialized path, the pushdown
    path and the DuckDB path returned identical schemas and identical values
    and produced three different IPC digests — chunk boundaries, array offsets
    and a present-vs-absent all-valid validity buffer are all in those bytes.
    A fingerprint that changes when a row group splits is not a governance
    fingerprint.
    """
    fields = [(field.name, str(field.type)) for field in table.schema]
    payload = repr(fields).encode() + b"\n" + repr(table.to_pylist()).encode()
    return hashlib.sha256(payload).hexdigest()


def _to_table(scannable) -> pa.Table:
    """Normalize whatever a read path handed back into a Table.

    Three shapes reach here: a Table (materialized policy), a Dataset or
    Scanner (pushdown), and a RecordBatchReader — which is what newer DuckDB
    builds return from ``.arrow()``, and which silently broke the digest of the
    SQL path when this only handled the first two.
    """
    if isinstance(scannable, pa.Table):
        return scannable
    if hasattr(scannable, "read_all"):
        return scannable.read_all()
    return scannable.to_table()


def _safe(fn) -> str:
    try:
        return fn()
    except PolicyRenderError as exc:
        return f"{ERROR_PREFIX}refused:{type(exc).__name__}"
    except Exception as exc:  # a broken read is a fingerprint value, not a crash
        return f"{ERROR_PREFIX}{type(exc).__name__}"


def governance_fingerprint(
    store: MetadataStore,
    catalog,
    principals: list[Optional[User]],
    datasets: Optional[list[str]] = None,
    object_types: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """The decision matrix, as digests.

    ``object_types`` maps an ontology type's api_name to its backing dataset —
    passed in rather than loaded here so this module never depends on the
    ontology loader, and so a caller can fingerprint a subset.
    """
    perms = PermissionService(store)
    names = sorted(datasets) if datasets is not None else sorted(
        d.name for d in store.list_datasets()
    )
    infos = {name: store.get_dataset(name) for name in names}

    cells: dict[str, dict[str, Any]] = {}
    for user in principals:
        who = _principal_key(user)
        arrow_plan_for = perms.arrow_policy_fn(user)
        sql_render = perms.sql_policy_fn(user)
        for name in names:
            info = infos.get(name)
            can_view, can_edit = perms.dataset_permission(user, name)
            cell: dict[str, Any] = {
                "can_view": can_view,
                "can_edit": can_edit,
                "effective_markings": store.get_effective_markings(name),
            }
            columns = _columns_of(catalog, info, name)
            decision = _safe(lambda: _decision_text(perms, name, columns, user))
            cell["decision"] = decision
            cell["sql"] = _safe(lambda: _sql_text(sql_render, name, columns))
            if info is not None and info.kind == "managed" and columns:
                readers = {
                    "table": lambda: perms.apply_table_policy(
                        user, name, catalog.read(name)
                    ),
                    "arrow": lambda: _to_table(
                        catalog.scan_for(name, plan_for=arrow_plan_for)
                    ),
                    "duckdb": lambda: _duckdb_table(
                        catalog, name, sql_render, columns
                    ),
                }
                rows: dict[str, Any] = {}
                visible: dict[str, Any] = {}
                for path, read in readers.items():
                    # Read once per path and derive all three views of it. The
                    # whole-result digest keeps ordering and Arrow types in
                    # scope; the row and cell sets are what make containment a
                    # subset check instead of a "these differ somehow" check.
                    result = _safe_table(read)
                    if isinstance(result, str):
                        cell[path] = rows[path] = visible[path] = result
                        continue
                    cell[path] = _result_digest(result)
                    rows[path] = _row_digests(result)
                    visible[path] = _visible_digests(result)
                cell["rows"] = rows
                cell["visible"] = visible
            cells[f"{who}|{name}"] = cell

    types: dict[str, list[bool]] = {}
    for user in principals:
        who = _principal_key(user)
        for api_name, backing in sorted((object_types or {}).items()):
            view, edit = perms.object_type_permission(user, api_name, backing)
            types[f"{who}|{api_name}"] = [view, edit]

    return {
        "principals": [_principal_key(u) for u in principals],
        "datasets": names,
        "cells": cells,
        "object_types": types,
    }


def _columns_of(catalog, info, name: str) -> list[str]:
    if info is None:
        return []
    try:
        if info.kind == "managed":
            return list(catalog.arrow_dataset(name).schema.names)
        version = catalog.store.get_version(name)
        return [c.name for c in (version.schema_ if version else [])]
    except Exception:
        return []


def _decision_text(perms: PermissionService, name: str, columns: list[str], user) -> str:
    decision = perms.decide(name, set(columns), user)
    return (
        f"denies_all={decision.denies_all} row={decision.row_column} "
        f"values={decision.allowed_values} "
        f"masks={[(c, m.value) for c, m in decision.masks]}"
    )


def _sql_text(render, name: str, columns: list[str]) -> str:
    policy = render(name, columns)
    return f"{policy.select_list} WHERE {policy.where} {policy.params}"


def _safe_table(fn):
    """Like ``_safe``, but the success value is a Table rather than a string.

    A read that refuses is a fingerprint *value* — "refused" and "returned
    nothing" must never compare equal — so the error string is returned and the
    caller stores it in place of the row set.
    """
    try:
        return fn()
    except PolicyRenderError as exc:
        return f"{ERROR_PREFIX}refused:{type(exc).__name__}"
    except Exception as exc:
        return f"{ERROR_PREFIX}{type(exc).__name__}"


def _duckdb_table(catalog, name: str, render, columns: list[str]) -> pa.Table:
    """Render the decision to SQL and actually run it.

    Rendering without executing would check the string, not the answer — and
    the string is the part that is easy to get right.
    """
    import duckdb

    table = catalog.read(name)
    types = {field.name: field.type for field in table.schema}
    policy = render(name, columns, None, types)
    conn = duckdb.connect()
    try:
        conn.register("t", table)
        sql = f"SELECT {policy.select_list} FROM t WHERE {policy.where}"
        return _to_table(conn.execute(sql, policy.params).arrow())
    finally:
        conn.close()


def _summarize(value: Any) -> str:
    """Describe a row/cell set in a few characters rather than a few kilobytes."""
    if isinstance(value, list):
        return f"{len(value)} entries"
    if value is None:
        return "not computed (dataset unreadable)"
    return repr(value)


def _describe(left: Any, right: Any) -> list[str]:
    """Say *what* moved, in a line a human can act on.

    The raw cells hold hundreds of hashes once row and cell sets are in them,
    and a failure that prints two of those is unreadable — which in practice
    means unread. Rows and visible cells are summarized as counts gained and
    lost, and "gained" is the word that matters: it is the widening.
    """
    notes: list[str] = []
    for field in ("can_view", "can_edit", "effective_markings", "decision", "sql"):
        if left.get(field) != right.get(field):
            notes.append(f"{field}: {left.get(field)!r} -> {right.get(field)!r}")
    for field in ("rows", "visible"):
        for path in ("table", "arrow", "duckdb"):
            before = left.get(field, {}).get(path)
            after = right.get(field, {}).get(path)
            if before == after:
                continue
            if not isinstance(before, list) or not isinstance(after, list):
                # One side could not read the dataset at all — a missing part
                # file leaves the schema unreadable, so the cell has no row set
                # rather than an empty one. Summarize; printing the raw hash
                # lists here made a real failure unreadable when this was first
                # exercised against an exporter that dropped appended parts.
                notes.append(
                    f"{field}.{path}: {_summarize(before)} -> {_summarize(after)}"
                )
                continue
            gained = len(set(after) - set(before))
            lost = len(set(before) - set(after))
            notes.append(
                f"{field}.{path}: {len(before)} -> {len(after)} "
                f"(+{gained} gained, -{lost} lost)"
            )
    for path in ("table", "arrow", "duckdb"):
        if left.get(path) != right.get(path) and not any(
            n.startswith(("rows." + path, "visible." + path)) for n in notes
        ):
            notes.append(f"{path}: result digest differs (ordering or Arrow type)")
    return notes


def diff_fingerprints(source: dict, target: dict) -> list[dict]:
    """Every cell where the two workspaces answer differently."""
    out: list[dict] = []
    keys = sorted(set(source.get("cells", {})) | set(target.get("cells", {})))
    for key in keys:
        left = source.get("cells", {}).get(key)
        right = target.get("cells", {}).get(key)
        if left == right:
            continue
        if left is None or right is None:
            out.append({"cell": key, "changes": ["cell missing on one side"]})
            continue
        out.append({"cell": key, "changes": _describe(left, right)})
    for key in sorted(set(source.get("object_types", {})) | set(target.get("object_types", {}))):
        left = source.get("object_types", {}).get(key)
        right = target.get("object_types", {}).get(key)
        if left != right:
            out.append({"object_type": key, "source": left, "target": right})
    return out


def _multiset_gain(before: list[str], after: list[str]) -> int:
    """How many rows the destination returned *in excess of* the source.

    A multiset, not a set, and the distinction is not academic — it was found
    by the negative control in
    ``test_the_proof_catches_a_destination_that_returns_one_extra_row``. Under
    a redact-and-null mask the row ``('eu', '777', 7.70)`` presents to a viewer
    as ``('eu', '***', None)``, which is byte-identical to the masked form of a
    row the source already had. Set difference therefore reported *nothing
    gained* for a destination that handed the principal an extra row.

    Multiplicity is itself disclosure: "there are four EU transactions" is a
    fact the source did not release. So the comparison counts occurrences.
    """
    counts = Counter(before)
    gained = 0
    for digest, seen in Counter(after).items():
        gained += max(0, seen - counts.get(digest, 0))
    return gained


def _set_gain(before: list[str], after: list[str]) -> int:
    """Distinct (column, value) pairs the destination revealed and the source did not.

    Distinctness is the right notion here, unlike for rows: the question a mask
    answers is *which values* a principal can read, and seeing the same
    plaintext twice is not a second disclosure.
    """
    return len(set(after) - set(before))


def widenings(source_cell: Optional[dict], target_cell: Optional[dict]) -> list[str]:
    """Every way this target cell is *wider* than its source cell.

    Narrowing is legal and must not be reported: an unbound import is expected
    to answer with less. So this is deliberately asymmetric — a subset check on
    the row and cell sets, an implication check on the booleans, and nothing at
    all on the whole-result digests, which cannot distinguish "narrower" from
    "different" and would therefore forbid the legal direction.

    A target row absent from the source is the headline case. A target row that
    *matches* a source row but carries a value the source masked shows up in
    ``visible`` — the mask changes the value, so the pair is new.
    """
    if target_cell is None:
        return []
    if source_cell is None:
        if target_cell.get("can_view") or target_cell.get("can_edit"):
            return ["the destination answers for a cell the source does not have"]
        return []

    out: list[str] = []
    for flag in ("can_view", "can_edit"):
        if target_cell.get(flag) and not source_cell.get(flag):
            out.append(f"{flag}: False at the source, True at the destination")

    for field, gain in (("rows", _multiset_gain), ("visible", _set_gain)):
        source_paths = source_cell.get(field) or {}
        target_paths = target_cell.get(field) or {}
        for path, after in target_paths.items():
            before = source_paths.get(path)
            if not isinstance(after, list):
                continue
            if not isinstance(before, list):
                # The source could not read it at all and the destination
                # could. That is the widest possible move.
                if after:
                    out.append(
                        f"{field}.{path}: source recorded {before!r}, "
                        f"destination returned {len(after)}"
                    )
                continue
            gained = gain(before, after)
            if gained:
                out.append(
                    f"{field}.{path}: {gained} not present at the source "
                    f"({len(before)} -> {len(after)})"
                )
    return out


def contains(source_cell: Optional[dict], target_cell: Optional[dict]) -> bool:
    """Whether a target cell is no wider than its source cell."""
    return not widenings(source_cell, target_cell)
