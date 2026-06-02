import json
from dataclasses import dataclass

import httpx


class WeeekApiError(Exception):
    pass


@dataclass
class WeeekOption:
    id: str
    name: str
    raw: dict


class WeeekClient:
    def __init__(self, api_token: str, base_url: str, workspace_id: str = ""):
        if not api_token:
            raise WeeekApiError("WEEEK_API_TOKEN is not configured")
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.workspace_id = workspace_id.strip()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _extract_items(self, payload) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []

        for key in ("items", "data", "projects", "boards", "columns", "tasks", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = self._extract_items(value)
                if nested:
                    return nested
        return []

    def _coerce_option(self, item: dict, fallback_name: str) -> WeeekOption | None:
        item_id = (
            item.get("id")
            or item.get("_id")
            or item.get("projectId")
            or item.get("boardId")
            or item.get("columnId")
        )
        if item_id is None:
            return None
        name = (
            item.get("name")
            or item.get("title")
            or item.get("label")
            or item.get("description")
            or fallback_name
        )
        return WeeekOption(str(item_id), str(name).strip() or fallback_name, item)

    async def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            snippet = response.text[:300]
            raise WeeekApiError(f"{response.status_code} {snippet}")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise WeeekApiError(f"Invalid JSON from Weeek: {response.text[:200]}") from exc

    def _base_list_params(self) -> dict:
        params = {}
        if self.workspace_id:
            params["workspaceId"] = self.workspace_id
        return params

    async def list_projects(self) -> list[WeeekOption]:
        payload = await self._request("GET", "/tm/projects", params=self._base_list_params())
        items = self._extract_items(payload)
        if self.workspace_id:
            filtered = [
                item for item in items
                if str(item.get("workspaceId") or item.get("workspace", {}).get("id") or "") == self.workspace_id
            ]
            if filtered:
                items = filtered
        options = []
        for item in items:
            option = self._coerce_option(item, "Проект")
            if option:
                options.append(option)
        return options

    async def list_boards(self, project_id: str) -> list[WeeekOption]:
        params = self._base_list_params()
        params["projectId"] = project_id
        payload = await self._request("GET", "/tm/boards", params=params)
        items = self._extract_items(payload)
        filtered = []
        for item in items:
            value = str(item.get("projectId") or item.get("project", {}).get("id") or "")
            if not project_id or value == str(project_id):
                filtered.append(item)
        items = filtered or items
        options = []
        for item in items:
            option = self._coerce_option(item, "Доска")
            if option:
                options.append(option)
        return options

    async def list_columns(self, board_id: str) -> list[WeeekOption]:
        candidates = [
            ("GET", f"/tm/boards/{board_id}", None),
            ("GET", f"/tm/boards/{board_id}/columns", None),
            ("GET", f"/tm/boards/{board_id}/board-columns", None),
            ("GET", "/tm/board-columns", {"boardId": board_id}),
        ]
        last_error = None
        for method, path, params in candidates:
            try:
                payload = await self._request(method, path, params=params)
            except WeeekApiError as exc:
                last_error = exc
                continue

            if isinstance(payload, dict):
                for key in ("columns", "boardColumns", "boardColumnFunnels", "funnels"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        items = value
                        break
                else:
                    items = self._extract_items(payload)
            else:
                items = self._extract_items(payload)

            options = []
            for item in items:
                option = self._coerce_option(item, "Колонка")
                if option:
                    options.append(option)
            if options:
                return options

        if last_error:
            raise last_error
        return []

    async def create_task(
        self,
        *,
        title: str,
        description: str,
        board_id: str,
        column_id: str,
        project_id: str = "",
        due_date: str = "",
        due_time: str = "",
    ) -> dict:
        payload = {
            "title": title,
            "description": description,
            "boardId": board_id,
            "boardColumnId": column_id,
        }
        if project_id:
            payload["projectId"] = project_id
        if due_date:
            payload["dueDate"] = due_date
        if due_time:
            payload["dueTime"] = due_time
        if self.workspace_id:
            payload["workspaceId"] = self.workspace_id
        response = await self._request("POST", "/tm/tasks", json_body=payload)
        if isinstance(response, dict):
            return response
        return {"raw": response}
