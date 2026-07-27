"""OntologyService: materialize objects (base data + edit overlay), traverse
links, and apply write-back actions.

Objects are computed in memory per request: the backing dataset's latest
version is read via duckdb, projected to declared properties, then the
recorded ObjectEdits are replayed in order (create / update / delete).
"""

from __future__ import annotations

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
    ):
        self.workspace = workspace
        self.catalog = catalog
        self.store = store
        self.ontology = ontology
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

    def _base_rows(self, ot: ObjectTypeDef) -> list[dict]:
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

    # -- index --------------------------------------------------------------------

    def index_state(self, ot: ObjectTypeDef) -> Optional[dict]:
        """The index's record of what it was built from, if it exists."""
        return self.store.object_index_state(ot.api_name)

    def index_is_fresh(self, ot: ObjectTypeDef) -> bool:
        """Whether the index still reflects the data.

        Two things can invalidate it: a new dataset version, or a new edit in
        the overlay. Both are cheap to check, and checking beats trusting —
        a stale index is worse than no index, because it answers confidently.
        """
        state = self.store.object_index_state(ot.api_name)
        if state is None:
            return False
        dataset = self.catalog.store.get_dataset(ot.backing_dataset)
        version = dataset.latest_version if dataset else None
        if version is None or state["dataset_version"] != version:
            return False
        return state["edit_count"] == self.store.count_object_edits(ot.api_name)

    def reindex(self, type_name: str) -> int:
        """Materialize an object type into the index. Returns the count.

        Indexing is only worthwhile for types small enough to hold in the
        metadata store — which is exactly the modelling advice anyway: entities
        in the ontology, high-volume events in datasets.
        """
        ot = self._require_object_type(type_name)
        backing = self.catalog.store.get_dataset(ot.backing_dataset)
        if backing is None or backing.is_federated or backing.latest_version is None:
            # Nothing stable to index against.
            self.store.drop_object_index(type_name)
            return 0

        string_props = [
            name for name, prop in ot.properties.items() if prop.type == "string"
        ]
        rows = []
        for obj in self._materialize(ot):
            searchable = " ".join(
                str(obj[p]) for p in string_props
                if isinstance(obj.get(p), str)
            ).lower()
            rows.append({
                "pk": obj["__pk"],
                "title": str(obj.get("__title", "")),
                "search_text": searchable,
                "props": {k: v for k, v in obj.items() if not k.startswith("__")},
            })
        self.store.replace_object_index(
            type_name, rows, backing.latest_version,
            self.store.count_object_edits(type_name),
        )
        return len(rows)

    def _index_query(
        self,
        ot: ObjectTypeDef,
        search: Optional[str],
        filters: Optional[dict[str, str]],
        limit: int,
        offset: int,
    ) -> Optional[dict]:
        """Answer from the index when it is fresh and safe to use.

        Row-level security is not represented in the index — it is per-user,
        and baking one user's view into a shared table would be a serious bug —
        so a policied user always falls through to the scan.
        """
        if self._policy_for_dataset(ot.backing_dataset) is not None:
            return None
        # Only the primary key is an indexed column. Any other filter would
        # cost a JSON extraction per row — measured slower than the DuckDB
        # scan — so those go to the scan, which prunes row groups instead.
        filters = filters or {}
        if set(filters) - {ot.primary_key}:
            return None
        if not self.index_is_fresh(ot):
            return None
        rows, total = self.store.search_object_index(
            ot.api_name, search=search, pk=filters.get(ot.primary_key),
            limit=limit, offset=offset,
        )
        objects = []
        for row in rows:
            obj = dict(row["props"])
            obj["__pk"] = row["pk"]
            obj["__title"] = row["title"]
            objects.append(obj)
        return _result(objects, total, search)

    # -- pushdown -----------------------------------------------------------------

    def _overlay(self, ot: ObjectTypeDef) -> tuple[set[str], dict[str, dict], list[dict]]:
        """Replay the edit log into (deleted pks, updates by pk, created rows).

        The overlay is a hand-edit log — thousands of entries at most, against
        datasets of millions of rows — so it is always cheap to load whole.
        """
        keep = set(ot.properties) | {ot.primary_key}
        deleted: set[str] = set()
        updates: dict[str, dict] = {}
        creates: dict[str, dict] = {}
        for edit in self.store.list_object_edits(ot.api_name):
            payload = {k: v for k, v in edit.payload.items() if k in keep}
            if edit.kind == EditKind.create:
                pk = str(payload.get(ot.primary_key, edit.pk_value))
                creates[pk] = payload
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
                updates.pop(pk, None)
                deleted.add(pk)
        return deleted, updates, creates

    @contextmanager
    def _object_scan(
        self,
        ot: ObjectTypeDef,
        search: Optional[str],
        filters: Optional[dict[str, str]],
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
        if backing is not None and backing.is_federated:
            # Every object page would become a full remote scan, and the edit
            # overlay has no stable row identity to merge against. Refuse
            # loudly rather than perform catastrophically: materialize the
            # federated table into a managed dataset with a transform and bind
            # the object type to that.
            raise ValueError(
                f"Object type {ot.api_name!r} is backed by federated dataset "
                f"{ot.backing_dataset!r}. Bind object types to managed datasets "
                f"— use a transform to materialize the rows you need."
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
        cols = [c for c in available if c in (set(ot.properties) | {pk})]

        deleted, updates, creates = self._overlay(ot)
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
                    con, cols, available, updates, creates
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
    ) -> dict[str, bool]:
        """Register the (small) update and create sets as typed Arrow tables so
        DuckDB can merge them with the base scan. Raises if a payload value
        can't be represented in the dataset's own column type."""
        registered = {"updates": False, "creates": False}

        def build(by_pk: dict[str, dict]) -> pa.Table:
            arrays = {"__pk": pa.array(list(by_pk), type=pa.string())}
            for col in cols:
                typ = pa.type_for_alias(available[col])
                arrays[col] = pa.array(
                    [row.get(col) for row in by_pk.values()], type=typ
                )
            return pa.table(arrays)

        if updates:
            con.register("__ovl_updates", build(updates))
            registered["updates"] = True
        if creates:
            con.register("__ovl_creates", build(creates))
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
            merged_cols = ", ".join(
                f"COALESCE(u.{q(c)}, b.{q(c)}) AS {q(c)}" for c in cols
            )
            body = (
                f"SELECT {merged_cols}, b.__ord FROM ({base}) b "
                f"LEFT JOIN __ovl_updates u "
                f"ON CAST(b.{q(pk)} AS VARCHAR) = u.__pk"
            )
        else:
            body = f"SELECT {projection}, __ord FROM ({base})"

        if registered["creates"]:
            # Created objects are appended after the base rows, as in-memory
            # materialization does.
            body = (
                f"{body} UNION ALL SELECT {projection}, "
                f"9223372036854775807 AS __ord FROM __ovl_creates"
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

        sql = f"SELECT * FROM ({body}) o"
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
        """Base rows with the edit overlay applied, in stable order."""
        keep = set(ot.properties) | {ot.primary_key}
        objects: dict[str, dict] = {}
        for row in self._base_rows(ot):
            objects[str(row.get(ot.primary_key))] = row
        for edit in self.store.list_object_edits(ot.api_name):
            if edit.kind == EditKind.create:
                payload = {k: v for k, v in edit.payload.items() if k in keep}
                pk = str(payload.get(ot.primary_key, edit.pk_value))
                objects[pk] = payload
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

    def edits(self, type_name: str) -> list[ObjectEdit]:
        return self.store.list_object_edits(type_name)

    # -- actions ------------------------------------------------------------------

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

        edit = ObjectEdit(
            id=uuid.uuid4().hex,
            object_type=ot.api_name,
            pk_value=pk_value,
            kind=kind,
            payload=payload,
            actor=actor,
        )
        self.store.add_object_edit(edit)
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
