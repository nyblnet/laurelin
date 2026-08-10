"""OntologyService: materialize objects (base data + edit overlay), traverse
links, and apply write-back actions.

Three paths answer the same question, fastest first:

1. the **operational object store** (``laurelin/ontology/store.py``) — a
   materialization of current object state that an edit *upserts* rather than
   invalidates, so a read costs a key lookup no matter how long the edit log is;
2. **pushdown into DuckDB** — the edit log replayed as registered Arrow tables
   and merged with the Parquet scan in SQL;
3. **in-memory materialization** — base rows with the overlay applied in Python.

(2) and (3) both read the edit log directly, so both are always correct. That
is what makes (1) safe to skip: a store that cannot prove it has applied every
committed edit returns nothing and the read falls through. Slower, never wrong.

(3) also remains the oracle the other two are tested against: if the three ever
disagree about which objects exist, that is the bug, and it is cheaper to catch
in a test than in production.
"""

from __future__ import annotations

import json
import math
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional

import duckdb
import pyarrow as pa

from laurelin.catalog import DatasetCatalog
from laurelin.core import limits
from laurelin.core.config import Workspace
from laurelin.core.db import (
    RANK_BODY_ONLY,
    SEARCH_TOTAL_CAP,
    MetadataStore,
    count_sql,
)
from laurelin.core.models import (
    EditKind,
    ObjectEdit,
    ObjectTypeDef,
    OntologyDef,
)
from laurelin.ontology.store import (
    MetadataObjectStore,
    ObjectRow,
    canonical_props,
    created_ord,
    definition_fingerprint,
    digest_hex,
    digest_of_rows,
)

# Cap on objects returned for one link traversal (the in-memory path was
# unbounded; a cap keeps a fan-out link from materializing a whole dataset).
_LINK_LIMIT = 10_000

_TRUE_STRINGS = {"true", "1"}
_FALSE_STRINGS = {"false", "0"}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _result(objects: list[dict], total: int, search: Optional[str]) -> dict:
    """Shape one page of objects.

    ``total`` saturates at SEARCH_TOTAL_CAP when a search term is present, so
    the caller is told whether the number is exact or a floor rather than
    having to guess. Browsing is never capped.
    """
    capped = bool(search) and total >= SEARCH_TOTAL_CAP
    return {"objects": objects, "total": min(total, SEARCH_TOTAL_CAP) if capped else total,
            "total_capped": capped}


