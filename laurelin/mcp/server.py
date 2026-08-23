"""MCP server: agents operate a Laurelin workspace through the normal API.

Runs as a stdio MCP server (``laurelin mcp --url ... --token ...``). Every
tool call is an authenticated REST call, so the agent is subject to the same
RBAC / dataset ACLs / row-level security / classification markings as any
user with that token, and every mutation lands in the audit log. There is no
privileged path.

Requires the optional ``mcp`` extra: ``pip install laurelin[mcp]``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from laurelin.mcp.client import LaurelinClient


def _j(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def build_server(client: LaurelinClient):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The MCP server needs the 'mcp' package: pip install 'laurelin[mcp]'"
        ) from exc

    server = FastMCP(
        "laurelin",
        instructions=(
            "Tools for a Laurelin workspace (datasets, SQL, ontology objects, "
            "actions, builds, sources, no-code flow authoring). All access is "
            "scoped to the API token's permissions; mutations are audited. "
            "Prefer query_sql for analytics and ontology tools for entity "
            "lookups and write-back. To author a pipeline: create_dataset / "
            "create_source for inputs, flow_dataset_schema to discover "
            "columns, preview_flow to iterate, write_flow to save, run_build "
            "to materialize."
        ),
    )

    # -- data -----------------------------------------------------------------

    @server.tool()
    def list_datasets() -> str:
        """List datasets (name, description, latest version) visible to this token."""
        return _j(client.list_datasets())

    @server.tool()
    def dataset_schema(name: str) -> str:
        """Column names and types of a dataset's latest version."""
        return _j(client.dataset_schema(name))

    @server.tool()
    def dataset_rows(name: str, limit: int = 50, offset: int = 0) -> str:
        """Page through raw rows of a dataset (row-level security applies)."""
        return _j(client.dataset_rows(name, limit=limit, offset=offset))

    @server.tool()
    def query_sql(sql: str, max_rows: int = 200) -> str:
        """Run read-only SQL (DuckDB dialect) over the datasets this token can
        view; each dataset is a view named after the dataset. Filesystem and
        network access are disabled."""
        return _j(client.query(sql, max_rows=max_rows))

    # -- ontology --------------------------------------------------------------

    @server.tool()
    def list_object_types() -> str:
        """List ontology object types (the workspace's semantic entities)."""
        return _j(client.list_object_types())

    @server.tool()
    def get_object_type(name: str) -> str:
        """An object type's properties, links, and available actions."""
        return _j(client.get_object_type(name))

    @server.tool()
    def search_objects(type_name: str, search: str = "", limit: int = 25, offset: int = 0) -> str:
        """Search/browse objects of a type; `search` matches across properties."""
        return _j(client.search_objects(type_name, search=search, limit=limit, offset=offset))

    @server.tool()
    def aggregate_objects(
        type_name: str,
        group_by: Optional[list[str]] = None,
        metrics: Optional[list[dict]] = None,
        filters: Optional[dict] = None,
        search: str = "",
        limit: int = 100,
    ) -> str:
        """Count or summarize objects, grouped by their properties.

        Prefer this over query_sql for questions like "how many orders per
        region" or "total amount by status": SQL reads the *backing dataset*,
        which does not include edits made by ontology actions, so it can
        confidently disagree with what the object list shows.

        metrics: [{"op": "count"|"count_distinct"|"sum"|"avg"|"min"|"max"|"median",
                   "property": "<name>", "alias": "<output name>"}]
        `property` is omitted only for plain "count". Defaults to counting.
        """
        return _j(client.aggregate_objects(
            type_name, group_by=group_by, metrics=metrics,
            filters=filters, search=search or None, limit=limit,
        ))

    @server.tool()
    def get_object(type_name: str, pk: str) -> str:
        """One object by primary key."""
        return _j(client.get_object(type_name, pk))

    @server.tool()
    def get_linked_objects(type_name: str, pk: str, link_name: str) -> str:
        """Objects linked to this object through a named link type."""
        return _j(client.get_links(type_name, pk, link_name))

    @server.tool()
    def list_actions() -> str:
        """Validated write-back actions defined on the ontology."""
        return _j(client.list_actions())

    @server.tool()
    def apply_action(name: str, pk: Optional[str] = None, parameters: Optional[dict] = None) -> str:
        """Apply an ontology action (validated write-back). The edit is
        attributed to this token's user and recorded in the audit log."""
        return _j(client.apply_action(name, pk=pk, parameters=parameters))

    # -- ontology authoring (admin) --------------------------------------------

    @server.tool()
    def put_object_type(
        api_name: str,
        backing_dataset: str,
        primary_key: str,
        properties: Optional[dict] = None,
        display_name: Optional[str] = None,
        description: str = "",
        title_property: Optional[str] = None,
    ) -> str:
        """Create or update an ontology object type. Requires ADMIN. The
        backing dataset must already exist (404 otherwise — create datasets
        before object types). properties maps property name -> {"type": ...,
        "display_name": ..., "description": ...}; names missing from the
        backing dataset's current schema come back as warnings, not errors
        (columns may arrive with a later build). The definition is live on the
        next request. A 409 means the api_name is owned by a hand-written
        ontology file, which this API refuses to touch."""
        return _j(client.put_object_type(
            api_name, backing_dataset, primary_key, properties=properties,
            display_name=display_name, description=description,
            title_property=title_property,
        ))

    @server.tool()
    def delete_object_type(api_name: str) -> str:
        """Delete an API-managed object type definition. Requires ADMIN.
        Refuses (409) definitions living in hand-written ontology files. Any
        per-type grants are left in the store (inert once the type is gone) so
        the audit trail stays honest."""
        return _j(client.delete_object_type(api_name))

    @server.tool()
    def put_link_type(
        api_name: str,
        from_type: str,
        to_type: str,
        from_property: str,
        to_property: str,
        cardinality: str = "one_to_many",
        display_name: Optional[str] = None,
    ) -> str:
        """Create or update a link between two object types, joined where
        from_type.from_property == to_type.to_property. Requires ADMIN. Both
        object types must already be defined (404 otherwise — author object
        types before links). cardinality: one_to_one, one_to_many or
        many_to_many."""
        return _j(client.put_link_type(
            api_name, from_type, to_type, from_property, to_property,
            cardinality=cardinality, display_name=display_name,
        ))

    @server.tool()
    def delete_link_type(api_name: str) -> str:
        """Delete an API-managed link type definition. Requires ADMIN; refuses
        (409) hand-written ontology files."""
        return _j(client.delete_link_type(api_name))

    @server.tool()
    def put_action_type(
        api_name: str,
        object_type: str,
        kind: str,
        parameters: Optional[dict] = None,
        display_name: Optional[str] = None,
        description: str = "",
    ) -> str:
        """Create or update a validated write-back action on an object type.
        Requires ADMIN. kind is create, update or delete; the object type must
        already be defined (404 otherwise). parameters maps parameter name ->
        {"type": ..., "required": ..., "description": ...}. Once defined, any
        principal with edit access on the object type can apply_action."""
        return _j(client.put_action_type(
            api_name, object_type, kind, parameters=parameters,
            display_name=display_name, description=description,
        ))

    @server.tool()
    def delete_action_type(api_name: str) -> str:
        """Delete an API-managed action definition. Requires ADMIN; refuses
        (409) hand-written ontology files."""
        return _j(client.delete_action_type(api_name))

    @server.tool()
    def build_object_index(type_name: str) -> str:
        """Materialize one object type into the object index (faster queries;
        refreshed automatically after each build). Requires editor permissions
        plus edit access on the object type. Opt in per type: worth it for
        entities, wasteful for high-volume events."""
        return _j(client.build_object_index(type_name))

    @server.tool()
    def enable_writeback(type_name: str, allow_transform_backed: bool = False) -> str:
        """Fold an object type's accumulated action edits into a NEW version of
        its backing dataset. Requires editor permissions, edit access on the
        object type AND edit access on the backing dataset (rewriting a dataset
        is a dataset privilege). The backing dataset must have at least one
        version. For a transform-produced dataset the next build will overwrite
        the folded rows, so that case is refused unless allow_transform_backed
        is true."""
        return _j(client.enable_writeback(
            type_name, allow_transform_backed=allow_transform_backed
        ))

    @server.tool()
    def set_object_type_grants(type_name: str, grants: list[dict]) -> str:
        """Replace the FULL access-grant list of an object type. Requires
        ADMIN. Each grant: {"subject_kind": "everyone"|"role"|"group"|"user",
        "subject": "<name or empty for everyone>", "can_view": bool,
        "can_edit": bool} (edit implies view). An empty list restores the
        default: open per global role RBAC. With any grants present, access is
        only what the grants say — fail closed."""
        return _j(client.set_object_type_grants(type_name, grants))

    # -- governance (admin) ------------------------------------------------------

    @server.tool()
    def create_marking(name: str, description: str = "") -> str:
        """Create a classification marking (lowercase a-z 0-9 _ . -, max 48).
        Requires ADMIN. 409 if it already exists. Markings applied to a dataset
        propagate downstream through lineage; users need a matching clearance
        to read marked data."""
        return _j(client.create_marking(name, description=description))

    @server.tool()
    def set_dataset_markings(dataset: str, markings: list[str]) -> str:
        """Replace a dataset's explicit classification markings. Requires
        ADMIN. Every marking must already exist (create_marking first) and the
        dataset must exist (404 otherwise). Effective markings are recomputed
        across the whole lineage graph immediately — downstream datasets
        inherit these markings, and that propagation is fail-closed. The
        response reports only THIS dataset's explicit and effective markings;
        to see what propagated downstream, call list_dataset_markings."""
        return _j(client.set_dataset_markings(dataset, markings))

    @server.tool()
    def set_user_clearances(username: str, markings: list[str]) -> str:
        """Replace the set of markings a user is cleared to read. Requires
        ADMIN. A user without a dataset's effective markings in their
        clearances cannot read it regardless of role or grants."""
        return _j(client.set_user_clearances(username, markings))

    @server.tool()
    def set_dataset_grants(dataset: str, grants: list[dict]) -> str:
        """Replace the FULL access-grant list of a dataset. Requires ADMIN.
        Grant shape is the same as set_object_type_grants. An empty list
        restores default-open per global RBAC; any non-empty list is exhaustive
        and fail-closed, so grant deliberately (start narrow — fewer grants is
        the safe direction for a migration)."""
        return _j(client.set_dataset_grants(dataset, grants))

    @server.tool()
    def set_dataset_policy(
        dataset: str,
        row_policy: Optional[dict] = None,
        column_masks: Optional[list[dict]] = None,
    ) -> str:
        """Set (or clear, by passing neither) a dataset's row-level security
        and column masks. Requires ADMIN; the dataset must exist. row_policy:
        {"column": "<col>", "rules": [{"subject_kind": ..., "subject": ...,
        "values": [...]}]} — a non-admin sees a row only if a rule matching
        them allows the row's value; a policied dataset with no matching rule
        shows them NOTHING. column_masks: [{"column": ..., "mode":
        "null"|"redact"|"hash", "exempt": [...]}] — hash gives a stable
        pseudonym, so equality joins still work. Admins always read unmasked.
        WARNING: a row policy on a dataset any FLOW reads breaks that flow —
        builds, previews and edits are refused fail-closed (the flow's output
        would launder the policied rows). The response's 'warnings' names the
        affected transforms; row-policy only datasets no flow reads, or the
        flow's OUTPUT instead."""
        return _j(client.set_dataset_policy(
            dataset, row_policy=row_policy, column_masks=column_masks
        ))

    @server.tool()
    def create_user(username: str, password: str, role: str = "viewer") -> str:
        """Create a user account (role: viewer, editor or admin). On a
        single-workspace server this requires ADMIN; on a multi-workspace
        server it requires a server superadmin — if refused there, report it
        rather than retrying. Create users before granting them clearances or
        group membership."""
        return _j(client.create_user(username, password, role=role))

    @server.tool()
    def create_group(name: str) -> str:
        """Create a named user group (usable as a grant subject). Requires
        ADMIN. 409 if it already exists."""
        return _j(client.create_group(name))

    @server.tool()
    def set_group_members(name: str, members: list[str]) -> str:
        """Replace a group's member list. Requires ADMIN. Every member must
        already be an existing user (400 otherwise — create_user first), and
        the group must exist (404)."""
        return _j(client.set_group_members(name, members))

    # -- governance read-back (admin) -------------------------------------------

    @server.tool()
    def list_dataset_markings() -> str:
        """Explicit and effective classification markings for every dataset.
        Requires ADMIN. 'effective' includes markings inherited through
        lineage, so this is how to verify what a set_dataset_markings call
        propagated downstream."""
        return _j(client.list_dataset_markings())

    @server.tool()
    def list_dataset_grants() -> str:
        """The stored access-grant list of every dataset (empty list =
        default open per global role RBAC). Requires ADMIN. This is the
        read-back for set_dataset_grants — verify governance by reading it,
        not by re-issuing writes."""
        return _j(client.list_dataset_grants())

    @server.tool()
    def list_dataset_policies() -> str:
        """The row policy and column masks of every dataset (null = no
        policy). Requires ADMIN. This is the read-back for
        set_dataset_policy."""
        return _j(client.list_dataset_policies())

    @server.tool()
    def list_object_type_grants() -> str:
        """The stored access-grant list of every ontology object type (empty
        list = default open per global role RBAC). Requires ADMIN. This is the
        read-back for set_object_type_grants."""
        return _j(client.list_object_type_grants())

    @server.tool()
    def get_user_clearances(username: str) -> str:
        """The markings one user is cleared to read. Requires ADMIN. This is
        the read-back for set_user_clearances."""
        return _j(client.get_user_clearances(username))

    # -- pipeline ---------------------------------------------------------------

    @server.tool()
    def list_transforms() -> str:
        """Registered pipeline transforms (inputs -> output datasets)."""
        return _j(client.list_transforms())

    @server.tool()
    def get_lineage() -> str:
        """The dataset/transform lineage graph."""
        return _j(client.lineage())

    @server.tool()
    def run_build(targets: Optional[list[str]] = None, wait: bool = False) -> str:
        """Run the pipeline (optionally only for specific output datasets).
        Default is async: poll with get_build. Requires editor permissions."""
        return _j(client.run_build(targets=targets, wait=wait))

    @server.tool()
    def get_build(build_id: str) -> str:
        """Status and per-transform tasks of a build."""
        return _j(client.get_build(build_id))

    # -- pipeline authoring (flows) --------------------------------------------

    @server.tool()
    def create_dataset(name: str, description: str = "") -> str:
        """Register a new, empty dataset (name, description — no rows yet).
        Requires editor permissions. Rows arrive through a source sync, a file
        upload, or a build; bulk row data is deliberately not an MCP tool."""
        return _j(client.create_dataset(name, description=description))

    @server.tool()
    def flow_dataset_schema(dataset: str) -> str:
        """Column names and kinds (number/text/timestamp/...) of one dataset,
        for authoring flows over it. Requires editor permissions plus view
        access to that dataset. Unlike dataset_schema, this also resolves
        remotely-backed (federated/ClickHouse/StarRocks/Iceberg) datasets, so
        use it when planning a flow's source columns."""
        return _j(client.flow_dataset_schema(dataset))

    @server.tool()
    def preview_flow(flow: dict, node_id: Optional[str] = None, max_rows: int = 50) -> str:
        """Run a draft flow (or the prefix ending at node_id) and return sample
        rows without saving anything. Requires editor permissions. The preview
        runs AS THE CALLER: this token's row-level security and column masks
        apply, while the eventual build runs unpolicied — so preview row counts
        may legitimately be lower than build row counts. Masked columns render
        as '***' (the reply's masked_columns lists them per source dataset)."""
        return _j(client.preview_flow(flow, node_id=node_id, max_rows=max_rows))

    @server.tool()
    def write_flow(name: str, flow: dict) -> str:
        """Create or update a no-code flow — the preferred (and only) way an
        agent authors a pipeline here; Python transform authoring is
        deliberately not exposed over MCP. Requires editor permissions, and is
        refused with 403 on a server started with --lock-flows /
        LAURELIN_LOCK_FLOWS=1 — that lock is deployment policy, so stop and
        report rather than retry. The flow is fully validated (structure, every
        column against the live schema, governance) BEFORE it is saved; a 409
        means another transform already produces the output dataset (one
        producer per dataset). The recorded author is this token's user — any
        'author' field in the body is ignored — and every future build
        re-checks that author's read access to the input datasets."""
        return _j(client.write_flow(name, flow))

    @server.tool()
    def delete_flow(name: str) -> str:
        """Delete a flow definition. Requires editor permissions; refused when
        flows are locked (--lock-flows). The dataset the flow produced is NOT
        deleted, and its lineage plus any classification markings it propagated
        are retained (fail-closed, so nothing downstream is declassified) — the
        response says so explicitly."""
        return _j(client.delete_flow(name))

    # -- sources ---------------------------------------------------------------

    @server.tool()
    def list_sources() -> str:
        """Configured external data sources (connector configs are redacted)."""
        return _j(client.list_sources())

    @server.tool()
    def sync_source(name: str) -> str:
        """Pull a source now, writing a new version of its target dataset.
        Requires edit access to that dataset."""
        return _j(client.sync_source(name))

    @server.tool()
    def create_source(
        name: str, type: str, dataset: str, config: Optional[dict] = None
    ) -> str:
        """Create or update an external data source (connector) targeting a
        dataset. Requires ADMIN. type is 'postgres' (config: url + table|query),
        'http' (config: url, optional format/headers) or 'file' (config: path
        on the SERVER's filesystem, optional format). The config transits this
        conversation, so secret-bearing sources (DSNs with passwords) may
        instead be pre-created by a human admin — after that, sync_source is
        all an agent needs. Config is redacted or withheld on every read."""
        return _j(client.upsert_source(name, type, dataset, config=config))

    @server.tool()
    def delete_source(name: str) -> str:
        """Delete a source definition. Requires ADMIN. The target dataset and
        the data already synced into it are not touched."""
        return _j(client.delete_source(name))

    # -- presentation (dashboards + schedules) ---------------------------------

    @server.tool()
    def list_dashboards() -> str:
        """List dashboards. Panels are audience-projected: with a viewer token
        the query half of each panel (sql / object aggregation / flow IR) is
        withheld — only presentation fields (id, title, chart, bindings)
        arrive. Use run_dashboard_panel for a panel's results."""
        return _j(client.list_dashboards())

    @server.tool()
    def get_dashboard(name: str) -> str:
        """One dashboard with its panels. Same audience projection as
        list_dashboards: a viewer token never receives panel query text. An
        editor or admin token receives the full document — use this to verify
        a dashboard you just wrote."""
        return _j(client.get_dashboard(name))

    @server.tool()
    def upsert_dashboard(
        name: str, title: str = "", description: str = "",
        panels: Optional[list[dict]] = None,
    ) -> str:
        """Create or replace a dashboard as a whole document. Requires editor
        permissions. At most 50 panels; each panel has an 'id', presentation
        fields (title, chart: table|bar|line|area|stat|pie|scatter, x, y, series,
        stacked, width 1-12) and EXACTLY ONE query source: 'sql' (a SELECT over
        datasets), 'object_type' + 'metrics' (+ optional group_by/filters/
        search — an ontology aggregation, which sees writeback edits that SQL
        over the backing dataset does not), or 'flow' (a Flow IR document, what
        Explore saves; compiled per run). Flow panels are validated against the
        live schema at save time. Surprise to know: for a panel id that already
        exists, query fields OMITTED from the payload are inherited from the
        stored panel (so round-tripping a viewer projection cannot blank a
        query); send a field explicitly to change or clear it."""
        return _j(client.upsert_dashboard(
            name, title=title, description=description, panels=panels,
        ))

    @server.tool()
    def run_dashboard_panel(name: str, panel_id: str, max_rows: int = 1000) -> str:
        """Execute one stored panel server-side AS THIS TOKEN'S USER and return
        {columns, rows, row_count, truncated}. Viewer-callable: this is how a
        viewer (or you, verifying a migration) gets a panel's RESULTS without
        its query text. The caller's dataset ACLs, row-level security and
        column masks apply on every run, so two principals can get different
        rows from the same panel; storing a dashboard grants nobody new read
        access."""
        return _j(client.run_dashboard_panel(name, panel_id, max_rows=max_rows))

    # -- schedules -------------------------------------------------------------

    @server.tool()
    def upsert_schedule(
        name: str, trigger: str = "cron", cron: str = "",
        upstream_dataset: str = "", action: str = "build",
        targets: Optional[list[str]] = None, source: str = "",
        enabled: bool = True,
    ) -> str:
        """Create or update a schedule. Requires editor permissions (a schedule
        runs the pipeline, so authoring one is a pipeline write). trigger is
        'cron' (cron: standard 5-field expression, validated at save — a bad
        expression fails here, not silently never) or 'upstream'
        (upstream_dataset: fire when that dataset gains a version). action is
        'build' (optional targets: output datasets) or 'sync' (source: a
        registered source name). The response includes non-blocking 'warnings'
        — a credential-looking string in a free-text field, or a referent that
        does not exist yet (unknown source, a build target no transform
        produces, an upstream dataset that does not exist). Read them: a
        warned schedule saves, but will fail (or never fire) when its window
        arrives."""
        return _j(client.upsert_schedule(
            name, enabled=enabled, trigger=trigger, cron=cron,
            upstream_dataset=upstream_dataset, action=action,
            targets=targets, source=source,
        ))

    @server.tool()
    def run_schedule(name: str) -> str:
        """Fire a schedule now, without waiting for its window. Requires editor
        permissions. 409 if the schedule is disabled. The run is queued: the
        scheduler picks it up on its next poll (same path as a natural firing),
        so poll get_build / audit rather than expecting rows in the reply."""
        return _j(client.run_schedule(name))

    return server
