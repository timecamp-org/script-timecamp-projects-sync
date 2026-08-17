import re
from time import perf_counter
from typing import Any, Dict, List, Optional

import requests


TIMECAMP_API_BASE_URL = "https://app.timecamp.com/third_party/api"
TIMECAMP_INTERNAL_API_BASE_URL = "https://app.timecamp.com/internal/api"
TIMECAMP_TASK_NAME_MAX_LENGTH = 190


def normalize_timecamp_task_name(name: Any) -> str:
    """Return the task name representation stored by TimeCamp."""
    if name is None:
        return ""

    return (
        str(name)
        .replace("\t", " ")
        .replace("|", "")
        .replace("→", "")
        .strip()[:TIMECAMP_TASK_NAME_MAX_LENGTH]
    )


class TimeCampRateLimitError(Exception):
    def __init__(self, method: str, url: str, retry_after: Optional[str] = None):
        self.method = method
        self.url = url
        self.retry_after = retry_after
        message = f"TimeCamp rate limit exceeded for {method} {url}"
        if retry_after:
            message += f" (retry after: {retry_after})"
        super().__init__(message)


class TimeCampClient:
    def __init__(
        self,
        api_token: str,
        base_url: str = TIMECAMP_API_BASE_URL,
        internal_base_url: str = TIMECAMP_INTERNAL_API_BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.internal_base_url = internal_base_url.rstrip("/")
        self._api_request_counts: Dict[str, int] = {}
        self._api_request_seconds: Dict[str, float] = {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        metric_key = _api_metric_key(method, endpoint)
        started_at = perf_counter()
        try:
            response = self.session.request(method, url, json=json, params=params)
            if response.status_code == 429:
                raise TimeCampRateLimitError(
                    method=method,
                    url=url,
                    retry_after=response.headers.get("Retry-After"),
                )

            response.raise_for_status()

            if not response.content:
                return {}

            return response.json()
        finally:
            elapsed_seconds = perf_counter() - started_at
            self._record_api_metric(metric_key, elapsed_seconds)

    def _request_internal(
        self,
        method: str,
        endpoint: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.internal_base_url}/{endpoint.lstrip('/')}"
        metric_key = _api_metric_key(method, f"internal/{endpoint.lstrip('/')}")
        started_at = perf_counter()
        try:
            response = self.session.request(method, url, json=json, params=params)
            if response.status_code == 429:
                raise TimeCampRateLimitError(
                    method=method,
                    url=url,
                    retry_after=response.headers.get("Retry-After"),
                )

            response.raise_for_status()

            if not response.content:
                return {}

            return response.json()
        finally:
            elapsed_seconds = perf_counter() - started_at
            self._record_api_metric(metric_key, elapsed_seconds)

    def _record_api_metric(self, metric_key: str, elapsed_seconds: float) -> None:
        self._api_request_counts[metric_key] = (
            self._api_request_counts.get(metric_key, 0) + 1
        )
        self._api_request_seconds[metric_key] = (
            self._api_request_seconds.get(metric_key, 0.0) + elapsed_seconds
        )

    def get_api_metrics_snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {
            "counts": dict(self._api_request_counts),
            "seconds": dict(self._api_request_seconds),
        }

    def get_tasks(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "tasks")

        if isinstance(data, dict):
            return list(data.values())
        if isinstance(data, list):
            return data

        raise ValueError(f"Unexpected TimeCamp tasks response: {type(data)}")

    def get_internal_projects(
        self,
        parent_id: Any,
        status: str = "active",
        include: Optional[List[str]] = None,
        page: int = 1,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "parentId": int(parent_id),
            "status": status,
            "page": int(page),
        }
        if include:
            body["include"] = include

        data = self._request_internal("POST", "v3/projects", json=body)
        if isinstance(data, dict):
            return data

        raise ValueError(f"Unexpected TimeCamp internal projects response: {type(data)}")

    def create_task(
        self,
        name: str,
        parent_id: int,
        external_task_id: str,
    ) -> Dict[str, Any]:
        task_name = normalize_timecamp_task_name(name)
        if not task_name:
            raise ValueError("Task name cannot be empty")

        data = {
            "name": task_name,
            "parent_id": int(parent_id),
            "external_task_id": external_task_id,
        }
        response_data = self._request("POST", "tasks", json=data)

        if isinstance(response_data, dict) and len(response_data) == 1:
            task_data = next(iter(response_data.values()))
            if "task_id" in task_data:
                task_data["external_task_id"] = external_task_id
                return task_data

        raise ValueError(f"Unexpected response format from TimeCamp API: {response_data}")

    def archive_task(self, task_id: Any) -> Any:
        return self._request(
            "PUT",
            "tasks",
            json={
                "archived": 1,
                "task_id": task_id,
            },
        )

    def update_task_name(self, task_id: Any, name: str) -> Any:
        task_name = normalize_timecamp_task_name(name)
        if not task_name:
            raise ValueError("Task name cannot be empty")

        return self._request(
            "PUT",
            "tasks",
            json={
                "task_id": task_id,
                "name": task_name,
            },
        )

    def update_task_estimate(
        self,
        task_id: Any,
        original_estimate_seconds: int,
    ) -> Any:
        estimate_seconds = int(original_estimate_seconds)
        if estimate_seconds < 0:
            raise ValueError("Task estimate cannot be negative")

        return self._request(
            "PATCH",
            f"v3/task/{task_id}/billing-settings",
            json={
                "budget": estimate_seconds,
                "budgetUnit": "hours",
            },
        )

    def get_tag_lists(self, include_tags: bool = True) -> Dict[str, Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if include_tags:
            params["tags"] = 1

        data = self._request("GET", "tag_list", params=params)
        if isinstance(data, dict):
            return data

        raise ValueError(f"Unexpected TimeCamp tag lists response: {type(data)}")

    def get_tag_list_tags(self, tag_list_id: int) -> Dict[str, Dict[str, Any]]:
        data = self._request("GET", f"tag_list/{tag_list_id}/tags")
        if isinstance(data, dict):
            return data

        raise ValueError(f"Unexpected TimeCamp tag list tags response: {type(data)}")

    def create_tag_list(self, name: str) -> int:
        return int(self._request("POST", "tag_list", json={"name": name}))

    def update_tag_list(self, tag_list_id: int, **params: Any) -> Any:
        return self._request("PUT", f"tag_list/{tag_list_id}", json=params)

    def create_tag(self, tag_list_id: int, name: str) -> int:
        return int(
            self._request(
                "POST",
                "tag",
                json={
                    "list": tag_list_id,
                    "name": name,
                },
            )
        )

    def update_tag(self, tag_id: int, **params: Any) -> Any:
        return self._request("PUT", f"tag/{tag_id}", json=params)

    def get_task_tags(self, task_id: Any) -> Dict[str, Dict[str, Any]]:
        data = self._request("GET", f"task/{task_id}/tags")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return _task_tags_list_to_dict(data)

        raise ValueError(f"Unexpected TimeCamp task tags response: {type(data)}")

    def add_tags_to_task(
        self,
        task_id: Any,
        tags: List[Dict[str, Any]],
    ) -> Any:
        return self._request(
            "POST",
            f"task/{task_id}/tag",
            json={"tags": tags},
        )

    def update_task_tags(
        self,
        task_id: Any,
        tags: List[Dict[str, Any]],
    ) -> Any:
        return self._request(
            "PUT",
            f"task/{task_id}/tag",
            json={"tags": tags},
        )

    def remove_tag_list_from_task(
        self,
        task_id: Any,
        tag_list_id: int,
    ) -> Any:
        return self._request(
            "DELETE",
            f"v3/task/{task_id}/tag-list/{tag_list_id}",
        )

    def get_users(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "users")

        if isinstance(data, dict):
            return list(data.values())
        if isinstance(data, list):
            return data

        raise ValueError(f"Unexpected TimeCamp users response: {type(data)}")

    def assign_users_to_task(
        self,
        task_id: Any,
        user_ids: List[int],
        role_id: int,
    ) -> Any:
        return self._request(
            "PUT",
            f"v3/projects/{task_id}/assign",
            json={
                "userIds": user_ids,
                "roleId": role_id,
            },
        )

    def get_project_assigned_users(self, task_id: Any) -> List[Dict[str, Any]]:
        data = self._request("GET", f"v3/projects/{task_id}/assigned-users")

        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data, list):
            return data

        raise ValueError(
            f"Unexpected TimeCamp assigned users response: {type(data)}"
        )

    def unassign_users_from_task(
        self,
        task_id: Any,
        user_ids: List[int],
    ) -> Any:
        return self._request(
            "PUT",
            f"v3/projects/{task_id}/unassign",
            json={"userIds": [int(user_id) for user_id in user_ids]},
        )

    def get_time_entries(
        self,
        start_date: Any = None,
        end_date: Any = None,
        user_ids: Optional[List[int]] = None,
        opt_fields: Optional[str] = None,
        modify_from: Any = None,
        modify_to: Any = None,
    ) -> List[Dict[str, Any]]:
        has_entry_dates = start_date is not None or end_date is not None
        has_modify_dates = modify_from is not None or modify_to is not None
        if has_entry_dates and (start_date is None or end_date is None):
            raise ValueError("Both start_date and end_date must be provided")
        if has_modify_dates and (modify_from is None or modify_to is None):
            raise ValueError("Both modify_from and modify_to must be provided")
        if not has_entry_dates and not has_modify_dates:
            raise ValueError(
                "Provide an entry date range or a modification date range"
            )

        params: Dict[str, Any] = {}
        if has_entry_dates:
            params["from"] = str(start_date)
            params["to"] = str(end_date)
        if has_modify_dates:
            params["modify_from"] = str(modify_from)
            params["modify_to"] = str(modify_to)
        if user_ids:
            params["user_ids"] = ",".join(str(user_id) for user_id in user_ids)
        if opt_fields:
            params["opt_fields"] = opt_fields

        data = self._request("GET", "entries", params=params)
        if isinstance(data, list):
            return data

        raise ValueError(f"Unexpected TimeCamp entries response: {type(data)}")

    def get_time_entry_deletions(
        self,
        modify_from: Any,
        modify_to: Any,
        user_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "from": str(modify_from),
            "to": str(modify_to),
        }
        if user_ids:
            params["user_ids"] = ",".join(str(user_id) for user_id in user_ids)

        data = self._request("GET", "entries_deletions", params=params)
        if isinstance(data, list):
            return data

        raise ValueError(
            f"Unexpected TimeCamp entry deletions response: {type(data)}"
        )

    def get_user_details(
        self,
        user_ids: List[int],
        batch_size: int = 100,
    ) -> List[Dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(int(user_id) for user_id in user_ids))
        if not normalized_ids:
            return []
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        users: List[Dict[str, Any]] = []
        for offset in range(0, len(normalized_ids), batch_size):
            batch = normalized_ids[offset:offset + batch_size]
            data = self._request(
                "GET",
                "user",
                params={"user_id": ",".join(str(user_id) for user_id in batch)},
            )
            if isinstance(data, dict):
                users.append(data)
            elif isinstance(data, list):
                users.extend(data)
            else:
                raise ValueError(
                    f"Unexpected TimeCamp user details response: {type(data)}"
                )

        return users

    def get_entry_tags(self, entry_id: Any) -> Dict[str, List[Dict[str, Any]]]:
        data = self._request("GET", f"entries/{entry_id}/tags")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {str(entry_id): data}

        raise ValueError(f"Unexpected TimeCamp entry tags response: {type(data)}")

    def add_tags_to_entry(
        self,
        entry_id: Any,
        tag_ids: List[int],
    ) -> Any:
        return self._request(
            "PUT",
            f"entries/{entry_id}/tags",
            json={"tags": ",".join(str(tag_id) for tag_id in tag_ids)},
        )

    def update_time_entry_task(self, entry_id: Any, task_id: Any) -> Any:
        return self._request(
            "PUT",
            f"v3/time-entries/{entry_id}",
            json={"taskId": int(task_id)},
        )


def _task_tags_list_to_dict(tags: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    assignments: Dict[str, Dict[str, Any]] = {}

    for tag in tags:
        if not isinstance(tag, dict):
            continue

        tag_list_id = tag.get("tagListId") or tag.get("tag_list_id") or tag.get("list_id")
        if tag_list_id is None:
            continue

        tag_list_key = str(tag_list_id)
        tag_list = assignments.setdefault(
            tag_list_key,
            {
                "id": tag_list_id,
                "name": tag.get("tagListName") or tag.get("tag_list_name"),
                "inherit": tag.get("inherit", False),
                "hasAssignedTags": True,
                "tags": [],
            },
        )
        tag_list["tags"].append(tag)

    return assignments


def _api_metric_key(method: str, endpoint: str) -> str:
    normalized_endpoint = endpoint.lstrip("/").split("?", 1)[0]
    normalized_endpoint = re.sub(r"/\d+(?=/|$)", "/{id}", normalized_endpoint)
    return f"{method.upper()} {normalized_endpoint}"
