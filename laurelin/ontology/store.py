"""The operational object store: a pluggable materialization of object state.

The edit log in the metadata store is the **source of truth**. Everything here
is a *materialization* of it, and the whole design follows from one fact: a
materialization can lag, and for a remote one it can lag across a network with
no shared transaction. So a lagging store must be **detectable and replayable**,
never silently wrong.

Three rules carry the whole thing:

1. **The log commits first, the materialization catches up, the watermark
   advances last.** A watermark *behind* the data costs one redundant,
   idempotent replay. A watermark *ahead* of the data is a silent permanent
   stale read. The safe direction is mandatory, not preferred.

2. **A store that is behind never answers.** It returns ``None`` and the caller
   falls through to the scan path, which reads the edit log directly and is
   therefore always correct — slower, never wrong. That fallback is the entire
   safety argument, and it is why this stays an optimization rather than a
   correctness dependency. *Unreachable counts as behind*, and so does a missing
   state row, a dropped table, and a digest mismatch. Never return empty: an
   empty page is a valid-looking answer meaning "these objects do not exist",
   and the write path's existence check would act on it.

3. **Conflicts resolve by log position**, never arrival order or wall clock.
   Each row carries the ``applied_seq`` it was written at, and an apply whose
   position is not strictly newer is a no-op. ``created_at`` is unusable: it is
   a TEXT timestamp from the writing replica's clock, with sub-second
   collisions already observed.

Two implementations ship:

* :class:`MetadataObjectStore` — the default, co-located with the edit log in
  PostgreSQL/SQLite, so "append the edit" and "apply it" are **one
  transaction** and the caught-up check is exact. Zero new dependencies.
* :class:`StarRocksObjectStore` — for scale. Postgres and StarRocks share no
  transaction, so exactly one gap remains (between the log commit and the
  load), and that gap is precisely the lag the watermark exposes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

# Ordinal base for objects that exist only in the overlay.
#
# A created object's ordinal must sort after every base row and every earlier
# create, be computable at write time without reading the store or renumbering,
# and be reproducible by the scan path. Dense `n, n+1, ...` fails all three: `n`
# is the base row count, which changes with the next dataset version, and two
# racing creates compute the same `n`. `edit_seq` is already monotonic and
# already allocated in the append transaction. 2**62 + seq fits signed int64 on
# SQLite, Postgres, DuckDB and StarRocks alike.
ORD_CREATED_BASE = 2 ** 62


def created_ord(edit_seq: int) -> int:
    return ORD_CREATED_BASE + int(edit_seq)


# -- the divergence digest ---------------------------------------------------
#
# A watermark cannot detect divergence: a store that has drifted can be
# perfectly caught up by position. So content gets its own check.
#
# Combined by XOR, which is what makes it maintainable incrementally — a row's
# contribution can be removed and re-added on update without recomputing the
# whole set — and order-independent by construction, so any shuffle of the edit
# stream that reaches the same state yields the same digest.
#
# Computed in Python from the canonical text and stored, never recomputed by
# the engine: JSON key order and number formatting do not survive a round trip
# through a SQL engine, and a digest that disagrees with itself is worse than
# no digest.

def canonical_props(props: dict) -> str:
    """The one text form of a property bag. Written to the store *and* hashed,
    so the two can never disagree about what a row contains."""
    return json.dumps(props, sort_keys=True, separators=(",", ":"), default=str)


def row_digest(pk: str, applied_seq: int, props_json: str) -> int:
    """One row's contribution. Length-prefixed so no two different rows can
    concatenate to the same bytes."""
    payload = f"{len(pk)}:{pk}|{int(applied_seq)}|{props_json}"
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:16], "big")


def digest_of_rows(rows) -> int:
    combined = 0
    for row in rows:
        combined ^= row_digest(row["pk"], row["applied_seq"], row["props_json"])
    return combined


def digest_hex(value: int) -> str:
    return f"{value:032x}"


def definition_fingerprint(**parts) -> str:
    """A stable hash of an object-type definition.

    Separate from the row digest and doing a different job: the digest asks
    "do the rows still say what the log says", this asks "were these rows even
    built from the definition we are serving them under". Nothing asked the
    second question, so withdrawing a property from the ontology left the
    materialization projecting it forever while every other path had stopped.
    """
    return hashlib.sha256(canonical_props(parts).encode("utf-8")).hexdigest()[:32]


def digest_int(value: str) -> int:
    return int(value, 16) if value else 0


def advance_digest(current: str, before, after) -> str:
    """Remove the old images, add the new ones. Equal to a from-scratch digest
    of the resulting set — that equality is a test, not a hope."""
    return digest_hex(digest_int(current) ^ digest_of_rows(before) ^ digest_of_rows(after))


# -- the interface -----------------------------------------------------------

@dataclass(frozen=True)
class ObjectRow:
    """One materialized object. ``ord`` is only consulted when the row is new:
    an object keeps the position it already had, so a write never reshuffles
    the page someone is looking at."""

    pk: str
    ord: int
    title: str
    search_text: str
    props: dict
    # The log position this row was last written at. Carried on the row and not
    # only in the watermark, so a full rebuild reproduces the same per-row
    # positions an incremental stream would have produced — which is what makes
    # the digest of a rebuild equal the digest of the increments.
    applied_seq: int = 0

    def as_dict(self) -> dict:
        return {"pk": self.pk, "ord": int(self.ord), "title": self.title,
                "search_text": self.search_text,
                "applied_seq": int(self.applied_seq),
                "props_json": canonical_props(self.props)}


@dataclass(frozen=True)
class StoreState:
    """What a materialization says it was built from."""

    dataset_version: int
    applied_seq: int
    digest: str = ""
    object_count: int = 0
    built_at: str = ""
    store: str = "metadata"
    # Hash of the object-type definition this was built from. The watermark
    # describes the data; this describes the *shape*. Without it, withdrawing a
    # property from the ontology left the store serving it indefinitely while
    # every other read path had already stopped.
    fingerprint: str = ""

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["StoreState"]:
        if d is None:
            return None
        return cls(
            dataset_version=int(d["dataset_version"]),
            applied_seq=int(d["applied_seq"]),
            digest=d.get("digest", ""),
            object_count=int(d.get("object_count", 0)),
            built_at=d.get("built_at", ""),
            store=d.get("store", "metadata"),
            fingerprint=d.get("fingerprint", ""),
        )


@runtime_checkable
class ObjectStore(Protocol):
    """A materialization of one workspace's objects.

    Deliberately absent:

    * ``get(pk)`` — it is ``page(pk=..., limit=1)``. A second method would be a
      second definition of "the object with this key", and the two would
      eventually disagree.
    * aggregation and arbitrary-property filters — those recreate the per-row
      JSON extraction the materialization exists to avoid (measured slower than
      the DuckDB scan) and would give the ontology two aggregation
      implementations that must agree.
    """

    name: str

    def state(self, object_type: str) -> Optional[StoreState]:
        """What this store was built from, or ``None``.

        ``None`` collapses "never materialized" and "unreachable" deliberately:
        the caller must not be able to tell them apart, because the correct
        response — fall through to the scan — is identical.
        """
        ...

    def replace(self, object_type: str, rows: list[ObjectRow], *,
                dataset_version: int, applied_seq: int, digest: str,
                fingerprint: str) -> None:
        ...

    def drop(self, object_type: str) -> None:
        ...

    def commit_edit(self, edit, *, pks: list[str], build) -> int:
        """Append the edit to the log and apply it here. Returns its ``edit_seq``.

        Takes the whole edit rather than letting the caller write the log
        itself, because that is the only way a co-located backend can put both
        in one transaction — and it makes the ordering rule a property of the
        implementation instead of a comment at the call site.

        **The rows are built here, not by the caller.** ``build(pre_image,
        seq)`` is invoked by the implementation with the current rows for
        ``pks`` and the position the edit actually got, and returns
        ``(upserts, deletes)`` or ``None`` for "cannot express this as rows"
        (which no shipped builder does today — the service writes no rows
        rather than declining, and raises for anything genuinely impossible).
        An earlier version took finished rows, which made every write a
        read-modify-write with the read outside the write's transaction — a
        lost update under two concurrent edits to one object, and a resurrected
        row under a concurrent delete.

        **Contract on failure: all or nothing.** Either the edit is in the log
        and its ``edit_seq`` is returned, or nothing was logged and this
        raises. The caller's recovery path appends the edit itself, so a
        half-committed write here would be logged twice — or, for a store that
        logs first, reported to the user as a failure while durably visible to
        every reader.
        """
        ...

    def apply_edit(self, edit, *, pks: list[str], build) -> None:
        """Apply an edit that is *already* in the log, advancing the watermark
        to ``edit.edit_seq``. Catch-up, and idempotent: every edit kind is an
        absolute assignment, so replaying one is a no-op."""
        ...

    def page(self, object_type: str, *, search: Optional[str] = None,
             pk: Optional[str] = None, limit: int = 100, offset: int = 0
             ) -> Optional[tuple[list[dict], int, StoreState]]:
        """One page plus the state it was read with, or ``None`` if unavailable.

        The state is returned *with* the page rather than checked before it, so
        the caller validates the state the rows actually came from.
        """
        ...


# -- the default: co-located with the edit log -------------------------------

class MetadataObjectStore:
    """Objects materialized in the metadata store itself (the ``object_index``
    table), beside the edit log.

    Its atomicity argument in one line: ``MetadataStore._conn()`` opens a new
    connection per call and commits on clean exit, so one ``with`` block is one
    transaction — and therefore atomicity *cannot* be obtained by composing
    store calls. That is why ``commit_object_edit`` exists as a single method
    doing all four writes rather than as a helper that calls two.
    """

    name = "metadata"

    def __init__(self, store):
        self.store = store

    def state(self, object_type: str) -> Optional[StoreState]:
        return StoreState.from_dict(self.store.object_index_state(object_type))

    def replace(self, object_type: str, rows: list[ObjectRow], *,
                dataset_version: int, applied_seq: int, digest: str,
                fingerprint: str = "") -> None:
        self.store.replace_object_index(
            object_type, [r.as_dict() for r in rows],
            dataset_version, applied_seq, digest, fingerprint,
        )

    def drop(self, object_type: str) -> None:
        self.store.drop_object_index(object_type)

    def rows_for(self, object_type: str, pks: list[str]) -> dict[str, dict]:
        """Current rows for these keys, on their own connection.

        Diagnostics and tests only. **Not** a pre-image source for a write:
        a row read here and merged into a write issued later is a
        read-modify-write across two transactions. The write path gets its
        pre-image from ``build``'s argument instead, inside the transaction
        that writes.
        """
        return {r["pk"]: r for r in self.store.object_index_rows(object_type, pks)}

    @staticmethod
    def _rows_out(built):
        """The builder's ObjectRows as the metadata store wants them, or None."""
        if built is None:
            return None
        upserts, deletes = built
        return [r.as_dict() for r in upserts], [str(p) for p in deletes]

    def commit_edit(self, edit, *, pks: list[str], build) -> int:
        return self.store.commit_object_edit(
            edit,
            pks=[str(p) for p in pks],
            build=lambda pre, seq: self._rows_out(build(pre, seq)),
            digest_of=advance_digest,
        )

    def apply_edit(self, edit, *, pks: list[str], build) -> None:
        self.store.apply_object_edit(
            edit.object_type, edit.edit_seq,
            pks=[str(p) for p in pks],
            build=lambda pre, seq: self._rows_out(build(pre, seq)),
            digest_of=advance_digest,
        )

    def page(self, object_type: str, *, search=None, pk=None,
             limit: int = 100, offset: int = 0):
        rows, total, state = self.store.search_object_index(
            object_type, search=search, pk=pk, limit=limit, offset=offset
        )
        if state is None:
            return None  # no state row -> behind, by rule 2
        return rows, total, StoreState.from_dict(state)


