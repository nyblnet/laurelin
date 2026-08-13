"""Shared data models. These are the contracts between all Laurelin modules.

Every module (catalog, transforms, ontology, api) speaks in these types.
Keep this file dependency-light: pydantic, plus three leaf modules that sit
*below* it — ``roles`` (privilege), ``audience`` (who a field was written for)
and ``failure`` (a structured, Laurelin-authored failure). All three are
imported here rather than the other way round, because every model that crosses
the API boundary has to declare an audience and several of them hold a
``Failure``.

**Reading a model in this file means reading two things about each field: its
type, and who it was written for.** A field with no ``Audience`` annotation is
OPERATIONAL — withheld from anyone below the record's ``laurelin_author_role``.
That default is deliberate and is argued in ``laurelin/core/audience.py``: a
field added tomorrow by somebody who has not read that file discloses nothing,
and the author has to opt *in* to viewer-visibility in a line somebody else can
grep for.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from laurelin.core.audience import Audience, AuthoredBy, Governed
from laurelin.core.failure import Failure
from laurelin.core.roles import Role


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

class ColumnSchema(Governed):
    # A column name and its arrow type. Both are PRESENTATION: they are the
    # shape of data a viewer is entitled to read, and the table header is
    # useless without them.
    laurelin_author_role: ClassVar[Role] = Role.editor
    name: Annotated[str, Audience.PRESENTATION]
    type: Annotated[str, Audience.PRESENTATION]  # arrow type, e.g. "string", "int64"


# Every dataset kind, mapped to the SQL dialect a policy must be rendered in
# for it. **Total by construction and with no default**: a kind that is not a
# key here has no dialect, and asking for one raises.
#
# The alternative -- `"clickhouse" if kind == "clickhouse" else "duckdb"` --
# was live until StarRocks, and it is the shape that makes a governance bug
# out of a forgotten line. A new source-scanned kind added without touching
# it would be read with DuckDB's *flat* statement and DuckDB's *quoter*, and
# the guard in `catalog.source_table` that compares the policy's dialect with
# the reader's could not catch it: both sides would say "duckdb".
_SQL_DIALECTS: dict[str, str] = {
    "managed": "duckdb",
    "federated": "duckdb",
    "iceberg": "duckdb",
    "clickhouse": "clickhouse",
    "starrocks": "starrocks",
}

# Kinds read through the source expression rather than local Parquet parts.
_SCANNED_AT_SOURCE = frozenset({"federated", "iceberg", "clickhouse", "starrocks"})

DATASET_KINDS = tuple(_SQL_DIALECTS)


# The keys a dataset's source descriptor may carry, and the shape each value
# must have. This is an **allowlist over Laurelin's own vocabulary**, not a
# denylist over a connector's: a key that is not named here is absent from the
# descriptor whatever it is called, whatever it contains, and whoever added it.
#
# Contrast with what this replaced. `redact_mapping` walked the *config's* keys
# and decided, per key, whether the value looked like a credential — a guess
# about somebody else's vocabulary, and the thing three adversarial rounds kept
# walking around. Here there is no guess: `url`, `path`, `dsn`, `options`,
# `odbc`, `conninfo` and every key nobody has thought of yet are all equally
# absent, because they are not on this list.
_DESCRIPTOR_KEYS = ("type", "table", "format", "catalog", "database", "namespace", "branch")

# The shape a descriptor value must have to be disclosed. A dotted/underscored
# identifier — which is what a table, catalog, database, namespace, branch or
# format name is. NOT a credential matcher: it is a positive shape rule, and it
# admits no `:` `/` `@` `=` `?` `'` `"` `,` `;` or space, so a libpq conninfo,
# an ODBC keyword string, a JDBC URL, an s3:// path, a `CREATE SECRET` body, a
# PEM block and an AWS key pair are all excluded by construction rather than by
# recognition. Same trick as `Failure.exc_class`, same reason: this question is
# decidable and "does this contain a secret" is not.
_DESCRIPTOR_VALUE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")

# Laurelin-authored keys the importer stamps into `source`. Booleans and short
# first-party strings; the Datasets screen draws its "cannot be read yet" pill
# from them, and a viewer needs that pill as much as an admin does — in a
# governance product an empty result is indistinguishable from a working row
# policy.
_IMPORT_SENTINELS = {"__laurelin_needs_credentials": "needs_credentials",
                     "__laurelin_data_state": "data_state"}


def source_descriptor(source: dict[str, Any]) -> dict[str, Any]:
    """Which table, in which format — and nothing about where it lives.

    What an editor or a viewer is told about a federated dataset. Everything
    here is either a value Laurelin chose or a value that passed a positive
    shape gate; nothing is a redaction of author text.
    """
    if not isinstance(source, dict) or not source:
        return {}
    out: dict[str, Any] = {}
    for key in _DESCRIPTOR_KEYS:
        value = source.get(key)
        if isinstance(value, str) and _DESCRIPTOR_VALUE_RE.match(value):
            out[key] = value
    for raw, name in _IMPORT_SENTINELS.items():
        value = source.get(raw)
        if isinstance(value, bool):
            out[name] = value
        elif isinstance(value, str) and _DESCRIPTOR_VALUE_RE.match(value):
            out[name] = value
    return out


class DatasetInfo(Governed):
    laurelin_author_role: ClassVar[Role] = Role.editor

    name: Annotated[str, Audience.PRESENTATION]
    description: Annotated[str, Audience.PRESENTATION] = ""
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    latest_version: Annotated[Optional[int], Audience.PRESENTATION] = None
    # "managed":   Laurelin owns the Parquet and versions it.
    # "federated": the bytes live elsewhere (Iceberg/Delta/Parquet/Postgres);
    #              Laurelin governs the table and scans it in place, so it has
    #              no versions.
    # "iceberg":   Laurelin owns an Iceberg table — writable and versioned like
    #              managed, but read at source like federated, and readable by
    #              Spark/Trino/DuckDB without Laurelin.
    # "clickhouse": read-only, scanned in place by embedded ClickHouse (chdb).
    #              Like federated in every way a reader cares about, except
    #              that the SQL is a different dialect — see `sql_dialect`.
    # "starrocks": read-only, scanned in place by a StarRocks server over the
    #              MySQL wire protocol. Its own dialect again, and unlike
    #              ClickHouse it is a *remote* engine with write privileges to
    #              lose — see laurelin/core/starrocks.py.
    kind: Annotated[str, Audience.PRESENTATION] = "managed"
    # ADMIN-authored, inside an editor-authored record. The three routes that
    # write it — PUT /datasets/{name}/federated, /clickhouse, /starrocks — are
    # all AdminDep, and `set_dataset_source` is reachable from nowhere else.
    #
    # This annotation is the fix for the last confirmed place where somebody's
    # confidentiality rested on the free-text matcher. Before it, an EDITOR read
    # this dict with `redaction.redact_mapping` in front, and five live
    # credentials went through: `password='...'` (quoted, so `_KEYWORD_SECRET_RE`
    # missed it), `Pwd='...'`, `Password:...`, a bare AWS key pair and a
    # positional JDBC URL. One case was withheld — and only because the matcher
    # happened to fire on it.
    source: Annotated[dict[str, Any], AuthoredBy(Role.admin)] = Field(default_factory=dict)
    # What an editor and a viewer get instead: shape, not connection. Built by
    # `source_descriptor` from an allowlist, never by subtracting from the dict.
    source_descriptor: Annotated[dict[str, Any], Audience.PRESENTATION] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _derive_source_descriptor(self) -> "DatasetInfo":
        """Keep the descriptor in step with the source, wherever it is built.

        Derived rather than stored: there is no second column to forget to
        update, and no code path that can construct a `DatasetInfo` carrying a
        descriptor that disagrees with the config it describes.
        """
        object.__setattr__(self, "source_descriptor", source_descriptor(self.source))
        return self

    @property
    def is_federated(self) -> bool:
        return self.kind == "federated"

    @property
    def is_iceberg(self) -> bool:
        return self.kind == "iceberg"

    @property
    def is_clickhouse(self) -> bool:
        return self.kind == "clickhouse"

    @property
    def is_starrocks(self) -> bool:
        return self.kind == "starrocks"

    @property
    def scans_at_source(self) -> bool:
        """Read via the source expression rather than local Parquet parts.

        The distinction that matters to a *reader* is not who owns the table
        but where the scan happens — so federated, Iceberg, ClickHouse and
        StarRocks share every read path, and with it one implementation of how
        policy is applied.
        """
        return self.kind in _SCANNED_AT_SOURCE

    @property
    def sql_dialect(self) -> str:
        """Which SQL dialect a policy must be rendered in for this dataset.

        Until ClickHouse, ``scans_at_source`` silently encoded two facts —
        "read via the source expression" *and* "DuckDB renders the SQL". They
        diverge here, and in a governance layer a derivation that drifts is a
        leak rather than a wrong number, so the second fact gets a name and one
        definition instead of N call sites re-deriving it.

        Raises on an unknown kind rather than falling back to DuckDB. A
        fallback is the wrong default in exactly one direction: the engine that
        gets read with the wrong dialect is the *new* one, and the wrong
        dialect is the one whose quoter and statement shape were never checked
        against it.
        """
        try:
            return _SQL_DIALECTS[self.kind]
        except KeyError:
            raise ValueError(
                f"Dataset {self.name!r} has kind {self.kind!r}, which declares "
                "no SQL dialect. Add it to _SQL_DIALECTS in "
                "laurelin/core/models.py — reading it with another engine's "
                "dialect would render its policy in a language it does not "
                "speak."
            ) from None


class DatasetVersionInfo(Governed):
    laurelin_author_role: ClassVar[Role] = Role.editor

    dataset: Annotated[str, Audience.PRESENTATION]
    version: Annotated[int, Audience.PRESENTATION]
    # Iceberg only: the snapshot this version pins, so a Laurelin version
    # number and an Iceberg snapshot mean the same point in history.
    snapshot_id: Annotated[Optional[int], Audience.PRESENTATION] = None
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    row_count: Annotated[int, Audience.PRESENTATION] = 0
    schema_: Annotated[list[ColumnSchema], Audience.PRESENTATION] = Field(
        default_factory=list, alias="schema"
    )
    # OPERATIONAL: server-side storage layout. `path` and `files` are where the
    # bytes live on the server (or in the bucket), which is deployment
    # information, not something a version's reader needs.
    path: str = ""  # workspace-relative directory of this version
    # Workspace-relative Parquet part files making up this version. A version
    # written by `append` lists its predecessor's parts plus the new one, so an
    # append costs O(delta) instead of rewriting the dataset. Empty means
    # "every *.parquet under `path`" — the layout used before manifests, still
    # read correctly.
    files: list[str] = Field(default_factory=list)
    build_id: Annotated[Optional[str], Audience.PRESENTATION] = None
    # A closed set of Laurelin-authored tokens, not free text.
    source: Annotated[str, Audience.PRESENTATION] = "upload"

    model_config = ConfigDict(populate_by_name=True)


class ObjectAppInfo(Governed):
    """A curated view over one object type.

    The ontology explorer is generic: every type, every property, every action.
    An *app* is the opposite — one type, the columns that matter, the filters
    that scope it, and only the actions an operator should reach for. Same
    data and the same permissions; a narrower, nameable surface.

    Configuration, not code: everything here is declarative, so an app is
    something you define rather than a frontend you build.
    """

    laurelin_author_role: ClassVar[Role] = Role.admin

    name: Annotated[str, Audience.PRESENTATION]
    title: Annotated[str, Audience.PRESENTATION] = ""
    description: Annotated[str, Audience.PRESENTATION] = ""
    # An ontology api_name. Structural, like a dataset name on /lineage, and
    # the app page cannot fetch anything without it.
    object_type: Annotated[str, Audience.PRESENTATION]
    # Empty means "every declared property", in ontology order.
    columns: Annotated[list[str], Audience.PRESENTATION] = Field(default_factory=list)
    # OPERATIONAL, despite sitting between two captions: this is a filter
    # *expression*, authored for the machine. The server applies it either way,
    # so withholding it costs the reader nothing.
    filters: dict[str, str] = Field(default_factory=dict)
    search_placeholder: Annotated[str, Audience.PRESENTATION] = ""
    # Empty means "every action available on the type"; naming them keeps an
    # operational app to the handful of operations it is actually about.
    actions: Annotated[list[str], Audience.PRESENTATION] = Field(default_factory=list)
    # Link types to show as panels on the detail view.
    links: Annotated[list[str], Audience.PRESENTATION] = Field(default_factory=list)

    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    created_by: str = ""
    updated_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)


class ScheduleInfo(Governed):
    """A trigger bound to an action — the piece that makes a pipeline run
    without anyone pressing a button.

    Every field is OPERATIONAL. A schedule is a machine instruction end to end
    — a cron expression, a target list, a source name — and both its routes are
    editor-gated, so the reader who is entitled to it is the reader who wrote
    it. There is nothing here authored *for* a viewer.
    """

    laurelin_author_role: ClassVar[Role] = Role.editor

    name: str
    enabled: bool = True
    # "cron": fire on a schedule. "upstream": fire when a dataset gains a
    # version, so a pipeline follows its inputs instead of a clock.
    trigger: str = "cron"
    cron: str = ""
    upstream_dataset: str = ""
    # "build" (optionally specific targets) or "sync" (one connector source).
    action: str = "build"
    targets: list[str] = Field(default_factory=list)
    source: str = ""

    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None  # "succeeded" | "failed"
    # R1: a structured, Laurelin-authored failure. Replaces `last_error`, which
    # held a driver's sentence and was read by a VIEWER off GET /audit in round
    # 3 of this bug — see laurelin/core/failure.py.
    last_failure: Optional[Failure] = None
    last_build_id: Optional[str] = None
    # Highest upstream version already acted on, for the "upstream" trigger.
    watermark: Optional[int] = None

    created_at: str = Field(default_factory=utcnow_iso)
    created_by: str = ""


class SourceInfo(Governed):
    """A configured external data source that syncs into a dataset.

    **Authored by an ADMIN** — `PUT`/`DELETE /sources` are admin-gated, because a
    connector config embeds credentials and `file` reads the server's
    filesystem — but *read* by an EDITOR, who owns the datasets these fill. That
    is a real privilege crossing, and R2 draws it through the middle of this
    model rather than around the route.

    An editor gets the Laurelin-owned facts: which source, which connector kind,
    which dataset, when it last ran, whether it worked, and a structured failure
    if it did not. They do not get `config` — which is the operator's own dict,
    in the *connector's* vocabulary, and is where a credential lives. (The old
    answer was `redact_mapping`, a denylist over key names somebody else chose;
    round 3 read an ODBC keyword string out of a `path` key that no denylist
    covers. Not disclosing the dict at all is not a better denylist, it is the
    absence of one.)
    """

    laurelin_author_role: ClassVar[Role] = Role.admin

    name: Annotated[str, Audience.PRESENTATION]
    # A closed set of Laurelin connector kinds, not free text.
    type: Annotated[str, Audience.PRESENTATION]  # "postgres" | "http" | "file"
    dataset: Annotated[str, Audience.PRESENTATION]
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    created_by: str = ""
    last_sync_at: Annotated[Optional[str], Audience.PRESENTATION] = None
    last_sync_status: Annotated[Optional[str], Audience.PRESENTATION] = None
    # R1: see ScheduleInfo.last_failure. This column held the driver's own
    # words and was the round-2 disclosure. PRESENTATION because an editor who
    # owns the target dataset has to know why its data is stale — and safe to
    # show because a Failure is Laurelin's own record, projected again on its
    # own terms for anyone below editor.
    last_sync_failure: Annotated[Optional[Failure], Audience.PRESENTATION] = None
    last_sync_version: Annotated[Optional[int], Audience.PRESENTATION] = None
    last_sync_rows: Annotated[Optional[int], Audience.PRESENTATION] = None
    # OPERATIONAL: the high-water mark for incremental (mode="append") syncs is
    # a *value out of the source's own data*, carried into the next pull's
    # WHERE. It is governed data wearing a bookkeeping hat.
    cursor_value: Optional[str] = None


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

class ChartKind(str, Enum):
    table = "table"
    bar = "bar"
    line = "line"
    area = "area"
    stat = "stat"  # single big number (first cell of the result)


class DashboardPanel(Governed):
    """One saved query + presentation.

    A panel is authored by an EDITOR and read by a VIEWER, and R2 splits it
    along exactly that line: the *presentation* half (id, title, chart kind,
    axis bindings, width) is what the viewer's screen is made of; the *query*
    half (sql, or the object aggregation) is a machine instruction the viewer
    never receives.

    The viewer still gets the RESULTS. `POST /dashboards/{name}/panels/{id}/run`
    executes the stored panel **server-side, as the caller**, applying that
    caller's ACL/RLS/masking — so the old invariant holds for a new reason:
    storing a dashboard still grants nobody any new read access, because the
    server, not the browser, is the thing running the query.

    A panel draws from exactly one of two sources:

    ``sql``
        Arbitrary SQL over datasets. Maximum power, and it sees raw rows.

    ``object_type`` + ``metrics``
        An aggregation over ontology objects. This is the one to reach for
        when charting something the ontology models, because SQL over the
        *backing dataset* misses the edit overlay — it answers from rows an
        action has already changed, and the chart gives no hint that it
        disagrees with the object list beside it.
    """

    laurelin_author_role: ClassVar[Role] = Role.editor

    id: Annotated[str, Audience.PRESENTATION]
    # Always non-empty on a stored panel: `upsert_dashboard` fills "Panel {n}"
    # when the author leaves it blank, so the label a viewer reads never has to
    # fall back to a slice of the SQL (which is what Dashboards.tsx used to do).
    title: Annotated[str, Audience.PRESENTATION] = ""
    # -- source A: SQL. OPERATIONAL: author-written, executed, and the field
    # three rounds of this bug leaked out of GET /dashboards.
    sql: str = ""
    # -- source B: an object aggregation. Equally a machine instruction.
    object_type: str = ""
    group_by: list[str] = Field(default_factory=list)
    # `{"op": ..., "property": ..., "alias": ...}`. OPERATIONAL as a whole, and
    # note what that does and does not claim. `op` and `property` are
    # instructions and are withheld — a viewer's 400 quoted both of them back
    # before `stored_instruction_error` existed. `alias` is a **caption**: it
    # becomes the column header of the table the viewer is looking at, and the
    # run route puts it there deliberately. An editor who types a credential
    # into a column header has disclosed it to their own readers on purpose,
    # exactly as they would by typing it into `title` above. See
    # `test_a_panels_column_labels_are_captions_and_reach_the_viewer_by_design`.
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    search: str = ""

    chart: Annotated[ChartKind, Audience.PRESENTATION] = ChartKind.table
    # Column bindings (empty = infer: first text column as x, numeric as y).
    x: Annotated[str, Audience.PRESENTATION] = ""
    y: Annotated[list[str], Audience.PRESENTATION] = Field(default_factory=list)
    width: Annotated[int, Audience.PRESENTATION] = Field(default=6, ge=1, le=12)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "DashboardPanel":
        has_sql, has_object = bool(self.sql.strip()), bool(self.object_type.strip())
        if has_sql and has_object:
            raise ValueError(
                "A panel draws from either sql or object_type, not both — "
                "two sources would make it ambiguous which one the chart shows."
            )
        if not has_sql and not has_object:
            raise ValueError("A panel needs either sql or object_type")
        if has_object and not self.metrics:
            raise ValueError(
                "An object panel needs at least one metric (e.g. "
                '{"op": "count", "alias": "count"})'
            )
        return self

    @property
    def is_object_panel(self) -> bool:
        return bool(self.object_type.strip())


class DashboardInfo(Governed):
    laurelin_author_role: ClassVar[Role] = Role.editor

    name: Annotated[str, Audience.PRESENTATION]
    title: Annotated[str, Audience.PRESENTATION] = ""
    description: Annotated[str, Audience.PRESENTATION] = ""
    # PRESENTATION so the viewer's board is not an empty box — each panel is
    # then projected on its own terms (see DashboardPanel), so what arrives is
    # the layout without the queries.
    panels: Annotated[list[DashboardPanel], Audience.PRESENTATION] = Field(
        default_factory=list
    )
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    created_by: str = ""
    updated_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)


# ---------------------------------------------------------------------------
# Builds & lineage
# ---------------------------------------------------------------------------

class BuildStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class BuildTaskInfo(Governed):
    laurelin_author_role: ClassVar[Role] = Role.editor

    transform_name: Annotated[str, Audience.PRESENTATION]
    output_dataset: Annotated[str, Audience.PRESENTATION]
    status: Annotated[BuildStatus, Audience.PRESENTATION] = BuildStatus.pending
    started_at: Annotated[Optional[str], Audience.PRESENTATION] = None
    finished_at: Annotated[Optional[str], Audience.PRESENTATION] = None
    # R1. This field used to be `error: str` holding f"{type(exc).__name__}:
    # {exc}" (builder.py:270) — a driver sentence on a VIEWER-gated route, with
    # no redactor anywhere on the path. Now a Failure, itself projected down to
    # {code, subject} for a reader below editor.
    failure: Annotated[Optional[Failure], Audience.PRESENTATION] = None
    rows_written: Annotated[Optional[int], Audience.PRESENTATION] = None
    output_version: Annotated[Optional[int], Audience.PRESENTATION] = None
    # One entry per declared expectation: {expectation, passed, severity,
    # measured, message}. Recorded whether the build passed or failed — a
    # check that passed is evidence, and a `warn` that fired needs somewhere
    # to be seen.
    #
    # OPERATIONAL by omission, and correctly so: `message` is prose an EDITOR
    # wrote in a pipeline file, which is exactly the class of text R2 is about.
    expectations: list[dict] = Field(default_factory=list)


class BuildInfo(Governed):
    laurelin_author_role: ClassVar[Role] = Role.editor

    id: Annotated[str, Audience.PRESENTATION]
    targets: Annotated[list[str], Audience.PRESENTATION] = Field(default_factory=list)
    status: Annotated[BuildStatus, Audience.PRESENTATION] = BuildStatus.pending
    started_at: Annotated[Optional[str], Audience.PRESENTATION] = None
    finished_at: Annotated[Optional[str], Audience.PRESENTATION] = None
    failure: Annotated[Optional[Failure], Audience.PRESENTATION] = None
    tasks: Annotated[list[BuildTaskInfo], Audience.PRESENTATION] = Field(
        default_factory=list
    )


class LineageEdge(BaseModel):
    upstream_dataset: str
    downstream_dataset: str
    transform_name: str


# ---------------------------------------------------------------------------
# Ontology definitions (parsed from workspace ontology/*.yml)
# ---------------------------------------------------------------------------

class PropertyDef(Governed):
    # The ontology is admin-authored, but every field here is a caption or a
    # type name written FOR the person browsing objects — that is what an
    # ontology is for. The dangerous class of ontology text (a filter
    # expression) lives on ObjectAppInfo.filters, which stays OPERATIONAL.
    laurelin_author_role: ClassVar[Role] = Role.admin
    type: Annotated[str, Audience.PRESENTATION] = "string"
    display_name: Annotated[Optional[str], Audience.PRESENTATION] = None
    description: Annotated[str, Audience.PRESENTATION] = ""


class ObjectTypeDef(Governed):
    laurelin_author_role: ClassVar[Role] = Role.admin

    api_name: Annotated[str, Audience.PRESENTATION]
    display_name: Annotated[Optional[str], Audience.PRESENTATION] = None
    description: Annotated[str, Audience.PRESENTATION] = ""
    # A dataset *name*, structural in the same way GET /lineage is structural,
    # and the type page cannot explain where its rows come from without it.
    # Access to the dataset is a separate check that this does not weaken.
    backing_dataset: Annotated[str, Audience.PRESENTATION]
    primary_key: Annotated[str, Audience.PRESENTATION]
    title_property: Annotated[Optional[str], Audience.PRESENTATION] = None
    properties: Annotated[dict[str, PropertyDef], Audience.PRESENTATION] = Field(
        default_factory=dict
    )

    def title_for(self, obj: dict[str, Any]) -> str:
        key = self.title_property or self.primary_key
        return str(obj.get(key, ""))


class Cardinality(str, Enum):
    one_to_one = "one_to_one"
    one_to_many = "one_to_many"
    many_to_many = "many_to_many"


class LinkTypeDef(Governed):
    laurelin_author_role: ClassVar[Role] = Role.admin

    api_name: Annotated[str, Audience.PRESENTATION]
    display_name: Annotated[Optional[str], Audience.PRESENTATION] = None
    from_type: Annotated[str, Audience.PRESENTATION] = Field(alias="from")
    to_type: Annotated[str, Audience.PRESENTATION] = Field(alias="to")
    cardinality: Annotated[Cardinality, Audience.PRESENTATION] = Cardinality.one_to_many
    from_property: Annotated[str, Audience.PRESENTATION]  # join key, from-side
    to_property: Annotated[str, Audience.PRESENTATION]  # join key, to-side

    model_config = ConfigDict(populate_by_name=True)


class ActionKind(str, Enum):
    create = "create"
    update = "update"
    delete = "delete"


class ActionParameterDef(Governed):
    laurelin_author_role: ClassVar[Role] = Role.admin
    type: Annotated[str, Audience.PRESENTATION] = "string"
    required: Annotated[bool, Audience.PRESENTATION] = False
    description: Annotated[str, Audience.PRESENTATION] = ""


class ActionDef(Governed):
    laurelin_author_role: ClassVar[Role] = Role.admin

    api_name: Annotated[str, Audience.PRESENTATION]
    display_name: Annotated[Optional[str], Audience.PRESENTATION] = None
    description: Annotated[str, Audience.PRESENTATION] = ""
    object_type: Annotated[str, Audience.PRESENTATION]
    kind: Annotated[ActionKind, Audience.PRESENTATION]
    parameters: Annotated[dict[str, ActionParameterDef], Audience.PRESENTATION] = Field(
        default_factory=dict
    )


class OntologyDef(BaseModel):
    object_types: list[ObjectTypeDef] = Field(default_factory=list)
    link_types: list[LinkTypeDef] = Field(default_factory=list)
    actions: list[ActionDef] = Field(default_factory=list)

    def object_type(self, api_name: str) -> Optional[ObjectTypeDef]:
        return next((o for o in self.object_types if o.api_name == api_name), None)

    def link_type(self, api_name: str) -> Optional[LinkTypeDef]:
        return next((lt for lt in self.link_types if lt.api_name == api_name), None)

    def action(self, api_name: str) -> Optional[ActionDef]:
        return next((a for a in self.actions if a.api_name == api_name), None)


# ---------------------------------------------------------------------------
# Write-back (edit overlay) & audit
# ---------------------------------------------------------------------------

class EditKind(str, Enum):
    create = "create"
    update = "update"
    delete = "delete"


class ObjectEdit(Governed):
    laurelin_author_role: ClassVar[Role] = Role.editor

    id: Annotated[str, Audience.PRESENTATION]
    object_type: Annotated[str, Audience.PRESENTATION]
    pk_value: Annotated[str, Audience.PRESENTATION]
    kind: Annotated[EditKind, Audience.PRESENTATION]
    # OPERATIONAL: the values an action wrote. Governed data, protected by the
    # object type's own grants — it must not arrive as a side effect of reading
    # an edit record.
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: Annotated[str, Audience.PRESENTATION] = "anonymous"
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    # Gapless per-type position in the edit log, allocated at append. 0 means
    # "not yet appended" — the value an in-memory edit carries before commit.
    edit_seq: int = 0


class AuditEvent(Governed):
    """One row of the audit trail.

    `details` is an open bag, and an open bag is what rounds 2 and 3 both leaked
    through: a writer put driver text in it and a VIEWER-gated route served it.
    It is OPERATIONAL here, and gated a second time at the store by
    `audit_log.min_read_role` (default admin, so a new `log_audit` call site
    fails closed).
    """

    laurelin_author_role: ClassVar[Role] = Role.admin

    id: Annotated[Optional[int], Audience.PRESENTATION] = None
    timestamp: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    actor: Annotated[str, Audience.PRESENTATION] = "anonymous"
    action: Annotated[str, Audience.PRESENTATION]
    details: dict[str, Any] = Field(default_factory=dict)
    # The level the row's writer declared for its `details`, straight off the
    # `audit_log.min_read_role` column. `admin` for anything undeclared, so a
    # new `log_audit` call site fails closed, and `admin` for every row that
    # existed before the column did.
    min_read_role: Role = Role.admin

    def laurelin_record_author_role(self) -> Role:
        """A row's author role is the level its writer declared, not the class's.

        This is what makes `min_read_role` mean something. Without it the
        serializer asked the class — always `admin` — and dropped `details`
        from every row below that, so the five call sites that lowered the level
        lowered nothing. Two confirmed defects fell out of the same gap: an
        EDITOR-gated `GET /audit` returned rows with no content at all, while
        the VIEWER-gated `GET /audit/mine` returned the identical bag whole,
        because it re-attached `details` with a `|` override outside the
        serializer.
        """
        return self.min_read_role


# ---------------------------------------------------------------------------
# Authentication & authorization
# ---------------------------------------------------------------------------

# ``Role`` moved to laurelin/core/roles.py so that modules below this one —
# ``failure`` (which declares who may read a failure record) and ``serialize``
# (which compares roles on every response) — can speak about privilege without
# importing this module. Re-exported so every existing
# ``from laurelin.core.models import Role`` keeps working.
# (imported at the top of this module.)


class User(Governed):
    """A Laurelin account. The password hash is intentionally NOT part of this
    model so it can never leak through an API response.

    Every field is PRESENTATION because every field is an identity fact the
    account holder reads about *themselves* on `/auth/status`. It is `Governed`
    at all so that `_user_json` can go through `serialize.dump` like everything
    else: a plain `BaseModel` there meant a field added tomorrow shipped to
    whoever could reach the route, which is exactly the fail-closed property
    `audience.py` claims for new fields.

    ``role`` is the account's role. In multi-workspace mode a user's *effective*
    role is per-workspace (from membership); ``role`` there is a baseline and
    ``superadmin`` marks a server administrator (manages workspaces + users and
    is admin in every workspace). In single-workspace mode ``superadmin`` is
    unused and ``role`` is the account's role directly.
    """

    laurelin_author_role: ClassVar[Role] = Role.admin

    id: Annotated[str, Audience.PRESENTATION]
    username: Annotated[str, Audience.PRESENTATION]
    role: Annotated[Role, Audience.PRESENTATION] = Role.viewer
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)
    disabled: Annotated[bool, Audience.PRESENTATION] = False
    superadmin: Annotated[bool, Audience.PRESENTATION] = False


class WorkspaceInfo(Governed):
    """A workspace registered in the multi-workspace control plane."""

    laurelin_author_role: ClassVar[Role] = Role.admin

    slug: Annotated[str, Audience.PRESENTATION]
    name: Annotated[str, Audience.PRESENTATION]
    description: Annotated[str, Audience.PRESENTATION] = ""
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)


class WorkspaceMembership(BaseModel):
    slug: str
    username: str
    role: Role = Role.viewer


# ---------------------------------------------------------------------------
# Groups & fine-grained ontology permissions
# ---------------------------------------------------------------------------

class GroupInfo(BaseModel):
    name: str
    members: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)


class SubjectKind(str, Enum):
    everyone = "everyone"  # any authenticated user
    role = "role"          # a global role name (viewer/editor/admin)
    group = "group"        # a named group
    user = "user"          # a specific username


class Grant(BaseModel):
    """A single access grant on an object type. ``subject`` is empty for
    ``everyone``, else the role name / group name / username."""

    subject_kind: SubjectKind
    subject: str = ""
    can_view: bool = False
    can_edit: bool = False  # edit implies view

    def normalized_subject(self) -> str:
        return self.subject.strip().lower() if self.subject_kind != SubjectKind.everyone else ""


class ObjectTypeGrants(BaseModel):
    object_type: str
    grants: list[Grant] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Row-level security & column masking (per dataset)
# ---------------------------------------------------------------------------

class PolicySubject(BaseModel):
    """A subject a row-rule or mask-exemption applies to."""
    subject_kind: SubjectKind
    subject: str = ""

    def normalized_subject(self) -> str:
        return self.subject.strip().lower() if self.subject_kind != SubjectKind.everyone else ""


class RowRule(PolicySubject):
    """Matching subjects may see rows whose policy column is in ``values``."""
    values: list[str] = Field(default_factory=list)


class RowPolicy(BaseModel):
    """Row-level security on a dataset: a non-admin user sees a row only if some
    rule matches them AND the row's ``column`` value is in that rule's values.
    A dataset with a row policy but no matching rule for the user => no rows."""
    column: str
    rules: list[RowRule] = Field(default_factory=list)


class MaskMode(str, Enum):
    null = "null"      # replace with NULL (keeps the column's type)
    redact = "redact"  # replace with "***"
    hash = "hash"      # replace with a sha256 prefix (stable pseudonym)


class ColumnMask(BaseModel):
    """Mask ``column`` for everyone except the exempt subjects (admins always
    see unmasked)."""
    column: str
    mode: MaskMode = MaskMode.redact
    exempt: list[PolicySubject] = Field(default_factory=list)


class DatasetPolicy(BaseModel):
    dataset: str
    row_policy: Optional[RowPolicy] = None
    column_masks: list[ColumnMask] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification markings (mandatory access control, propagated via lineage)
# ---------------------------------------------------------------------------

class Marking(Governed):
    laurelin_author_role: ClassVar[Role] = Role.admin
    name: Annotated[str, Audience.PRESENTATION]
    description: Annotated[str, Audience.PRESENTATION] = ""
    created_at: Annotated[str, Audience.PRESENTATION] = Field(default_factory=utcnow_iso)


class DatasetMarkings(BaseModel):
    dataset: str
    explicit: list[str] = Field(default_factory=list)   # admin-assigned
    effective: list[str] = Field(default_factory=list)  # explicit ∪ inherited via lineage


class UserClearances(BaseModel):
    username: str
    markings: list[str] = Field(default_factory=list)


class ObjectTypePermission(BaseModel):
    """The effective permission a specific user has on an object type."""

    can_view: bool
    can_edit: bool
