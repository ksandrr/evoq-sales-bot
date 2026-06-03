import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    async def list_tasks(self, project_id: str = "", board_id: str = "", search: str = "") -> list[WeeekOption]:
        params = self._base_list_params()
        if project_id:
            params["projectId"] = project_id
        if board_id:
            params["boardId"] = board_id
        if search:
            params["search"] = search
        payload = await self._request("GET", "/tm/tasks", params=params)
        items = self._extract_items(payload)
        filtered = []
        for item in items:
            item_project_id = str(item.get("projectId") or item.get("project", {}).get("id") or "")
            item_board_id = str(item.get("boardId") or item.get("board", {}).get("id") or "")
            if project_id and item_project_id and item_project_id != str(project_id):
                continue
            if board_id and item_board_id and item_board_id != str(board_id):
                continue
            filtered.append(item)
        items = filtered or items
        options = []
        for item in items:
            option = self._coerce_option(item, "Task")
            if option:
                options.append(option)
        return options

    def _task_payload(
        self,
        *,
        title: str,
        description: str,
        board_id: str,
        column_id: str,
        project_id: str = "",
        due_date: str = "",
        due_time: str = "",
        timezone_name: str = "",
        parent_id: str = "",
    ) -> dict:
        location = {}
        if project_id:
            location["projectId"] = int(project_id) if str(project_id).isdigit() else project_id
        if board_id:
            location["boardId"] = int(board_id) if str(board_id).isdigit() else board_id
        if column_id:
            location["boardColumnId"] = int(column_id) if str(column_id).isdigit() else column_id

        payload = {
            "title": title,
            "description": description,
            "locations": [location] if location else [],
            "boardId": board_id,
            "boardColumnId": column_id,
            "type": "action",
        }
        if project_id:
            payload["projectId"] = project_id
        has_due_date = bool(due_date and re.match(r"^\d{4}-\d{2}-\d{2}$", due_date))
        has_due_time = bool(due_time and re.match(r"^\d{2}:\d{2}$", due_time))
        if has_due_date and has_due_time:
            # Weeek rejects mixed due fields; send only one UTC timestamp field.
            payload["dueDateTime"] = self._format_due_datetime(due_date, due_time, timezone_name)
        elif has_due_date:
            payload["dueDate"] = due_date
        elif has_due_time:
            payload["dueTime"] = due_time
        if parent_id:
            payload["parentId"] = int(parent_id) if str(parent_id).isdigit() else parent_id
        if self.workspace_id:
            payload["workspaceId"] = self.workspace_id
        return payload

    def _format_due_datetime(self, due_date: str, due_time: str, timezone_name: str) -> str:
        local_tz = timezone.utc
        if timezone_name:
            try:
                local_tz = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                local_tz = timezone.utc
        local_dt = datetime.strptime(f"{due_date} {due_time}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        timezone_name: str = "",
    ) -> dict:
        payload = self._task_payload(
            title=title,
            description=description,
            board_id=board_id,
            column_id=column_id,
            project_id=project_id,
            due_date=due_date,
            due_time=due_time,
            timezone_name=timezone_name,
        )
        response = await self._request("POST", "/tm/tasks", json_body=payload)
        if isinstance(response, dict):
            return response
        return {"raw": response}

    async def create_subtask(
        self,
        *,
        title: str,
        description: str,
        board_id: str,
        column_id: str,
        parent_task_id: str,
        project_id: str = "",
        due_date: str = "",
        due_time: str = "",
        timezone_name: str = "",
    ) -> dict:
        payload = self._task_payload(
            title=title,
            description=description,
            board_id=board_id,
            column_id=column_id,
            project_id=project_id,
            due_date=due_date,
            due_time=due_time,
            timezone_name=timezone_name,
            parent_id=parent_task_id,
        )
        response = await self._request("POST", "/tm/tasks", json_body=payload)
        if isinstance(response, dict):
            task = response.get("task")
            if isinstance(task, dict) and not task.get("parentId"):
                task_id = task.get("id") or response.get("id") or response.get("taskId")
                if task_id:
                    try:
                        await self._request(
                            "POST",
                            f"/tm/tasks/{task_id}/parent",
                            json_body={"parentId": int(parent_task_id) if str(parent_task_id).isdigit() else parent_task_id},
                        )
                    except WeeekApiError:
                        pass
            return response
        return {"raw": response}
