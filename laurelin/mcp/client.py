"""Thin typed client for the Laurelin REST API.

Used by the MCP server (and usable standalone as a Python SDK). Every call
goes through the normal HTTP surface with a Bearer API token, so RBAC, ACLs,
row-level security, markings, and audit all apply exactly as they would for
any other API consumer — an agent gets no side door.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class LaurelinError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class LaurelinClient:
    """Synchronous client. ``transport`` is injectable for tests
    (httpx.ASGITransport) and ``workspace`` selects the active workspace on a
    multi-workspace server (X-Laurelin-Workspace header)."""

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        workspace: str = "",
        http: Optional[httpx.Client] = None,
        timeout: float = 60.0,
    ):
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        if workspace:
            self._headers["X-Laurelin-Workspace"] = workspace
        # ``http`` is injectable for tests (fastapi.testclient.TestClient is an
        # httpx.Client) and for custom transports/retries in embedding code.
        self._own = http is None
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        if self._own:
            self._http.close()

    def _req(self, method: str, path: str, **kwargs) -> Any:
        resp = self._http.request(method, "/api/v1" + path, headers=self._headers, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:  # noqa: BLE001
                detail = resp.text
            raise LaurelinError(resp.status_code, str(detail))
        return resp.json()

    # -- datasets ---------------------------------------------------------

    def list_datasets(self) -> list[dict]:
        return self._req("GET", "/datasets")

    def create_dataset(self, name: str, description: str = "") -> dict:
        return self._req(
            "POST", "/datasets", json={"name": name, "description": description}
        )

    def dataset_schema(self, name: str, version: Optional[int] = None) -> list[dict]:
        params = {"version": version} if version is not None else {}
        return self._req("GET", f"/datasets/{name}/schema", params=params)

    def dataset_rows(self, name: str, limit: int = 100, offset: int = 0) -> dict:
        return self._req(
            "GET", f"/datasets/{name}/rows", params={"limit": limit, "offset": offset}
        )

    def query(self, sql: str, max_rows: int = 1000) -> dict:
        return self._req("POST", "/query", json={"sql": sql, "max_rows": max_rows})

    # -- ontology ---------------------------------------------------------

    def list_object_types(self) -> list[dict]:
        return self._req("GET", "/ontology/object-types")

    def get_object_type(self, name: str) -> dict:
        return self._req("GET", f"/ontology/object-types/{name}")

    def search_objects(
        self, type_name: str, search: str = "", limit: int = 25, offset: int = 0
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if search:
            params["search"] = search
        return self._req("GET", f"/ontology/objects/{type_name}", params=params)

    def aggregate_objects(
        self,
        type_name: str,
        group_by: Optional[list[str]] = None,
        metrics: Optional[list[dict]] = None,
        filters: Optional[dict] = None,
        search: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        return self._req(
            "POST",
            f"/ontology/objects/{type_name}/aggregate",
            json={
                "group_by": group_by or [],
                "metrics": metrics or [{"op": "count", "alias": "count"}],
                "filters": filters or {},
                "search": search,
                "limit": limit,
            },
        )

    def get_object(self, type_name: str, pk: str) -> dict:
        return self._req("GET", f"/ontology/objects/{type_name}/{pk}")

    def get_links(self, type_name: str, pk: str, link_name: str) -> dict:
        return self._req(
            "GET", f"/ontology/objects/{type_name}/{pk}/links/{link_name}"
        )

    def list_actions(self) -> list[dict]:
        return self._req("GET", "/ontology/actions")

    def apply_action(
        self, name: str, pk: Optional[str] = None, parameters: Optional[dict] = None
    ) -> dict:
        return self._req(
            "POST",
            f"/ontology/actions/{name}/apply",
            json={"pk": pk, "parameters": parameters or {}},
        )

    # -- pipeline ---------------------------------------------------------

    def list_transforms(self) -> list[dict]:
        return self._req("GET", "/transforms")

    def lineage(self) -> dict:
        return self._req("GET", "/lineage")

    def run_build(self, targets: Optional[list[str]] = None, wait: bool = False) -> dict:
        body: dict[str, Any] = {"wait": wait}
        if targets:
            body["targets"] = targets
        return self._req("POST", "/builds", json=body)

    def get_build(self, build_id: str) -> dict:
        return self._req("GET", f"/builds/{build_id}")

    def dataset_health(self) -> list[dict]:
        # Filtered server-side to datasets this credential can view, exactly
        # like the REST rollup — the client adds nothing and removes nothing.
        return self._req("GET", "/health/datasets")

    # -- flows (no-code authoring) -----------------------------------------

    def flow_dataset_schema(self, dataset: str) -> dict:
        return self._req("GET", "/flows/schema", params={"dataset": dataset})

    def preview_flow(
        self,
        flow: dict,
        node_id: Optional[str] = None,
        max_rows: int = 50,
    ) -> dict:
        return self._req(
            "POST",
            "/flows/preview",
            json={"flow": flow, "node_id": node_id, "max_rows": max_rows},
        )

    def write_flow(self, name: str, flow: dict) -> dict:
        return self._req("PUT", f"/flows/{name}", json={"flow": flow})

    def delete_flow(self, name: str) -> dict:
        return self._req("DELETE", f"/flows/{name}")

    # -- ontology authoring (admin) ---------------------------------------

    def put_object_type(
        self,
        api_name: str,
        backing_dataset: str,
        primary_key: str,
        properties: Optional[dict] = None,
        display_name: Optional[str] = None,
        description: str = "",
        title_property: Optional[str] = None,
    ) -> dict:
        return self._req(
            "PUT",
            f"/ontology/object-types/{api_name}",
            json={
                "backing_dataset": backing_dataset,
                "primary_key": primary_key,
                "properties": properties or {},
                "display_name": display_name,
                "description": description,
                "title_property": title_property,
            },
        )

    def delete_object_type(self, api_name: str) -> dict:
        return self._req("DELETE", f"/ontology/object-types/{api_name}")

    def put_link_type(
        self,
        api_name: str,
        from_type: str,
        to_type: str,
        from_property: str,
        to_property: str,
        cardinality: str = "one_to_many",
        display_name: Optional[str] = None,
    ) -> dict:
        return self._req(
            "PUT",
            f"/ontology/link-types/{api_name}",
            json={
                "from_type": from_type,
                "to_type": to_type,
                "from_property": from_property,
                "to_property": to_property,
                "cardinality": cardinality,
                "display_name": display_name,
            },
        )

    def delete_link_type(self, api_name: str) -> dict:
        return self._req("DELETE", f"/ontology/link-types/{api_name}")

    def put_action_type(
        self,
        api_name: str,
        object_type: str,
        kind: str,
        parameters: Optional[dict] = None,
        display_name: Optional[str] = None,
        description: str = "",
    ) -> dict:
        return self._req(
            "PUT",
            f"/ontology/action-types/{api_name}",
            json={
                "object_type": object_type,
                "kind": kind,
                "parameters": parameters or {},
                "display_name": display_name,
                "description": description,
            },
        )

    def delete_action_type(self, api_name: str) -> dict:
        return self._req("DELETE", f"/ontology/action-types/{api_name}")

    def build_object_index(self, type_name: str) -> dict:
        return self._req("POST", f"/ontology/object-types/{type_name}/index")

    def enable_writeback(self, type_name: str, allow_transform_backed: bool = False) -> dict:
        return self._req(
            "POST",
            f"/ontology/object-types/{type_name}/writeback",
            params={"allow_transform_backed": allow_transform_backed},
        )

    def set_object_type_grants(self, type_name: str, grants: list[dict]) -> dict:
        return self._req(
            "PUT", f"/ontology/permissions/{type_name}", json={"grants": grants}
        )

    # -- governance (admin) ------------------------------------------------

    def create_marking(self, name: str, description: str = "") -> dict:
        return self._req(
            "POST", "/markings", json={"name": name, "description": description}
        )

    def set_dataset_markings(self, dataset: str, markings: list[str]) -> dict:
        return self._req(
            "PUT", f"/datasets/{dataset}/markings", json={"markings": markings}
        )

    def set_user_clearances(self, username: str, markings: list[str]) -> dict:
        return self._req(
            "PUT", f"/users/{username}/clearances", json={"markings": markings}
        )

    def set_dataset_grants(self, dataset: str, grants: list[dict]) -> dict:
        return self._req(
            "PUT", f"/datasets/{dataset}/permissions", json={"grants": grants}
        )

    def set_dataset_policy(
        self,
        dataset: str,
        row_policy: Optional[dict] = None,
        column_masks: Optional[list[dict]] = None,
    ) -> dict:
        return self._req(
            "PUT",
            f"/datasets/{dataset}/policy",
            json={"row_policy": row_policy, "column_masks": column_masks or []},
        )

    def create_user(self, username: str, password: str, role: str = "viewer") -> dict:
        return self._req(
            "POST",
            "/users",
            json={"username": username, "password": password, "role": role},
        )

    def create_group(self, name: str) -> dict:
        return self._req("POST", "/groups", json={"name": name})

    def set_group_members(self, name: str, members: list[str]) -> dict:
        return self._req("PUT", f"/groups/{name}/members", json={"members": members})

    # -- governance change approval (admin) --------------------------------

    def list_proposals(self, state: Optional[str] = None, limit: int = 200) -> list[dict]:
        params: dict = {"limit": limit}
        if state:
            params["state"] = state
        return self._req("GET", "/proposals", params=params)

    def get_proposal(self, proposal_id: str) -> dict:
        return self._req("GET", f"/proposals/{proposal_id}")

    def approve_proposal(self, proposal_id: str) -> dict:
        return self._req("POST", f"/proposals/{proposal_id}/approve")

    def reject_proposal(self, proposal_id: str, reason: str = "") -> dict:
        return self._req(
            "POST", f"/proposals/{proposal_id}/reject", json={"reason": reason}
        )

    # -- governance read-back (admin) --------------------------------------

    def list_dataset_markings(self) -> list[dict]:
        return self._req("GET", "/dataset-markings")

    def list_dataset_grants(self) -> list[dict]:
        return self._req("GET", "/dataset-permissions")

    def list_dataset_policies(self) -> list[dict]:
        return self._req("GET", "/dataset-policies")

    def list_object_type_grants(self) -> list[dict]:
        return self._req("GET", "/ontology/permissions")

    def get_user_clearances(self, username: str) -> dict:
        return self._req("GET", f"/users/{username}/clearances")

    # -- sources ----------------------------------------------------------

    def list_sources(self) -> list[dict]:
        return self._req("GET", "/sources")

    def sync_source(self, name: str) -> dict:
        return self._req("POST", f"/sources/{name}/sync")

    def upsert_source(
        self, name: str, type: str, dataset: str, config: Optional[dict] = None
    ) -> dict:
        return self._req(
            "PUT",
            f"/sources/{name}",
            json={"type": type, "dataset": dataset, "config": config or {}},
        )

    def delete_source(self, name: str) -> dict:
        return self._req("DELETE", f"/sources/{name}")

    # -- dashboards --------------------------------------------------------

    def list_dashboards(self) -> list[dict]:
        return self._req("GET", "/dashboards")

    def get_dashboard(self, name: str) -> dict:
        return self._req("GET", f"/dashboards/{name}")

    def upsert_dashboard(
        self,
        name: str,
        title: str = "",
        description: str = "",
        panels: Optional[list[dict]] = None,
    ) -> dict:
        return self._req(
            "PUT",
            f"/dashboards/{name}",
            json={
                "title": title,
                "description": description,
                "panels": panels or [],
            },
        )

    def run_dashboard_panel(
        self, name: str, panel_id: str, max_rows: int = 1000
    ) -> dict:
        return self._req(
            "POST",
            f"/dashboards/{name}/panels/{panel_id}/run",
            json={"max_rows": max_rows},
        )

    # -- schedules ---------------------------------------------------------

    def upsert_schedule(
        self,
        name: str,
        *,
        enabled: bool = True,
        trigger: str = "cron",
        cron: str = "",
        upstream_dataset: str = "",
        action: str = "build",
        targets: Optional[list[str]] = None,
        source: str = "",
    ) -> dict:
        return self._req(
            "PUT",
            f"/schedules/{name}",
            json={
                "enabled": enabled,
                "trigger": trigger,
                "cron": cron,
                "upstream_dataset": upstream_dataset,
                "action": action,
                "targets": targets or [],
                "source": source,
            },
        )

    def run_schedule(self, name: str) -> dict:
        return self._req("POST", f"/schedules/{name}/run")