# -- StarRocks ---------------------------------------------------------------

# The table this store expects. Every non-key column is NULLable *with a
# default*, which is a measured requirement rather than a style choice: a
# `__op=delete` load carries only the key, and without the defaults it fails
# with "Column has no default value: ord".
#
# The table name carries the workspace, so cross-workspace isolation is
# structural rather than a predicate someone has to remember — Postgres metadata
# is schema-per-workspace today, which is why this is a regression risk worth
# spending DDL on. It is also deliberately NOT registerable as a dataset:
# otherwise dataset ACLs would silently stand in for ontology grants.
STARROCKS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
  object_type VARCHAR(128) NOT NULL,
  pk          VARCHAR(512) NOT NULL,
  ord         BIGINT NULL DEFAULT "0",
  applied_seq BIGINT NULL DEFAULT "0",
  title       VARCHAR(1024) NULL,
  search_text STRING NULL,
  props_json  STRING NULL
) PRIMARY KEY(object_type, pk)
  DISTRIBUTED BY HASH(object_type, pk)
"""


def starrocks_table_name(workspace: str) -> str:
    return f"objects__{workspace}"


class StarRocksObjectStore:
    """Objects materialized into a StarRocks primary-key table.

    **UNVERIFIED against a real StarRocks server.** The Track A agent measured
    the engine behaviours this relies on — Stream Load is byte-faithful with no
    escaping, upsert-by-pk works, a bad row aborts the whole load, and data rows
    plus a sentinel land in one transaction — but *this class* has only been
    exercised against the in-memory double in ``tests/test_object_store.py``.
    Nothing here has run against a live server. Treat the SQL and the HTTP
    shapes as the best reading of measured behaviour, not as tested code.

    Design points that are not negotiable:

    * **Writes go via Stream Load, never SQL.** ``StarRocksDialect.literal()``
      raises by construction, and it must: stacked statements execute on
      StarRocks and the client's MULTI_STATEMENTS flag does not stop them, so a
      string-concatenated policy value is a remote write primitive. Stream Load
      takes JSON over HTTP — the value never enters a SQL grammar, so there is
      no escaping question to get wrong.
    * **The watermark lives in StarRocks**, as a reserved row with ``ord = -1``,
      excluded from every page by an ``ord >= 0`` predicate. It lands in the
      same load as the data, which collapses "apply the delta" and "advance the
      watermark" into one atomic step and leaves exactly one non-atomic gap —
      between the log commit and the load — which is precisely the lag the
      watermark exposes. Keeping it in Postgres would reintroduce a gap in both
      directions *and* keep making claims about a table that could have been
      dropped underneath it.
    * **Rebuild is not atomic here.** ``replace()`` publishes the watermark row
      only after the data rows have committed, and a table with rows but no
      watermark row falls back rather than answering.
    """

    name = "starrocks"

    WATERMARK_PK = "__watermark__"
    WATERMARK_ORD = -1

    def __init__(self, metadata_store, client, table: str):
        # The edit log always lives in the metadata store: it is the source of
        # truth and StarRocks is a materialization of it, never the reverse.
        self.metadata = metadata_store
        self.client = client
        self.table = table

    # -- reads ---------------------------------------------------------------

    def state(self, object_type: str) -> Optional[StoreState]:
        try:
            rows = self.client.query(
                f"SELECT props_json FROM `{self.table}` "
                f"WHERE object_type = ? AND pk = ? AND ord = ?",
                [object_type, self.WATERMARK_PK, self.WATERMARK_ORD],
            )
        except Exception:
            # Unreachable is indistinguishable from never-built on purpose:
            # both mean "do not answer from here".
            return None
        if not rows:
            return None
        return StoreState.from_dict({**json.loads(rows[0]["props_json"]),
                                     "store": self.name})

    def page(self, object_type: str, *, search=None, pk=None,
             limit: int = 100, offset: int = 0):
        state = self.state(object_type)
        if state is None:
            return None
        where = ["object_type = ?", "ord >= 0"]
        params: list = [object_type]
        if search:
            where.append("search_text LIKE ?")
            params.append(f"%{search.lower()}%")
        if pk is not None:
            where.append("pk = ?")
            params.append(str(pk))
        clause = " AND ".join(where)
        try:
            total = self.client.query(
                f"SELECT count(*) AS n FROM `{self.table}` WHERE {clause}", list(params)
            )[0]["n"]
            rows = self.client.query(
                f"SELECT pk, ord, applied_seq, title, props_json FROM `{self.table}` "
                f"WHERE {clause} ORDER BY ord LIMIT ? OFFSET ?",
                [*params, max(0, limit), max(0, offset)],
            )
        except Exception:
            return None
        return (
            [{"pk": r["pk"], "ord": int(r["ord"]), "applied_seq": int(r["applied_seq"]),
              "title": r["title"], "props": json.loads(r["props_json"])} for r in rows],
            int(total),
            state,
        )

    # -- writes --------------------------------------------------------------

    def _record(self, object_type: str, row: dict, applied_seq: int, op: int = 0) -> dict:
        return {"object_type": object_type, "pk": row["pk"], "ord": int(row["ord"]),
                "applied_seq": int(applied_seq), "title": row.get("title", ""),
                "search_text": row.get("search_text", ""),
                "props_json": row.get("props_json", "{}"), "op": op}

    def _watermark_record(self, object_type: str, state: StoreState) -> dict:
        return {
            "object_type": object_type, "pk": self.WATERMARK_PK,
            "ord": self.WATERMARK_ORD, "applied_seq": state.applied_seq,
            "title": "", "search_text": "",
            "props_json": canonical_props({
                "dataset_version": state.dataset_version,
                "applied_seq": state.applied_seq,
                "digest": state.digest,
                "object_count": state.object_count,
                "built_at": state.built_at,
                "fingerprint": state.fingerprint,
            }),
            "op": 0,
        }

    def replace(self, object_type: str, rows: list[ObjectRow], *,
                dataset_version: int, applied_seq: int, digest: str,
                fingerprint: str = "") -> None:
        from laurelin.core.models import utcnow_iso

        # Drop first so a shrinking rebuild cannot leave orphans behind: an
        # upsert-only load would keep every key the new build no longer has.
        self.client.execute(
            f"DELETE FROM `{self.table}` WHERE object_type = ?", [object_type]
        )
        state = StoreState(dataset_version=dataset_version, applied_seq=applied_seq,
                           digest=digest, object_count=len(rows),
                           built_at=utcnow_iso(), store=self.name,
                           fingerprint=fingerprint)
        if rows:
            self.client.stream_load(
                self.table,
                [self._record(object_type, r.as_dict(), r.applied_seq) for r in rows],
            )
        # The watermark goes last and on its own: a table holding rows without
        # a watermark row falls back rather than answering, so a crash between
        # the two loads is a slow read, not a wrong one.
        self.client.stream_load(self.table, [self._watermark_record(object_type, state)])

    def drop(self, object_type: str) -> None:
        self.client.execute(
            f"DELETE FROM `{self.table}` WHERE object_type = ?", [object_type]
        )

    def rows_for(self, object_type: str, pks: list[str]) -> dict[str, dict]:
        if not pks:
            return {}
        placeholders = ", ".join("?" for _ in pks)
        rows = self.client.query(
            f"SELECT pk, ord, applied_seq, title, search_text, props_json "
            f"FROM `{self.table}` WHERE object_type = ? AND ord >= 0 "
            f"AND pk IN ({placeholders})",
            [object_type, *[str(p) for p in pks]],
        )
        return {r["pk"]: dict(r) for r in rows}

    def commit_edit(self, edit, *, pks: list[str], build) -> int:
        """Log first, then load. The gap between them is the lag, and the lag is
        exactly what the watermark reports.

        Two rules make that gap safe rather than merely documented:

        * **The load is attempted only from the position immediately behind
          this edit.** There is no shared transaction to serialize the
          read-modify-write against, so instead of hoping, we check: if the
          watermark is not exactly ``seq - 1``, some other writer's edit is
          unaccounted for here and this pre-image may be stale. Skip the load.
          The store stays behind, reads fall through to the scan, and
          ``catch_up`` replays in log order — which *is* single-writer.
        * **A failed load is not a failed write.** The log commit already
          happened and is durable, so raising here would tell the caller their
          write failed while every reader can see it — and the caller's
          recovery path would append the same edit a second time. It is caught,
          and the resulting lag is reported by the watermark.
        """
        self._require_state(edit.object_type)
        seq = self.metadata.add_object_edit(edit)
        try:
            self._apply_at(edit, seq, pks, build)
        except Exception:  # noqa: BLE001 - the log is committed; lag, don't lose
            pass
        return seq

    def apply_edit(self, edit, *, pks: list[str], build) -> None:
        self._apply_at(edit, int(edit.edit_seq), pks, build)

    def _apply_at(self, edit, seq: int, pks: list[str], build) -> None:
        state = self._require_state(edit.object_type)
        if state.applied_seq != seq - 1:
            # Either we are catching up out of order or another writer landed
            # between our log append and here. Leaving the lag is the safe
            # direction; applying anyway would write a row merged onto a
            # pre-image that no longer describes the object.
            return
        before = self.rows_for(edit.object_type, [str(p) for p in pks])
        built = build(before, seq)
        if built is None:
            return  # not expressible as rows: stay behind and let reads fall through
        upserts, deletes = built
        self._load_delta(edit.object_type, seq, upserts, deletes, state, before,
                         built_at=edit.created_at)

    def _require_state(self, object_type: str) -> StoreState:
        state = self.state(object_type)
        if state is None:
            raise ValueError(
                f"Object type {object_type!r} is not materialized in "
                f"{self.name!r}; append to the log instead."
            )
        return state

    def _load_delta(self, object_type: str, seq: int, upserts, deletes,
                    state: StoreState, before: dict, built_at: str) -> None:
        records = [self._record(object_type, r.as_dict(), seq) for r in upserts]
        records += [
            self._record(object_type, {"pk": pk, "ord": 0, "props_json": "{}"},
                         seq, op=1)
            for pk in deletes
        ]
        after = [
            {"pk": r.pk, "applied_seq": seq, "props_json": canonical_props(r.props)}
            for r in upserts
        ]
        added = len([r for r in upserts if r.pk not in before])
        removed = len([pk for pk in deletes if pk in before])
        new_state = StoreState(
            dataset_version=state.dataset_version,
            applied_seq=max(int(seq), state.applied_seq),
            digest=advance_digest(state.digest, list(before.values()), after),
            object_count=state.object_count + added - removed,
            built_at=built_at, store=self.name,
            # Carried, never recomputed: the definition a materialization was
            # built from does not change because a row did.
            fingerprint=state.fingerprint,
        )
        # Data rows and the watermark in ONE load: a bad row aborts the whole
        # thing, so the watermark can never advance past rows that did not land.
        self.client.stream_load(
            self.table, records + [self._watermark_record(object_type, new_state)]
        )