def _sortable(value: Any):
    """A key that orders numbers naturally and never raises on mixed types."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _compute(spec: dict, members: list[dict]) -> Any:
    op, prop = spec["op"], spec["property"]
    if prop is None:
        return len(members)
    values = [m.get(prop) for m in members if m.get(prop) is not None]
    if op == "count":
        return len(values)
    if op == "count_distinct":
        return len({str(v) for v in values})
    if not values:
        return None
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not numbers:
        return None
    if op == "sum":
        return sum(numbers)
    if op == "avg":
        return sum(numbers) / len(numbers)
    if op == "median":
        ordered = sorted(numbers)
        mid = len(ordered) // 2
        return (ordered[mid] if len(ordered) % 2
                else (ordered[mid - 1] + ordered[mid]) / 2)
    raise AssertionError(f"unhandled aggregation {op!r}")  # pragma: no cover


def _coerce_parameter(name: str, value: Any, type_name: str) -> Any:
    """Coerce a raw parameter value to its declared type; ValueError if impossible."""
    try:
        if type_name == "integer":
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, float) and not value.is_integer():
                raise ValueError  # don't silently truncate 1.5 -> 1
            if isinstance(value, str) and "." in value:
                value = float(value)
                if not value.is_integer():
                    raise ValueError
            return int(value)
        if type_name == "float":
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if type_name == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in _TRUE_STRINGS:
                    return True
                if lowered in _FALSE_STRINGS:
                    return False
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(
            f"Parameter {name!r} has invalid value {value!r} for type {type_name!r}"
        ) from None
    return value


class OntologyService:
    """Queries and actions over ontology objects backed by catalog datasets."""

    def __init__(
        self,
        workspace: Workspace,
        catalog: DatasetCatalog,
        store: MetadataStore,
        ontology: OntologyDef,
        policy=None,
        policy_for=None,
        plan_for=None,
        object_store=None,
    ):
        self.workspace = workspace
        self.catalog = catalog
        self.store = store
        self.ontology = ontology
        # Where current object state is materialized. Defaults to the metadata
        # store, which needs no new dependency and puts the edit log and its
        # materialization in one transaction; a StarRocks store swaps in here.
        self.object_store = object_store or MetadataObjectStore(store)
        # Optional (dataset, pa.Table) -> pa.Table row-level-security / masking
        # transform, bound to the requesting user. Objects ARE dataset rows, so
        # applying it here keeps RLS from being bypassed via the ontology API.
        self.policy = policy
        # Optional (dataset) -> Optional[(pa.Table) -> pa.Table]: the same
        # enforcement, resolved per dataset. When it reports that a backing
        # dataset needs no filtering for this user, object queries can run
        # entirely in DuckDB instead of materializing the dataset in Python.
        self.policy_for = policy_for
        # Optional (dataset, schema) -> PolicyPlan: lets the same enforcement
        # be pushed into the Parquet scan, so object queries stay fast for
        # users who have row-level security on the backing dataset.
        self.plan_for = plan_for

    def _policy_for_dataset(self, dataset: str):
        """The row/column transform to apply to ``dataset`` for this user, or
        None if none is needed."""
        if self.policy_for is not None:
            return self.policy_for(dataset)
        return self.policy and (lambda t: self.policy(dataset, t))

    # -- definitions ----------------------------------------------------------

    def list_object_types(self) -> list[ObjectTypeDef]:
        return list(self.ontology.object_types)

    def _require_object_type(self, type_name: str) -> ObjectTypeDef:
        ot = self.ontology.object_type(type_name)
        if ot is None:
            raise KeyError(f"Unknown object type: {type_name!r}")
        return ot

    # -- materialization --------------------------------------------------------

    # A read-consistent snapshot of one object type's inputs, or None.
    #
    # ``reindex`` reads three things — the ordinals, the watermark and the
    # objects — and read each on its own connection. A create committing
    # between the first read and the third appeared in the objects and not in
    # the ordinals, and the rebuild died on `KeyError`. That endpoint is also
    # called by ``writeback`` and by the post-build refresh, so an ordinary
    # concurrent edit could fail a fold with an unhandled exception.
    _pin: Optional[dict] = None

    @contextmanager
    def _pinned(self, ot: ObjectTypeDef):
        """Pin this type's base rows and edit log for the block's duration."""
        previous = self._pin
        self._pin = {"type": ot.api_name, "base": self._read_base_rows(ot),
                     "edits": self.store.list_object_edits(ot.api_name)}
        try:
            yield
        finally:
            self._pin = previous

    def _live_edits(self, ot: ObjectTypeDef) -> list[ObjectEdit]:
        if self._pin is not None and self._pin["type"] == ot.api_name:
            return self._pin["edits"]
        return self.store.list_object_edits(ot.api_name)

    def _base_rows(self, ot: ObjectTypeDef) -> list[dict]:
        if self._pin is not None and self._pin["type"] == ot.api_name:
            # Copied, because _materialize updates these dicts in place and a
            # pinned snapshot is read more than once.
            return [dict(row) for row in self._pin["base"]]
        return self._read_base_rows(ot)

    def _read_base_rows(self, ot: ObjectTypeDef) -> list[dict]:
        """Rows from the backing dataset's latest version, projected to the
        declared properties (plus the primary key). Row-level security / column
        masking (``self.policy``) is applied first. Empty if the dataset does not
        exist or has no versions yet."""
        try:
            table = self.catalog.read(ot.backing_dataset)
        except KeyError:
            return []
        # Resolve through the same helper the pushdown path uses, so a service
        # given only `policy_for` still enforces row/column policy here.
        policy = self._policy_for_dataset(ot.backing_dataset)
        if policy is not None:
            table = policy(table)
        keep = set(ot.properties) | {ot.primary_key}
        return [
            {k: v for k, v in row.items() if k in keep}
            for row in self.catalog.table_to_rows(table)
        ]

    def _declared_columns(self, ot: ObjectTypeDef) -> list[str]:
        """Declared properties the backing dataset actually has, plus the key —
        the exact projection the pushdown emits."""
        version = self.catalog.store.get_version(ot.backing_dataset, None)
        if version is None:
            return []
        declared = set(ot.properties) | {ot.primary_key}
        return [c.name for c in version.schema_ if c.name in declared]

    def _pad_declared(self, ot: ObjectTypeDef, props: dict) -> dict:
        """A created object's properties with the declared-but-unset ones
        present as nulls.

        The pushdown emits every projected column for a created object, so it
        already returned ``{'name': 'new', 'realm': 'n', 'pop': None}`` where
        in-memory replay returned ``{'name': 'new', 'realm': 'n'}``. One object,
        two JSON shapes, and which one a client saw depended on whether the
        overlay had been folded into the dataset yet. This settles it on the
        shape the dataset's own schema justifies, everywhere.
        """
        return {**{c: None for c in self._declared_columns(ot)}, **props}

    # -- index --------------------------------------------------------------------

    def index_state(self, ot: ObjectTypeDef) -> Optional[dict]:
        """What the materialization says it was built from, if it exists.

        ``lag`` alone could not name the problem. There are three ways to be
        not-current and only one of them is self-healing:

        * behind by N edits — ``catch_up`` replays them on the next write;
        * built from an older dataset *version* — a version bump rewrites
          arbitrary rows and renumbers every ordinal, so no delta expresses it
          and ``catch_up`` returns 0 without replaying anything;
        * built from an older object-type *definition* — same, and for the
          same reason.

        The last two also accumulate lag, because edits keep landing in the log
        that the store will never apply. So a store that was a version behind
        *and* held unapplied edits reported "behind by 3" and climbing, the
        next write never cleared it, and only a rebuild did — with nothing in
        the payload to say so. The two booleans below are what make those
        states distinguishable from the outside; ``store_is_caught_up`` is the
        same three tests, collapsed to one answer.
        """
        state = self.store.object_index_state(ot.api_name)
        if state is None:
            return None
        dataset = self.catalog.store.get_dataset(ot.backing_dataset)
        current = dataset.latest_version if dataset else None
        return {
            **state,
            "lag": max(0, self.store.max_edit_seq(ot.api_name)
                       - int(state["applied_seq"])),
            "current_dataset_version": current,
            "stale_version": current is None or int(state["dataset_version"]) != current,
            "stale_definition": state["fingerprint"] != self._type_fingerprint(ot),
        }

    def _system_view(self) -> "OntologyService":
        """The same data with no policy bound.

        A materialization is *shared*. Building it through the calling user's
        policy would bake their narrowed view into everyone's index — and the
        rebuild endpoint is only EDITOR-gated, so that is an ordinary editor
        away. Policy is applied on read, by the reader, or not at all.
        """
        return OntologyService(
            self.workspace, self.catalog, self.store, self.ontology,
            object_store=self.object_store,
        )

    def store_is_caught_up(self, ot: ObjectTypeDef) -> bool:
        """Whether the materialization has applied every committed edit.

        The semantics here **inverted** in the operational-store change. This
        used to mean "nothing has changed since the build", so a single write
        made it False and the whole index was thrown away. It now means "the
        materialization is level with the log" — a write advances both, and
        only a *lagging* store is refused.

        A new dataset version still invalidates outright, deliberately: it can
        rewrite arbitrary base rows and renumbers every ordinal, so no
        incremental delta expresses it. A build is now the only thing that
        invalidates.
        """
        return self._state_is_current(ot, self.object_store.state(ot.api_name))

    # The pre-rename name, kept because a build and the type detail endpoint
    # both call it. Same question, older words.
    index_is_fresh = store_is_caught_up

    def _type_fingerprint(self, ot: ObjectTypeDef) -> str:
        """Everything about the definition that changes what a row contains:
        which properties are projected, their declared types (the search text
        is built from the string ones), the key, the title expression — and
        **which dataset the rows come from**.

        The backing dataset was missing, and it is the one whose absence made
        the store serve wrong rows rather than stale ones. Measured: build
        ``city`` against ``cities`` (v1), point the YAML at ``towns`` (also at
        v1), reload. The fingerprint is unchanged and the version comparison
        passes because it asks the *new* dataset for its latest version, so
        ``index_state`` reported ``stale_version: false``, ``stale_definition:
        false`` and ``lag: 0`` — fresh, by every field it has — while
        ``query()`` answered from a materialization of ``cities``. There is no
        delta from one dataset to another, so this is a rebuild-only state like
        the other two, and it now reports as one.
        """
        return definition_fingerprint(
            primary_key=ot.primary_key,
            title_property=ot.title_property,
            backing_dataset=ot.backing_dataset,
            properties={name: prop.type for name, prop in ot.properties.items()},
        )

    def _state_is_current(self, ot: ObjectTypeDef, state) -> bool:
        if state is None:
            return False  # never built, or unreachable — same answer either way
        if state.fingerprint != self._type_fingerprint(ot):
            # Built from a *different* object-type definition. Every other read
            # path projects to the declared properties on every read, so
            # withdrawing a property takes effect there immediately; only the
            # materialization kept serving it, and each subsequent write copied
            # it forward. A definition change now invalidates exactly like a
            # dataset version does — refuse, and let a rebuild republish.
            return False
        dataset = self.catalog.store.get_dataset(ot.backing_dataset)
        version = dataset.latest_version if dataset else None
        if version is None or state.dataset_version != version:
            return False
        return state.applied_seq >= self.store.max_edit_seq(ot.api_name)

    def _store_row(self, ot: ObjectTypeDef, obj: dict, ordinal: int,
                   applied_seq: int = 0) -> ObjectRow:
        """One object as the store holds it: key, position, title, the search
        text, and the property bag."""
        string_props = [
            name for name, prop in ot.properties.items() if prop.type == "string"
        ]
        searchable = " ".join(
            str(obj[p]) for p in string_props if isinstance(obj.get(p), str)
        ).lower()
        return ObjectRow(
            pk=obj["__pk"], ord=ordinal, title=str(obj.get("__title", "")),
            search_text=searchable, applied_seq=applied_seq,
            props={k: v for k, v in obj.items() if not k.startswith("__")},
        )

    def verify_digest(self, ot: ObjectTypeDef) -> bool:
        """Recompute the digest from the materialization's own rows.

        A detector nobody runs is not a detector, so this is a real method with
        a real caller (the rebuild endpoint) rather than a comment. It is the
        only check that catches a row *nobody logged*: the watermark cannot,
        because a store that has drifted can be perfectly caught up by position.
        """
        state = self.object_store.state(ot.api_name)
        if state is None:
            return False
        page = self.object_store.page(ot.api_name, limit=SEARCH_TOTAL_CAP, offset=0)
        if page is None:
            return False
        rows, _total, _state = page
        computed = digest_hex(digest_of_rows([
            {"pk": r["pk"], "applied_seq": r["applied_seq"],
             "props_json": canonical_props(r["props"])}
            for r in rows
        ]))
        return computed == state.digest

    def reindex(self, type_name: str) -> int:
        """Materialize an object type from scratch. Returns the count.

        Materializing is only worthwhile for types small enough to hold in the
        store — which is exactly the modelling advice anyway: entities in the
        ontology, high-volume events in datasets.
        """
        ot = self._require_object_type(type_name)
        backing = self.catalog.store.get_dataset(ot.backing_dataset)
        if backing is None or backing.scans_at_source or backing.latest_version is None:
            # Nothing stable to materialize against.
            self.object_store.drop(type_name)
            return 0

        system = self._system_view()
        # The watermark is read BEFORE the snapshot, deliberately. An edit that
        # commits in between is either inside the snapshot — rows ahead of the
        # watermark, which costs one redundant idempotent replay — or outside
        # it, which is an ordinary lag. Reading it afterwards would let the
        # watermark claim an edit the rows never included, which is the one
        # direction this design must never allow.
        applied_seq = self.store.max_edit_seq(type_name)
        with system._pinned(ot):
            # Ordinals come from the same rule the incremental path uses, never
            # from enumerate(): base rows in file order, created objects at
            # ORD_CREATED_BASE + edit_seq. Renumbering here would silently
            # reshuffle page 1 on every rebuild — precisely the bug `ord` was
            # added to fix. Both reads come off the pinned snapshot, so a
            # create landing mid-rebuild can no longer be in one and not the
            # other.
            ordinals, positions = system._object_ordinals(ot)
            rows = [
                system._store_row(ot, obj, ordinals[obj["__pk"]],
                                  positions.get(obj["__pk"], 0))
                for obj in system._materialize(ot)
            ]
        digest = digest_hex(digest_of_rows([
            {"pk": r.pk, "applied_seq": r.applied_seq,
             "props_json": canonical_props(r.props)}
            for r in rows
        ]))
        self.object_store.replace(
            type_name, rows, dataset_version=backing.latest_version,
            applied_seq=applied_seq, digest=digest,
            fingerprint=self._type_fingerprint(ot),
        )
        return len(rows)

    def _object_ordinals(self, ot: ObjectTypeDef) -> tuple[dict[str, int], dict[str, int]]:
        """(pk -> ordinal, pk -> last edit position), by the rules every path
        must agree on.

        A create for a key that already exists keeps the base row's ordinal —
        it is an in-place replacement, which is what ``_materialize`` does when
        it assigns into a dict that already holds the key. And the positions
        are the ones an incremental stream would have written, so a rebuild
        lands on the same digest instead of a different-but-equally-valid one.
        """
        ordinals: dict[str, int] = {}
        positions: dict[str, int] = {}
        for i, row in enumerate(self._base_rows(ot)):
            ordinals[str(row.get(ot.primary_key))] = i
        for edit in self._live_edits(ot):
            if edit.kind == EditKind.create:
                pk = str(edit.payload.get(ot.primary_key, edit.pk_value))
                ordinals.setdefault(pk, created_ord(edit.edit_seq))
            elif edit.kind == EditKind.delete:
                ordinals.pop(edit.pk_value, None)
                positions.pop(edit.pk_value, None)
                continue
            else:
                pk = edit.pk_value
            positions[pk] = edit.edit_seq
        return ordinals, positions

    def catch_up(self, type_name: str) -> int:
        """Replay everything the materialization still owes. Returns the count.

        This is what makes a lag temporary rather than terminal. It runs on the
        *write* path and on an explicit rebuild — never inside a read: a read
        that writes breaks read-only replicas, races other readers, and buys
        only latency on a path that is already correct.
        """
        ot = self._require_object_type(type_name)
        state = self.object_store.state(type_name)
        if state is None:
            return 0
        dataset = self.catalog.store.get_dataset(ot.backing_dataset)
        version = dataset.latest_version if dataset else None
        if version is None or state.dataset_version != version:
            # A version bump is not a delta; only a rebuild expresses it.
            return 0
        pending = self.store.list_object_edits_since(type_name, state.applied_seq)
        applied = 0
        for edit in pending:
            if not self._apply_to_store(ot, edit):
                # Stop rather than skip. Applying N+1 would advance the
                # watermark past N, and the store would then claim to have
                # applied an edit it never did — the one direction this design
                # must never allow. Leaving the lag in place costs a slow read.
                break
            applied += 1
        return applied

    def _index_query(
        self,
        ot: ObjectTypeDef,
        search: Optional[str],
        filters: Optional[dict[str, str]],
        limit: int,
        offset: int,
    ) -> Optional[dict]:
        """Answer from the materialization when it can prove it is level with
        the log. ``None`` means "cannot", and every caller falls through.

        Row-level security is not represented here — it is per-user, and baking
        one user's view into a shared table would be a serious bug — so a
        policied user always falls through to the scan.
        """
        if self._policy_for_dataset(ot.backing_dataset) is not None:
            return None
        # Only the primary key is an indexed column. Any other filter would
        # cost a JSON extraction per row — measured slower than the DuckDB
        # scan — so those go to the scan, which prunes row groups instead.
        filters = filters or {}
        if set(filters) - {ot.primary_key}:
            return None
        page = self.object_store.page(
            ot.api_name, search=search, pk=filters.get(ot.primary_key),
            limit=limit, offset=offset,
        )
        if page is None:
            return None
        rows, total, state = page
        # Validated *after* the read, against the state the page came from.
        # Checking first and paging second leaves a window where a write lands
        # in between and the answer comes from a state nobody validated.
        if not self._state_is_current(ot, state):
            return None
        keep = set(ot.properties) | {ot.primary_key}
        objects = []
        for row in rows:
            # Projected like every other path. The fingerprint check above
            # should make this unreachable; it is here anyway because "should"
            # is not a guarantee about a shared table that outlives a process,
            # and this was the one path that returned a withdrawn property.
            obj = {k: v for k, v in row["props"].items() if k in keep}
            obj["__pk"] = row["pk"]
            obj["__title"] = row["title"]
            objects.append(obj)
        return _result(objects, total, search)

    # -- pushdown -----------------------------------------------------------------

    def _overlay(self, ot: ObjectTypeDef):
        """Replay the edit log into (deleted pks, updates by pk, created rows,
        create ordinals).

        The overlay is a hand-edit log — thousands of entries at most, against
        datasets of millions of rows — so it is always cheap to load whole.
        """
        keep = set(ot.properties) | {ot.primary_key}
        deleted: set[str] = set()
        updates: dict[str, dict] = {}
        creates: dict[str, dict] = {}
        create_ord: dict[str, int] = {}
        # Whether each create is a *new* object rather than a replacement. A
        # create that follows a delete of the same key is new even though the
        # base row is still in the file, and it takes a new position — which is
        # what in-memory replay does when it pops the key and re-inserts it.
        fresh: dict[str, bool] = {}
        for edit in self._live_edits(ot):
            payload = {k: v for k, v in edit.payload.items() if k in keep}
            if edit.kind == EditKind.create:
                pk = str(payload.get(ot.primary_key, edit.pk_value))
                creates[pk] = payload
                # The position a *new* object takes. A create replacing an
                # existing base row keeps the base ordinal instead; that is
                # resolved against the scan, which is the only thing that knows
                # whether the key was already there.
                create_ord.setdefault(pk, created_ord(edit.edit_seq))
                fresh[pk] = fresh.get(pk, False) or pk in deleted
                deleted.discard(pk)
                updates.pop(pk, None)
            elif edit.kind == EditKind.update:
                pk = edit.pk_value
                if pk in creates:
                    creates[pk].update(payload)
                else:
                    updates.setdefault(pk, {}).update(payload)
            elif edit.kind == EditKind.delete:
                pk = edit.pk_value
                creates.pop(pk, None)
                create_ord.pop(pk, None)
                fresh.pop(pk, None)
                updates.pop(pk, None)
                deleted.add(pk)
        creates = self._policy_admits(ot, creates)
        create_ord = {pk: (o, fresh.get(pk, False))
                      for pk, o in create_ord.items() if pk in creates}
        return deleted, updates, creates, create_ord

    def _policy_admits(self, ot: ObjectTypeDef, creates: dict[str, dict]) -> dict[str, dict]:
        """Created objects this user's policy on the backing dataset allows.

        A created object is a whole row that exists only in the overlay, so it
        never passes through the policied scan the base rows do. Without this,
        a create carrying ``realm='beleriand'`` is returned to a user restricted
        to ``realm='valinor'`` — and materializing that overlay into a shared
        store would make it a durable cross-tenant insert channel.

        Fail closed twice over: a payload that omits the policy column is
        excluded (a null is never "in" an allowlist), and a payload that cannot
        be typed against the dataset's own schema is excluded rather than
        guessed at.

        This is the *read* half for creates. The write half — a create for a
        key that already exists being a replacement, and therefore a
        cross-tenant destructive write when the existing row is hidden — is
        ``_refuse_shadowing_create``. The same two halves for *updates* are
        ``_refuse_policy_escaping_update``.
        """
        policy = self._policy_for_dataset(ot.backing_dataset)
        if policy is None or not creates:
            return creates
        version = self.catalog.store.get_version(ot.backing_dataset, None)
        if version is None:
            return {}
        types = {c.name: c.type for c in version.schema_}
        pks = list(creates)
        arrays = {"__pk": pa.array(pks, type=pa.string())}
        for col, alias in types.items():
            try:
                arrays[col] = pa.array(
                    [creates[pk].get(col) for pk in pks], type=pa.type_for_alias(alias)
                )
            except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError):
                return {}
        rendered = self._under_policy(policy, pa.table(arrays))
        if rendered is None:
            return {}
        return {pk: row for pk, row in creates.items() if pk in rendered}

    @staticmethod
    def _under_policy(policy, table: pa.Table) -> Optional[dict[str, dict]]:
        """``table`` as this user's policy renders it: row-filtered and column
        masked, keyed by the ``__pk`` tag column the caller attached.

        A key that is absent from the result was filtered out by the row
        policy; a value that differs from the one handed in was masked. Those
        are the only two questions anything here asks of a policy, and asking
        them through the policy's own output is what keeps this from becoming a
        second, drifting interpretation of the rules.

        ``None`` means the policy could not be run at all, and every caller
        must treat that as admitting nothing.
        """
        try:
            rendered = policy(table)
        except Exception:  # noqa: BLE001 - a policy that cannot run admits nothing
            return None
        return {row["__pk"]: row for row in rendered.to_pylist()}

    @contextmanager
    def _object_scan(
        self,
        ot: ObjectTypeDef,
        search: Optional[str],
        filters: Optional[dict[str, str]],
        all_columns: bool = False,
    ):
        """Yield ``(con, sql, params, cols, pk)`` for one object type's rows,
        or ``(None, ...)`` when the pushdown can't be done faithfully.

        Paging and aggregation are the same question asked twice — *which
        objects* — so they share this. Splitting it would mean two definitions
        of "the objects of this type", and the second one would eventually
        disagree with the first about a deleted row or a policy.
        """
        empty = (None, None, None, None, None)
        if self.plan_for is None and self._policy_for_dataset(ot.backing_dataset) is not None:
            # A policy applies but we have no way to push it into the scan;
            # never take a path that would skip enforcement.
            yield empty
            return
        backing = self.catalog.store.get_dataset(ot.backing_dataset)
        if backing is not None and backing.scans_at_source:
            # Every object page would become a full scan at the source, and the
            # edit overlay has no stable row identity to merge against. Refuse
            # loudly rather than perform catastrophically: materialize the
            # table into a managed dataset with a transform and bind the object
            # type to that. This covers ClickHouse and Iceberg as well as
            # federated — the predicate was `is_federated`, which let an
            # Iceberg-backed type through to a path with no local parts.
            raise ValueError(
                f"Object type {ot.api_name!r} is backed by {backing.kind} dataset "
                f"{ot.backing_dataset!r}, which is scanned at the source. Bind "
                f"object types to managed datasets — use a transform to "
                f"materialize the rows you need."
            )
        try:
            version = self.catalog.store.get_version(ot.backing_dataset, None)
            # Row/column policy is applied inside this scan when it can be
            # expressed as a filter/projection; otherwise scan_for returns a
            # materialized, policy-filtered table and we still push the rest
            # (search, filters, paging) down onto it.
            base_scan = self.catalog.scan_for(
                ot.backing_dataset, plan_for=self.plan_for
            )
        except KeyError:
            yield empty
            return
        if version is None:
            yield empty
            return

        available = {c.name: c.type for c in version.schema_}
        pk = ot.primary_key
        if pk not in available:
            yield empty  # can't identify objects without the key column
            return
        # Project exactly what _materialize would keep: declared properties
        # that actually exist in the dataset, plus the primary key.
        #
        # ...unless the caller is folding the overlay back into the dataset, in
        # which case it needs every column. An object type almost never declares
        # every column of its backing dataset, and writing back the projection
        # would delete the undeclared ones — silent data loss landing on
        # downstream transforms rather than on the ontology.
        cols = (list(available) if all_columns
                else [c for c in available if c in (set(ot.properties) | {pk})])

        deleted, updates, creates, create_ord = self._overlay(ot)
        con = duckdb.connect()
        try:
            # The base scan is a registered Arrow object, so this connection
            # never needs filesystem access. The SQL is entirely
            # server-generated: identifiers come from the validated ontology
            # and are quoted, values are bound parameters.
            con.execute("SET enable_external_access=false")
            con.register("__base", base_scan)
            try:
                base = self._register_overlay_tables(
                    con, cols, available, updates, creates, create_ord
                )
            except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError):
                yield empty  # a payload we can't type faithfully; be exact instead
                return

            sql, params = self._build_sql(
                cols, pk, deleted, base, search, filters, ot
            )
            yield con, sql, params, cols, pk
        finally:
            con.close()

    def _sql_query(
        self,
        ot: ObjectTypeDef,
        search: Optional[str],
        filters: Optional[dict[str, str]],
        limit: int,
        offset: int,
    ) -> Optional[dict]:
        """Filter, search, count and page objects inside DuckDB.

        Returns None when the pushdown can't be done faithfully, so the caller
        falls back to the in-memory path. Semantics match ``_materialize``:
        base rows in file order with updates applied in place, created objects
        appended, deleted objects removed, last-wins on duplicate keys.
        """
        with self._object_scan(ot, search, filters) as (con, sql, params, cols, pk):
            if con is None:
                return None
            # Object queries are still linear in dataset size, so they carry the
            # same budget as any other interactive query.
            with limits.limited(con, limits.QueryLimits.interactive()):
                # The same saturating count the index applies. If these two
                # disagreed, "the index is a faster path to the same answer"
                # would stop being true the moment a search got popular.
                total = con.execute(
                    count_sql(sql, bool(search)), params
                ).fetchone()[0]
                page = con.execute(
                    f"{sql} LIMIT ? OFFSET ?", [*params, max(0, limit), max(0, offset)]
                ).arrow()
            if isinstance(page, pa.RecordBatchReader):
                page = page.read_all()

        objects = []
        for row in self.catalog.table_to_rows(page):
            row.pop("__ord", None)
            obj = dict(row)
            obj["__pk"] = str(row.get(pk))
            obj["__title"] = ot.title_for(row)
            objects.append(obj)
        return _result(objects, total, search)

    # -- aggregation ---------------------------------------------------------

    # An allowlist, not a passthrough: the op becomes a SQL function name, so
    # anything not in here would be an injection point wearing a feature's
    # clothes. Adding one is a deliberate edit, which is the point.
    AGGREGATIONS = {
        "count": "count",
        "sum": "sum",
        "avg": "avg",
        "min": "min",
        "max": "max",
        "median": "median",
        "count_distinct": "count",  # rendered with DISTINCT below
    }

    # Groups returned by one aggregation. A group-by on a high-cardinality
    # property is the easy way to ask for a million rows by accident, and an
    # aggregation that returns a million rows has not aggregated anything.
    MAX_GROUPS = 1000

    def aggregate(
        self,
        type_name: str,
        group_by: Optional[list[str]] = None,
        metrics: Optional[list[dict]] = None,
        filters: Optional[dict[str, str]] = None,
        search: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """Group objects and compute metrics over them.

        Dashboards could always chart the *backing dataset* with SQL, but that
        routes around the object model: it sees raw rows, not objects, so it
        misses the edit overlay entirely and answers from data an action has
        already changed. This aggregates the same object set that
        :meth:`query` pages, overlay and policy included.
        """
        ot = self._require_object_type(type_name)
        group_by = list(group_by or [])
        metrics = list(metrics or [{"op": "count", "alias": "count"}])
        limit = max(1, min(int(limit), self.MAX_GROUPS))

        declared = set(ot.properties) | {ot.primary_key}
        for prop in group_by:
            if prop not in declared:
                raise ValueError(
                    f"Unknown group_by property {prop!r} for object type "
                    f"{type_name!r}"
                )
        specs = [self._metric_spec(m, declared, type_name) for m in metrics]
        if not specs:
            raise ValueError("At least one metric is required")

        with self._object_scan(ot, search, filters) as (con, sql, params, cols, _pk):
            if con is not None:
                missing = [p for p in group_by if p not in cols]
                if missing:
                    # Declared in the ontology but absent from this version of
                    # the backing dataset: grouping on it would be a SQL error.
                    raise ValueError(
                        f"Property {missing[0]!r} is not present in dataset "
                        f"{ot.backing_dataset!r}"
                    )
                return self._aggregate_sql(con, sql, params, group_by, specs, limit)

        # No faithful pushdown (hash masking, an untypeable overlay): compute
        # over the exact in-memory objects instead. Slower, never wrong.
        return self._aggregate_python(ot, group_by, specs, filters, search, limit)

    def _metric_spec(self, metric: dict, declared: set[str], type_name: str) -> dict:
        op = str(metric.get("op", "")).lower()
        if op not in self.AGGREGATIONS:
            raise ValueError(
                f"Unknown aggregation {op!r}: expected one of "
                f"{', '.join(sorted(self.AGGREGATIONS))}"
            )
        prop = metric.get("property")
        if op == "count" and prop is None:
            pass  # count(*) needs no column
        elif prop is None:
            raise ValueError(f"Aggregation {op!r} requires a property")
        elif prop not in declared:
            raise ValueError(
                f"Unknown property {prop!r} for object type {type_name!r}"
            )
        alias = metric.get("alias") or (f"{op}_{prop}" if prop else op)
        return {"op": op, "property": prop, "alias": str(alias)}

    def _aggregate_sql(self, con, sql: str, params: list, group_by: list[str],
                       specs: list[dict], limit: int) -> dict:
        q = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
        selects = [q(p) for p in group_by]
        for spec in specs:
            fn = self.AGGREGATIONS[spec["op"]]
            if spec["property"] is None:
                expr = "count(*)"
            elif spec["op"] == "count_distinct":
                expr = f"count(DISTINCT {q(spec['property'])})"
            else:
                expr = f"{fn}({q(spec['property'])})"
            selects.append(f"{expr} AS {q(spec['alias'])}")

        grouped = f"SELECT {', '.join(selects)} FROM ({sql}) o"
        if group_by:
            keys = ", ".join(q(p) for p in group_by)
            # Order by the first metric descending: "biggest first" is what a
            # chart wants, and it makes the row cap keep the interesting rows
            # rather than an arbitrary slice.
            grouped += f" GROUP BY {keys} ORDER BY {q(specs[0]['alias'])} DESC NULLS LAST"

        with limits.limited(con, limits.QueryLimits.interactive()):
            total_groups = con.execute(
                f"SELECT count(*) FROM ({grouped}) g", params
            ).fetchone()[0] if group_by else 1
            table = con.execute(f"{grouped} LIMIT ?", [*params, limit]).arrow()
        if isinstance(table, pa.RecordBatchReader):
            table = table.read_all()
        rows = self.catalog.table_to_rows(table)
        return {"groups": rows, "group_count": int(total_groups),
                "truncated": bool(group_by) and int(total_groups) > limit}

    def _aggregate_python(self, ot: ObjectTypeDef, group_by: list[str],
                          specs: list[dict], filters, search, limit: int) -> dict:
        """The exact path, for objects the pushdown declines to handle."""
        objects = self.query(
            ot.api_name, search=search, filters=filters, limit=_LINK_LIMIT, offset=0
        )["objects"]

        buckets: dict[tuple, list[dict]] = {}
        for obj in objects:
            key = tuple(obj.get(p) for p in group_by)
            buckets.setdefault(key, []).append(obj)

        rows = []
        for key, members in buckets.items():
            row = dict(zip(group_by, key))
            for spec in specs:
                row[spec["alias"]] = _compute(spec, members)
            rows.append(row)
        if group_by:
            rows.sort(key=lambda r: (r.get(specs[0]["alias"]) is None,
                                     _sortable(r.get(specs[0]["alias"]))), reverse=True)
        return {"groups": rows[:limit], "group_count": len(rows),
                "truncated": len(rows) > limit}

    def _register_overlay_tables(
        self,
        con,
        cols: list[str],
        available: dict[str, str],
        updates: dict[str, dict],
        creates: dict[str, dict],
        create_ord: dict[str, int],
    ) -> dict[str, bool]:
        """Register the (small) update and create sets as typed Arrow tables so
        DuckDB can merge them with the base scan. Raises if a payload value
        can't be represented in the dataset's own column type."""
        registered = {"updates": False, "creates": False}

        def build(by_pk: dict[str, dict], ordinals=None, mask: bool = False) -> pa.Table:
            arrays = {"__pk": pa.array(list(by_pk), type=pa.string())}
            for col in cols:
                typ = pa.type_for_alias(available[col])
                arrays[col] = pa.array(
                    [row.get(col) for row in by_pk.values()], type=typ
                )
                if mask:
                    # Whether this update *assigned* the column, as distinct
                    # from leaving it alone. COALESCE cannot tell those apart,
                    # so an update clearing a property to NULL had no
                    # expression at all: folding one silently restored the old
                    # value, and — worse — once the create an update was
                    # merging into had been folded into the base, a null
                    # assignment that had been working stopped working.
                    arrays[f"__set__{col}"] = pa.array(
                        [col in row for row in by_pk.values()], type=pa.bool_()
                    )
            if ordinals is not None:
                arrays["__cord"] = pa.array(
                    [ordinals[pk][0] for pk in by_pk], type=pa.int64()
                )
                arrays["__fresh"] = pa.array(
                    [ordinals[pk][1] for pk in by_pk], type=pa.bool_()
                )
            return pa.table(arrays)

        if updates:
            con.register("__ovl_updates", build(updates, mask=True))
            registered["updates"] = True
        if creates:
            con.register("__ovl_creates", build(creates, create_ord))
            registered["creates"] = True
        return registered

    def _build_sql(
        self,
        cols: list[str],
        pk: str,
        deleted: set[str],
        registered: dict[str, bool],
        search: Optional[str],
        filters: Optional[dict[str, str]],
        ot: ObjectTypeDef,
    ) -> tuple[str, list]:
        q = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
        params: list = []
        prefix = ""
        projection = ", ".join(q(c) for c in cols)

        # Base scan, de-duplicated last-wins on the primary key and kept in
        # file order — object order must be deterministic for paging.
        # A predicate on the primary key can be applied *before* the dedup
        # window: the window partitions by that same key, so restricting to one
        # key keeps all of its duplicates and still picks the last. This turns
        # a point lookup into a pruned scan instead of a full one. No other
        # column may be pre-filtered — dropping a row before dedup could change
        # which duplicate survives.
        scan_where = ""
        pk_filter = (filters or {}).get(pk)
        if pk_filter is not None:
            scan_where = f" WHERE CAST({q(pk)} AS VARCHAR) = ?"
            params.append(str(pk_filter))

        base = (
            f"SELECT {projection}, row_number() OVER () AS __ord "
            f"FROM __base{scan_where}"
        )
        base = (
            f"SELECT * FROM ({base}) b "
            f"QUALIFY row_number() OVER (PARTITION BY CAST({q(pk)} AS VARCHAR) "
            f"ORDER BY __ord DESC) = 1"
        )
        if deleted:
            placeholders = ", ".join("?" for _ in deleted)
            base += f" AND CAST({q(pk)} AS VARCHAR) NOT IN ({placeholders})"
            params.extend(sorted(deleted))

        if registered["updates"]:
            # Updates apply in place, so an updated object keeps its position.
            # The per-column `__set__` flag, not COALESCE: an update that
            # assigns NULL is an assignment, and COALESCE reads it as "no
            # update" — which is how a null-clearing edit could be silently
            # undone by a fold. A row with no matching update has the flag as
            # NULL, so CASE falls to the base value.
            merged_cols = ", ".join(
                f"CASE WHEN u.{q('__set__' + c)} THEN u.{q(c)} "
                f"ELSE b.{q(c)} END AS {q(c)}"
                for c in cols
            )
            body = (
                f"SELECT {merged_cols}, b.__ord FROM ({base}) b "
                f"LEFT JOIN __ovl_updates u "
                f"ON CAST(b.{q(pk)} AS VARCHAR) = u.__pk"
            )
        else:
            body = f"SELECT {projection}, __ord FROM ({base})"

        if registered["creates"]:
            # Two rules, both matching what in-memory materialization does when
            # it assigns into a dict:
            #
            #   * a create for a key that is NOT in the base data is a new
            #     object and sorts after every base row, by edit position —
            #     not by a constant. Every create used to carry
            #     9223372036854775807, which left them all tied under
            #     ORDER BY __ord and made paging non-deterministic once there
            #     was more than one.
            #   * a create for a key that IS in the base data replaces that row
            #     *in place* and keeps its ordinal, so the page someone is
            #     looking at does not reshuffle. The base row is dropped below
            #     rather than emitted alongside — otherwise the object appears
            #     twice, which is exactly how the pushdown and the in-memory
            #     path came to disagree on the object count.
            #
            # The merged base goes in a CTE so it is *written* once: repeating
            # the subquery would repeat its bound parameters too, and the two
            # copies would silently consume each other's values.
            # A create assigns the *declared* properties absolutely and says
            # nothing about any other column of the backing dataset. Taking
            # `c.<col>` for an undeclared column wrote NULL over whatever the
            # replaced base row held — the exact silent loss the all_columns
            # projection exists to prevent, arriving through the create branch
            # instead. A create that follows a delete inherits nothing, so it
            # keeps the NULL.
            declared = set(ot.properties) | {pk}
            create_cols = ", ".join(
                (f"c.{q(c)} AS {q(c)}" if c in declared
                 else f"CASE WHEN c.__fresh THEN NULL ELSE m.{q(c)} END AS {q(c)}")
                for c in cols
            )
            prefix = f"WITH __merged AS ({body}) "
            body = (
                f"SELECT {create_cols}, "
                f"CASE WHEN c.__fresh THEN c.__cord "
                f"ELSE COALESCE(m.__ord, c.__cord) END AS __ord "
                f"FROM __ovl_creates c LEFT JOIN __merged m "
                f"ON CAST(m.{q(pk)} AS VARCHAR) = c.__pk"
                f" UNION ALL "
                f"SELECT {projection}, __ord FROM __merged "
                f"WHERE CAST({q(pk)} AS VARCHAR) NOT IN "
                f"(SELECT __pk FROM __ovl_creates)"
            )

        where: list[str] = []
        if search:
            string_props = [
                c for c in cols
                if c in ot.properties and ot.properties[c].type == "string"
            ]
            if not string_props:
                where.append("false")
            else:
                clauses = []
                for c in string_props:
                    clauses.append(f"lower(CAST({q(c)} AS VARCHAR)) LIKE ?")
                    params.append(f"%{search.lower()}%")
                where.append("(" + " OR ".join(clauses) + ")")
        for prop, value in (filters or {}).items():
            where.append(f"CAST({q(prop)} AS VARCHAR) = ?")
            params.append(str(value))

        sql = f"{prefix}SELECT * FROM ({body}) o"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if search and ot.title_property and ot.title_property in cols:
            # The identical ranking the index applies: by where the term
            # appears in the title, with body-only matches after it in object
            # order. Reorders results, never changes which ones match — and
            # the two paths must agree, or paging an indexed type would
            # differ from paging an unindexed one.
            title = f"lower(CAST({q(ot.title_property)} AS VARCHAR))"
            pos = f"strpos({title}, ?)"
            sql += (
                f" ORDER BY CASE WHEN {pos} > 0 THEN {pos} ELSE {RANK_BODY_ONLY} END,"
                f" __ord"
            )
            params.extend([search.lower(), search.lower()])
        else:
            sql += " ORDER BY __ord"
        return sql, params

    def _materialize(self, ot: ObjectTypeDef) -> list[dict]:
        """Base rows with the edit overlay applied, in stable order.

        The exact path, and the oracle: it replays the log directly, so it is
        the definition the other two paths are checked against.
        """
        keep = set(ot.properties) | {ot.primary_key}
        objects: dict[str, dict] = {}
        for row in self._base_rows(ot):
            objects[str(row.get(ot.primary_key))] = row
        # Created objects are subject to the same policy the base rows went
        # through; see _policy_admits for why an unpoliced create is a
        # cross-tenant insert channel rather than a cosmetic inconsistency.
        admitted = self._overlay(ot)[2]
        # Resolved once: a created object carries every declared column the
        # dataset has, so its shape does not change when the overlay is folded.
        pad = {c: None for c in self._declared_columns(ot)}
        for edit in self._live_edits(ot):
            if edit.kind == EditKind.create:
                payload = {k: v for k, v in edit.payload.items() if k in keep}
                pk = str(payload.get(ot.primary_key, edit.pk_value))
                if pk not in admitted:
                    continue
                objects[pk] = {**pad, **payload}
            elif edit.kind == EditKind.update:
                target = objects.get(edit.pk_value)
                if target is not None:
                    target.update(
                        {k: v for k, v in edit.payload.items() if k in keep}
                    )
            elif edit.kind == EditKind.delete:
                objects.pop(edit.pk_value, None)
        result = []
        for pk, row in objects.items():
            obj = dict(row)
            obj["__pk"] = pk
            obj["__title"] = ot.title_for(row)
            result.append(obj)
        return result

    # -- queries ----------------------------------------------------------------

    def query(
        self,
        type_name: str,
        search: Optional[str] = None,
        filters: Optional[dict[str, str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        ot = self._require_object_type(type_name)
        if filters:
            declared = set(ot.properties) | {ot.primary_key}
            for prop in filters:
                if prop not in declared:
                    raise ValueError(
                        f"Unknown filter property {prop!r} for object type {type_name!r}"
                    )
        # Fastest first: a fresh index answers from the metadata store without
        # touching Parquet at all.
        indexed = self._index_query(ot, search, filters, limit, offset)
        if indexed is not None:
            return indexed
        # Otherwise push filtering, search, counting and paging into DuckDB when
        # the backing dataset needs no per-user filtering. Falls back below when
        # it can't be done faithfully.
        pushed = self._sql_query(ot, search, filters, limit, offset)
        if pushed is not None:
            return pushed

        objects = self._materialize(ot)

        if search:
            needle = search.lower()
            string_props = [
                name for name, prop in ot.properties.items() if prop.type == "string"
            ]
            objects = [
                o
                for o in objects
                if any(
                    isinstance(o.get(p), str) and needle in o[p].lower()
                    for p in string_props
                )
            ]
        if filters:
            declared = set(ot.properties) | {ot.primary_key}
            for prop, value in filters.items():
                if prop not in declared:
                    raise ValueError(
                        f"Unknown filter property {prop!r} for object type {type_name!r}"
                    )
                objects = [
                    o
                    for o in objects
                    if o.get(prop) is not None and str(o.get(prop)) == str(value)
                ]

        offset = max(0, offset)
        limit = max(0, limit)
        return _result(objects[offset : offset + limit], len(objects), search)

    def get(self, type_name: str, pk: str) -> Optional[dict]:
        ot = self._require_object_type(type_name)
        pk = str(pk)
        # Through the store first, for the same reason query() does: without
        # this, apply_action's existence check still pays a full scan on every
        # write and "writes are O(1)" is simply not true.
        indexed = self._index_query(ot, None, {ot.primary_key: pk}, 1, 0)
        if indexed is not None:
            objects = indexed["objects"]
            return objects[0] if objects else None
        # A point lookup shouldn't cost a full scan: filter on the primary key
        # in DuckDB when possible.
        pushed = self._sql_query(ot, None, {ot.primary_key: pk}, limit=1, offset=0)
        if pushed is not None:
            objects = pushed["objects"]
            return objects[0] if objects else None
        for obj in self._materialize(ot):
            if obj["__pk"] == pk:
                return obj
        return None

    def linked(self, type_name: str, pk: str, link_name: str) -> list[dict]:
        ot = self._require_object_type(type_name)
        link = self.ontology.link_type(link_name)
        if link is None:
            raise KeyError(f"Unknown link type: {link_name!r}")
        if link.from_type == ot.api_name:
            my_prop, other_type_name, other_prop = (
                link.from_property,
                link.to_type,
                link.to_property,
            )
        elif link.to_type == ot.api_name:
            my_prop, other_type_name, other_prop = (
                link.to_property,
                link.from_type,
                link.from_property,
            )
        else:
            raise KeyError(
                f"Link {link_name!r} does not involve object type {type_name!r}"
            )
        obj = self.get(type_name, pk)
        if obj is None:
            return []
        other = self._require_object_type(other_type_name)
        my_value = obj.get(my_prop)
        if my_value is None:
            return []  # a null join key links to nothing, not to other nulls
        key = str(my_value)
        # Link traversal is a filter on the other side's join column, so it
        # rides the same pushdown as query().
        pushed = self._sql_query(
            other, None, {other_prop: key}, limit=_LINK_LIMIT, offset=0
        )
        if pushed is not None:
            return pushed["objects"]
        return [
            o
            for o in self._materialize(other)
            if o.get(other_prop) is not None and str(o.get(other_prop)) == key
        ]

    def edits(self, type_name: str, live_only: bool = True) -> list[ObjectEdit]:
        return self.store.list_object_edits(type_name, live_only=live_only)

    # -- writeback ----------------------------------------------------------------

    def backing_is_transform_produced(self, ot: ObjectTypeDef) -> Optional[str]:
        """The transform that produces this object type's backing dataset, if any.

        This is the hazard that actually matters for writeback, and it is not
        federation. Today an edit survives a rebuild because it is replayed on
        every read. Folding destroys that: fold into version 7, mark the edits
        folded, and tonight's build writes version 8 from upstream with no trace
        of them. Hand edits revert silently, hours later, with no error anywhere
        — a regression worse than the problem being solved, because it is
        invisible.
        """
        for edge in self.catalog.store.list_lineage():
            if edge.downstream_dataset == ot.backing_dataset:
                return edge.transform_name
        # *Any* version, not just the latest. Writeback stamps its own version
        # `source='writeback'` and compaction stamps `'compact'`, so asking only
        # about the latest version made this guard erode itself: one deliberate
        # override erased the evidence, and every fold after that proceeded with
        # no warning that tonight's build would revert the edits. A dataset a
        # transform has ever produced is a dataset a transform can produce again.
        for version in self.catalog.store.list_versions(ot.backing_dataset):
            if version.source == "transform":
                return "a transform"
        return None

    @staticmethod
    def _refuse_duplicate_keys_in_result(folded: pa.Table, ot: ObjectTypeDef, pk: str) -> None:
        """Refuse to publish a fold that would *introduce* a duplicate key.

        ``_refuse_duplicate_keys`` asks the same question of ``__base`` — the
        dataset as it stands *before* the overlay. That is the right question
        for a dataset that already had duplicates, and the wrong one for a fold
        that creates them: the overlay is what introduces the duplicate, so the
        input was clean, the guard passed, and the fold published a dataset
        whose primary key is not unique. Every later writeback then failed the
        input check for a duplicate this code had written itself, with the
        docstring's own remedy ("de-duplicate with a transform") as the only
        way out of a state no operator caused.

        ``_refuse_primary_key_rewrite`` closes the route that was measured
        reaching here. This is the invariant rather than the route: a fold
        publishes a dataset, and the dataset it publishes has to satisfy the
        property the fold demands of the one it read.
        """
        if pk not in folded.column_names:
            return
        keys = folded.column(pk).cast(pa.string()).to_pylist()
        extra = len(keys) - len(set(keys))
        if extra:
            raise ValueError(
                f"Cannot fold object type {ot.api_name!r}: applying the edit "
                f"overlay would produce {extra} row(s) sharing a {pk!r} value "
                f"with another row, so dataset {ot.backing_dataset!r} would "
                f"come out with a primary key that is not unique. Nothing has "
                f"been written. Review the pending edits for this object type."
            )

    @staticmethod
    def _refuse_duplicate_keys(con, ot: ObjectTypeDef, pk: str) -> None:
        """Refuse to fold a dataset whose primary key is not unique.

        The object view de-duplicates last-wins in a window function, so it
        never showed the extra rows; the fold *materializes* that dedup into
        the dataset. One unrelated hand edit was therefore enough to delete
        rows no edit ever referenced — measured, five rows to three — and they
        are gone for ``catalog.read()``, every downstream transform, every
        dashboard, and any other object type bound to the same dataset under a
        different key. Nothing in the audit trail names them.

        So: refuse, and say what to do about it. Deduplicating is a
        transformation of the data and belongs in a transform, where it is
        visible and reviewable, not as a side effect of saving an edit.
        """
        quoted = '"' + pk.replace('"', '""') + '"'
        extra = con.execute(
            f"SELECT COALESCE(sum(n), 0) - count(*) FROM ("
            f"  SELECT count(*) AS n FROM __base "
            f"  GROUP BY CAST({quoted} AS VARCHAR) HAVING count(*) > 1)"
        ).fetchone()[0]
        if extra:
            raise ValueError(
                f"Cannot fold object type {ot.api_name!r}: dataset "
                f"{ot.backing_dataset!r} has {int(extra)} row(s) sharing a "
                f"{pk!r} value with another row. The object view hides them by "
                f"keeping the last of each key; folding would delete them from "
                f"the dataset for every other reader. De-duplicate with a "
                f"transform first."
            )

    def writeback(
        self, type_name: str, actor: str = "anonymous", allow_transform_backed: bool = False
    ) -> dict:
        """Fold the edit overlay into a new dataset version, and shrink the log.

        Nothing is mutated in place: this mints a version whose rows already
        have the overlay applied, then marks those edits folded so the read
        paths stop replaying them.

        ``write``, not ``append``: an overlay *delete* has no expression as an
        appended row, and an update expressed as one leaves both rows. The
        last-wins dedup that would nearly rescue that lives only in the object
        query's window function — ``catalog.read()``, dashboards and every
        downstream transform see the raw parts — so folding via append would
        make the dataset disagree with the object view about how many rows
        exist.
        """
        ot = self._require_object_type(type_name)
        backing = self.catalog.store.get_dataset(ot.backing_dataset)
        if backing is None or backing.latest_version is None:
            raise ValueError(f"Dataset {ot.backing_dataset!r} has no versions to fold into")
        if backing.scans_at_source:
            raise ValueError(
                f"Object type {type_name!r} is backed by {backing.kind} dataset "
                f"{ot.backing_dataset!r}, which is scanned at the source and has "
                f"no version for Laurelin to write. Materialize the rows you need "
                f"with a transform and bind the object type to that."
            )
        transform = self.backing_is_transform_produced(ot)
        if transform and not allow_transform_backed:
            raise ValueError(
                f"Dataset {ot.backing_dataset!r} is produced by transform "
                f"{transform!r}. Folding edits into it would hand them to the "
                f"next build to overwrite, silently. Override deliberately if "
                f"this is a one-shot import."
            )

        # T0 — capture the exact edits being folded, as an explicit id list.
        # Never "all edits for this type", never `seq <= max_seq`: on Postgres
        # a reader can see 42 without seeing 41, and marking a range would mark
        # 41 folded when it never was, losing it unrecoverably.
        system = self._system_view()
        live = self.store.list_object_edits(type_name, live_only=True)
        if not live:
            return {"object_type": type_name, "folded": 0,
                    "version": backing.latest_version, "objects": None}
        edit_ids = [e.id for e in live]
        base_version = backing.latest_version

        # T1 — build the folded table. Policy is off by construction (system
        # view): a fold run as one user would rewrite the dataset *as they see
        # it*, deleting every row their RLS hides, for everyone.
        with system._object_scan(ot, None, None, all_columns=True) as (con, sql, params, cols, pk):
            if con is None:
                raise ValueError(
                    f"Cannot fold object type {type_name!r}: its overlay cannot be "
                    f"applied to dataset {ot.backing_dataset!r} faithfully."
                )
            with limits.limited(con, limits.QueryLimits.build()):
                # Inside the budget: it is a full aggregate over the base, and
                # a fold is not a licence to run an unbounded one.
                self._refuse_duplicate_keys(con, ot, pk)
                folded = con.execute(sql, params).arrow()
        if isinstance(folded, pa.RecordBatchReader):
            folded = folded.read_all()
        folded = folded.drop_columns(["__ord"]) if "__ord" in folded.column_names else folded
        # Before T2, so a fold that would publish a non-unique key publishes
        # nothing at all.
        self._refuse_duplicate_keys_in_result(folded, ot, pk)

        # T2 — publish, or fail, atomically against the base we read.
        #
        # This *was* a read-the-version-then-write check, and the gap between
        # the two was reachable by anything: two operators, a double click, a
        # client retry, a nightly build. `_commit_version` arbitrates version
        # *numbers*, not content, so a writer that loses simply takes the next
        # integer — publishing V+2 rebuilt from V and dropping V+1's rows
        # entirely. Measured both ways round: a concurrent fold lost a
        # committed edit that the other fold had already marked folded
        # (unrecoverable — the live log was empty), and a concurrent transform
        # build lost its whole version.
        #
        # `expect_version` is the same check with no gap: the row insert either
        # takes exactly base+1 or nothing is registered. A fold is cheap to
        # retry and there is no sound way to rebase one.
        result = self.catalog.write(
            ot.backing_dataset, folded, source="writeback",
            expect_version=base_version,
        )

        # T3 — mark, after the write, never before. Crash between T2 and T3 and
        # the folded edits replay on top of themselves: every edit kind is an
        # absolute assignment, so the result is correct, merely not yet cheaper.
        # Reversed, edits would be marked folded but never written — gone from
        # the read path and from the dataset, unrecoverably.
        marked = self.store.mark_edits_folded(edit_ids, result.version)
        self.store.log_audit(
            "object_edits_folded",
            {"object_type": type_name, "dataset": ot.backing_dataset,
             "version": result.version, "edits": marked, "rows": result.row_count},
            actor=actor,
        )
        # T4 — the new version invalidates the materialization the same way a
        # build does, so rebuild it now rather than leaving reads on the scan.
        objects = self.reindex(type_name) if self.object_store.state(type_name) else None
        return {"object_type": type_name, "folded": marked, "version": result.version,
                "row_count": result.row_count, "objects": objects}

    # -- the write path -----------------------------------------------------------

    def _record_edit(self, ot: ObjectTypeDef, edit: ObjectEdit) -> int:
        """Commit the edit and stamp the position it was given onto it, so the
        caller holds the same identity the log does."""
        edit.edit_seq = self._commit_edit(ot, edit)
        return edit.edit_seq

    def _commit_edit(self, ot: ObjectTypeDef, edit: ObjectEdit) -> int:
        """Record one edit, and apply it to the materialization if there is one.

        Three cases, and the third is the interesting one:

        * not materialized — append to the log; nothing else exists to update.
        * materialized and level with the log — one transaction that appends
          the edit *and* upserts the affected rows. This is the whole feature:
          a write no longer throws the materialization away.
        * materialized but behind — catch up first, then commit. If catching up
          is not possible (a new dataset version, an unreachable store), append
          to the log alone and let the read path detect the lag. The write is
          never failed for a materialization's sake: the edit log is the source
          of truth and every read path can replay it.
        """
        state = self.object_store.state(ot.api_name)
        if state is None:
            return self.store.add_object_edit(edit)
        if not self._state_is_current(ot, state):
            self.catch_up(ot.api_name)
            if not self.store_is_caught_up(ot):
                return self.store.add_object_edit(edit)
        try:
            return self.object_store.commit_edit(
                edit, pks=self._edit_pks(ot, edit),
                build=lambda pre_image, seq: self._rows_for_edit(ot, edit, pre_image, seq),
            )
        except Exception:  # noqa: BLE001
            # Safe *because* commit_edit is all-or-nothing (see
            # ObjectStore.commit_edit): reaching here means nothing was logged,
            # so appending is a recovery rather than a duplicate. A remote
            # store being unreachable must not fail a user's write.
            return self.store.add_object_edit(edit)

    @staticmethod
    def _edit_pks(ot: ObjectTypeDef, edit: ObjectEdit) -> list[str]:
        """The keys one edit touches, from the edit alone.

        The store needs these *before* the builder runs: it reads their current
        rows inside the write transaction and hands them over, which is what
        keeps the merge from being a read-modify-write across two connections.
        """
        if edit.kind == EditKind.create:
            return [str(edit.payload.get(ot.primary_key, edit.pk_value))]
        return [edit.pk_value]

    def _apply_to_store(self, ot: ObjectTypeDef, edit: ObjectEdit) -> bool:
        """Apply an already-logged edit to the materialization. True if applied.

        Only a *store* failure returns False, and the caller must then stop.
        """
        try:
            self.object_store.apply_edit(
                edit, pks=self._edit_pks(ot, edit),
                build=lambda pre_image, seq: self._rows_for_edit(ot, edit, pre_image, seq),
            )
        except Exception:  # noqa: BLE001
            return False
        return True

    def _rows_for_edit(
        self, ot: ObjectTypeDef, edit: ObjectEdit, pre_image: dict[str, dict], seq: int
    ) -> tuple[list[ObjectRow], list[str]]:
        """The rows one edit writes, given the current rows for the keys it
        touches and the log position it was actually given.

        Both arguments come from the store, from inside the transaction that
        will write the result. That is the whole point: this is a
        read-modify-write, and it used to read on one connection and write on
        another. Two concurrent updates to one object then merged onto the same
        pre-image and one committed edit vanished; a delete racing an update
        let the update re-insert the row the delete had removed. And ``seq``
        used to be a guess at ``MAX(edit_seq) + 1``, so four concurrent creates
        all took the same ordinal.

        The pre-image is unpoliced by construction, so a policied editor's
        masked, row-filtered view can never be written into a table everyone
        shares. The *existence* check in ``apply_action`` stays on the policied
        path, where it belongs — pointing it at an unfiltered store would turn
        it into an enumeration oracle over other tenants' keys.
        """
        keep = set(ot.properties) | {ot.primary_key}
        payload = {k: v for k, v in edit.payload.items() if k in keep}
        if edit.kind == EditKind.delete:
            return [], [edit.pk_value]
        pk = (str(payload.get(ot.primary_key, edit.pk_value))
              if edit.kind == EditKind.create else edit.pk_value)
        existing = pre_image.get(pk)
        if edit.kind == EditKind.create:
            # A create is an absolute assignment: nothing is inherited from a
            # deleted-and-recreated object, or from a row this key replaces.
            props = self._pad_declared(ot, payload)
            ordinal = int(existing["ord"]) if existing else created_ord(seq)
        else:
            if existing is None:
                # No such object. In-memory replay makes this exact edit a
                # no-op (`objects.get(pk)` is None, so nothing is updated), so
                # writing nothing and advancing the watermark keeps the two
                # paths saying the same thing. Declining instead would leave a
                # permanent lag for an edit that has no effect anywhere.
                return [], []
            # The pre-image is projected too. Filtering only the payload let a
            # property withdrawn from the ontology ride along in the stored
            # bag forever, refreshed by every subsequent write.
            props = {k: v for k, v in json.loads(existing["props_json"]).items()
                     if k in keep}
            props.update(payload)
            ordinal = int(existing["ord"])
        obj = {**props, "__pk": pk, "__title": ot.title_for(props)}
        return [self._store_row(ot, obj, ordinal, seq)], []

    # -- actions ------------------------------------------------------------------

    def _system_row(self, ot: ObjectTypeDef, pk_value: str) -> Optional[pa.Table]:
        """One object as the *dataset* holds it — every column, no policy, no
        masks — as a one-row Arrow table, or None if there is no such object.

        The same trick ``reindex`` uses for the same reason (see
        ``_system_view``): the thing that has to be checked is a value the
        caller is not allowed to see, so it is read under a system identity and
        never handed back. Nothing in this table reaches a response; only the
        verdict computed from it does.

        Arrow, not ``get()``'s JSON-safe dicts, and deliberately: the row goes
        straight back into a policy that expects the dataset's own types, and a
        timestamp round-tripped through an ISO string would fail to rebuild and
        turn a legitimate edit into a refusal.
        """
        system = self._system_view()
        with system._object_scan(
            ot, None, {ot.primary_key: pk_value}, all_columns=True
        ) as (con, sql, params, _cols, _pk):
            if con is None:
                return None
            with limits.limited(con, limits.QueryLimits.interactive()):
                table = con.execute(f"{sql} LIMIT 1", params).arrow()
            if isinstance(table, pa.RecordBatchReader):
                table = table.read_all()
        if table.num_rows == 0:
            return None
        if "__ord" in table.column_names:
            table = table.drop_columns(["__ord"])
        return table

    def _refuse_policy_escaping_update(
        self, ot: ObjectTypeDef, pk_value: str, payload: dict
    ) -> None:
        """A caller under a dataset policy may not write through it.

        An update cannot introduce a row — it merges onto a base row that
        already survived the policy — which is why this was left out for so
        long and documented as not implemented. Two things still got through:

        * **Writing over a masked column.** The overlay is applied *after* the
          policied scan, so an update to a masked property is read straight
          back in plaintext by its author, and the mask on that cell is gone
          for as long as the edit lives. Worse than the disclosure: the value
          is a blind overwrite of data the author was never allowed to read,
          and a writeback makes it the dataset's value for everyone.
        * **Moving a row out of the allowed set.** The row filter runs on the
          *base* value, so an editor confined to ``realm='valinor'`` could set
          ``realm='beleriand'`` and keep seeing the object — the mirror image
          of the create channel ``_policy_admits`` closed, except the row is
          pushed into another tenant's partition rather than pulled out of it.

        Both need the merged row re-checked under the policy, which needs the
        unmasked base row the policy has already withheld from the caller —
        hence ``_system_row``.

        **Refuse, never drop.** Silently discarding the offending properties
        would report success for a write that did not happen, which is the one
        outcome worse than either bug: an operator who is told their correction
        landed stops looking. Refusing costs an error message, and the message
        names the properties and the remedy, so the caller can retry without
        them or ask for an exemption.

        A ``delete`` is not checked. It removes a row the caller can already
        see, in full, and produces no merged row to re-check.
        """
        policy = self._policy_for_dataset(ot.backing_dataset)
        if policy is None or not payload:
            return
        refusal = (
            f"Cannot update {ot.api_name!r} object {pk_value!r}: it is governed by "
            f"a policy on dataset {ot.backing_dataset!r} that cannot be evaluated "
            f"against this edit. Ask an administrator to check the dataset policy."
        )
        before = self._system_row(ot, pk_value)
        if before is None:
            raise ValueError(refusal)
        after = before
        for col, value in payload.items():
            if col not in after.column_names:
                continue  # a declared property the dataset doesn't have
            # The *field*, not the name: set_column with a bare name mints a
            # fresh nullable field, and the two probe rows would then have
            # schemas that differ only in nullability — enough for
            # concat_tables to refuse, on a path whose failure mode is
            # refusing a legitimate edit.
            field = after.schema.field(col)
            try:
                column = pa.array([value], type=field.type)
            except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError):
                raise ValueError(refusal) from None
            after = after.set_column(after.column_names.index(col), field, column)
        probe = pa.concat_tables([before, after]).append_column(
            "__pk", pa.array(["__before", "__after"], type=pa.string())
        )
        rendered = self._under_policy(policy, probe)
        if rendered is None:
            raise ValueError(refusal)
        given = {row["__pk"]: row for row in probe.to_pylist()}
        # A column is masked when the policy renders it as something other than
        # what went in. Comparing the policy's own output against the exact
        # table handed to it is the only definition that stays true for every
        # mask kind: redact turns an integer column into the string "***",
        # null empties it, and hash rewrites it in place. Both probe rows are
        # tested because a mask over an already-null cell is invisible in the
        # before row and shows up only where the edit writes a value.
        masked = sorted(
            col for col in payload
            if col in given["__before"]
            and any(given[tag].get(col) != row.get(col) for tag, row in rendered.items())
        )
        if masked:
            raise ValueError(
                f"Cannot update {ot.api_name!r} object {pk_value!r}: "
                f"{', '.join(repr(c) for c in masked)} "
                f"{'is' if len(masked) == 1 else 'are'} masked for you on dataset "
                f"{ot.backing_dataset!r}. Writing a value you are not permitted to "
                f"read would overwrite the real one for everyone. Retry without "
                f"{'that property' if len(masked) == 1 else 'those properties'}, or "
                f"ask an administrator for a mask exemption on "
                f"{'that column' if len(masked) == 1 else 'those columns'}."
            )
        if "__after" not in rendered:
            changed = ", ".join(repr(c) for c in sorted(payload))
            raise ValueError(
                f"Cannot update {ot.api_name!r} object {pk_value!r}: setting "
                f"{changed} would move it outside the rows dataset "
                f"{ot.backing_dataset!r} lets you see. An object you can no longer "
                f"read is one you cannot correct afterwards. Choose a value inside "
                f"your access, or ask an administrator to widen it."
            )

    def _refuse_shadowing_create(self, ot: ObjectTypeDef, pk_value: str) -> None:
        """A caller under a dataset policy may not create over an existing key.

        A create for a key that already exists is a *replacement*: it takes
        over that object's ordinal in the shared materialization, and a
        writeback folds it over that row in the dataset itself. For a caller
        whose row-level security hides the existing row, that is a cross-tenant
        destructive write, and it was reachable with nothing more than EDITOR
        on the object type — measured: an editor confined to one realm replaced
        another realm's row, the store, the victim's scan and the attacker's
        scan then gave three different answers for one key, and a fold made it
        permanent and deleted the victim's data for everyone.

        ``_policy_admits`` closed the half where a policied user *reads* an
        overlay create. This is the half where they *write* one.

        The check is unpoliced, because the policied view is exactly what
        cannot see the row being clobbered, and it refuses for *any* existing
        key rather than only a hidden one — so a refusal does not distinguish
        "yours" from "someone else's". Callers with no policy on the backing
        dataset keep create-as-replacement, which is the documented behaviour
        and is not a cross-tenant operation for them.

        Residual, stated rather than hidden: this remains an existence oracle
        over the key space. A policied caller learns that *some* object holds
        the key they just named. That is inherent to a shared unique key, and
        no property of the hidden object is disclosed.
        """
        if self._policy_for_dataset(ot.backing_dataset) is None:
            return
        if self._key_is_taken(ot, pk_value):
            raise ValueError(
                f"Cannot create {ot.api_name!r} object with primary key "
                f"{pk_value!r}: an object with that key already exists. A "
                f"pending delete does not free the key — the row is still in "
                f"dataset {ot.backing_dataset!r} until the edits are written "
                f"back, and creating over it would replace that row rather "
                f"than add one."
            )

    def _key_is_taken(self, ot: ObjectTypeDef, pk_value: str) -> bool:
        """Whether `pk_value` names a row a create would replace.

        Asked of the **base dataset plus live creates**, deliberately not of
        the overlaid view. ``_system_view().get()`` applies the overlay, which
        includes the caller's own pending *delete* — so ``delete X`` then
        ``create X`` found nothing, the shadowing check passed, and the whole
        policy guard was walked around by spending one extra edit. Measured
        end to end through HTTP: an editor confined to ``realm='valinor'``
        whose ``rename`` to ``beleriand`` was correctly refused issued
        ``raze`` + ``found`` instead, got 200 on both, and moved the object
        into another tenant's partition with the masked ``founder`` and ``pop``
        columns overwritten with values of their choosing — durably, in the
        shared materialization, for every reader.

        A delete is a pending edit, not a fact. The base row is still there,
        the fold applies last-wins, and the create therefore replaces it —
        including every column the creator never named, which become null, and
        every column their masks hid from them.
        """
        try:
            version = self.catalog.store.get_version(ot.backing_dataset, None)
            base_scan = self.catalog.scan_for(ot.backing_dataset, plan_for=None)
        except KeyError:
            return False
        if version is None or ot.primary_key not in {c.name for c in version.schema_}:
            return False
        quoted = '"' + ot.primary_key.replace('"', '""') + '"'
        con = duckdb.connect()
        try:
            con.execute("SET enable_external_access=false")
            con.register("__base", base_scan)
            with limits.limited(con, limits.QueryLimits.interactive()):
                row = con.execute(
                    f"SELECT 1 FROM __base WHERE CAST({quoted} AS VARCHAR) = ? LIMIT 1",
                    [pk_value],
                ).fetchone()
        finally:
            con.close()
        if row is not None:
            return True
        # Not in the dataset, but another live create may already hold it. Read
        # unpoliced: the key this create would collide with is exactly the one
        # the caller's policy hides.
        _deleted, _updates, creates, _order = self._system_view()._overlay(ot)
        return pk_value in creates

    def _refuse_policy_escaping_create(
        self, ot: ObjectTypeDef, pk_value: str, payload: dict
    ) -> None:
        """A caller under a dataset policy may not create outside it.

        ``_policy_admits`` is the *read* half of this and was doing its job:
        a create landing outside the caller's rows is hidden from the caller.
        Hidden from the caller is not the same as not written. The edit is
        recorded, ``reindex`` materializes it under a system identity into the
        **shared** store, and every other reader — including the tenant whose
        partition it landed in — sees it. Measured with a three-tenant fixture:
        an editor restricted to ``valinor`` created a ``beleriand`` object,
        could not see it themselves, and the ``beleriand`` editor's object list
        grew by one.

        So the same question the read half asks is now asked before the write,
        and a create the policy would not return to its author is refused
        instead of being quietly filed under somebody else.

        Masks are **not** checked here, unlike on the update path. Writing a
        masked column of a *new* object overwrites nothing and discloses
        nothing — the value is the author's own. Refusing it would stop a
        policied editor from ever creating an object on a dataset with any mask
        on it, which is a large cost for no gain. The destructive case, a
        create landing on a key that already exists, is
        ``_refuse_shadowing_create``.
        """
        policy = self._policy_for_dataset(ot.backing_dataset)
        if policy is None:
            return
        refusal = (
            f"Cannot create {ot.api_name!r} object {pk_value!r}: it is governed "
            f"by a policy on dataset {ot.backing_dataset!r} that cannot be "
            f"evaluated against this edit. Ask an administrator to check the "
            f"dataset policy."
        )
        version = self.catalog.store.get_version(ot.backing_dataset, None)
        if version is None:
            raise ValueError(refusal)
        arrays = {"__pk": pa.array([pk_value], type=pa.string())}
        for column in version.schema_:
            try:
                arrays[column.name] = pa.array(
                    [payload.get(column.name)], type=pa.type_for_alias(column.type)
                )
            except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError):
                raise ValueError(refusal) from None
        rendered = self._under_policy(policy, pa.table(arrays))
        if rendered is None:
            raise ValueError(refusal)
        if pk_value not in rendered:
            raise ValueError(
                f"Cannot create {ot.api_name!r} object {pk_value!r}: it would "
                f"land outside the rows dataset {ot.backing_dataset!r} lets you "
                f"see. An object you cannot read is one you cannot correct "
                f"afterwards, and it is visible to whoever the policy does let "
                f"see those rows. Set the properties your access covers, or ask "
                f"an administrator to widen it."
            )

    def _refuse_primary_key_rewrite(
        self, ot: ObjectTypeDef, pk_value: str, payload: dict
    ) -> None:
        """An update may not move an object to a different primary key.

        The key is the object's identity: it is what the overlay files the edit
        under, what the materialization stores, and what ``writeback`` folds on.
        Rewriting it makes those three disagree, and the disagreement is not
        cosmetic.

        * **It walks straight through the policy guard.** ``rekey`` the pk to a
          key held by a row the caller cannot see: the merged row still renders
          identically (the key is unmasked) and still passes the row filter (it
          filters on a different column), so ``_refuse_policy_escaping_update``
          sees nothing wrong. ``_refuse_shadowing_create`` refuses exactly this
          collision on the create path; the update path had no equivalent.
        * **The collision destroys the other row.** Measured: after
          ``rekey city-0 -> city-1`` and a writeback, the dataset held two rows
          named ``city-1``; the object view dedups last-wins by key, so a
          subsequent delete of ``city-1`` removed *both*, taking a hidden
          tenant's row with it — six objects to four, with nothing in the audit
          trail naming the loss.
        * **Uncollided, it orphans the object.** ``rekey city-0 -> city-0b``
          leaves an edit filed under ``city-0`` that produces an object named
          ``city-0b``; the editor can then address it under neither key.

        Refused for every caller, not only policied ones: the writeback
        corruption needs no policy at all. Delete-and-create expresses the same
        intent through two operations that each have a guard.
        """
        if ot.primary_key not in payload:
            return
        new_key = payload[ot.primary_key]
        if new_key is None or str(new_key) == pk_value:
            return  # restating the key is not a rewrite
        raise ValueError(
            f"Cannot update {ot.api_name!r} object {pk_value!r}: "
            f"{ot.primary_key!r} is its primary key, and an update may not "
            f"change it to {str(new_key)!r}. The key is what the edit log, the "
            f"object store and the dataset all identify this object by, and "
            f"moving it makes them disagree — a colliding key silently deletes "
            f"the other object on the next writeback. Delete this object and "
            f"create the one you want instead."
        )

    def apply_action(
        self,
        action_name: str,
        pk: Optional[str],
        parameters: dict,
        actor: str = "anonymous",
    ) -> ObjectEdit:
        action = self.ontology.action(action_name)
        if action is None:
            raise ValueError(f"Unknown action: {action_name!r}")
        ot = self.ontology.object_type(action.object_type)
        if ot is None:
            raise ValueError(
                f"Action {action_name!r} targets unknown object type "
                f"{action.object_type!r}"
            )
        parameters = dict(parameters or {})

        for name in parameters:
            if name not in action.parameters:
                raise ValueError(
                    f"Unknown parameter {name!r} for action {action_name!r}"
                )
        for name, pdef in action.parameters.items():
            if pdef.required and (name not in parameters or parameters[name] is None):
                raise ValueError(
                    f"Missing required parameter {name!r} for action {action_name!r}"
                )

        payload = {
            name: _coerce_parameter(name, value, action.parameters[name].type)
            for name, value in parameters.items()
        }

        declared = set(ot.properties) | {ot.primary_key}
        for name in payload:
            if name not in declared:
                raise ValueError(
                    f"Parameter {name!r} is not a declared property of object type "
                    f"{ot.api_name!r}"
                )

        kind = EditKind(action.kind.value)
        if kind == EditKind.create:
            if ot.primary_key not in payload or payload[ot.primary_key] is None:
                raise ValueError(
                    f"Create action {action_name!r} requires a value for primary key "
                    f"{ot.primary_key!r} in parameters"
                )
            pk_value = str(payload[ot.primary_key])
            self._refuse_shadowing_create(ot, pk_value)
            self._refuse_policy_escaping_create(ot, pk_value, payload)
        else:
            if pk is None:
                raise ValueError(
                    f"Action {action_name!r} ({kind.value}) requires a pk"
                )
            pk_value = str(pk)
            if self.get(ot.api_name, pk_value) is None:
                raise ValueError(
                    f"No {ot.api_name!r} object with primary key {pk_value!r}"
                )
            if kind == EditKind.update:
                # After the existence check, never before: these read the row
                # under a system identity, so running them first would answer
                # for objects the caller cannot see and turn the policy check
                # itself into the enumeration oracle the check above avoids.
                #
                # The key rewrite is refused before the policy probe because it
                # is a structural rule that binds every caller, and because the
                # probe cannot see anything wrong with it — the merged row
                # renders identically and passes the same row filter.
                self._refuse_primary_key_rewrite(ot, pk_value, payload)
                self._refuse_policy_escaping_update(ot, pk_value, payload)

        edit = ObjectEdit(
            id=uuid.uuid4().hex,
            object_type=ot.api_name,
            pk_value=pk_value,
            kind=kind,
            payload=payload,
            actor=actor,
        )
        self._record_edit(ot, edit)
        self.store.log_audit(
            "action_applied",
            {
                "action": action_name,
                "object_type": ot.api_name,
                "pk_value": pk_value,
                "kind": kind.value,
                "edit_id": edit.id,
                "parameters": payload,
            },
            actor=actor,
        )
        return edit
