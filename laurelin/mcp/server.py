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
            "actions, builds). All access is scoped to the API token's "
            "permissions; mutations are audited. Prefer query_sql for "
            "analytics and ontology tools for entity lookups and write-back."
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

    return server
