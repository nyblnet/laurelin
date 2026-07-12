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

    # -- sources ----------------------------------------------------------

    def list_sources(self) -> list[dict]:
        return self._req("GET", "/sources")

    def sync_source(self, name: str) -> dict:
        return self._req("POST", f"/sources/{name}/sync")

    # -- dashboards --------------------------------------------------------

    def list_dashboards(self) -> list[dict]:
        return self._req("GET", "/dashboards")
