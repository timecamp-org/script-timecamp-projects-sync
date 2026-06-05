from typing import Any, Dict, List, Optional

import requests


TIMECAMP_API_BASE_URL = "https://app.timecamp.com/third_party/api"


class TimeCampClient:
    def __init__(self, api_token: str, base_url: str = TIMECAMP_API_BASE_URL):
        self.base_url = base_url.rstrip("/")
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
        response = self.session.request(method, url, json=json, params=params)
        response.raise_for_status()

        if not response.content:
            return {}

        return response.json()

    def get_tasks(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "tasks")

        if isinstance(data, dict):
            return list(data.values())
        if isinstance(data, list):
            return data

        raise ValueError(f"Unexpected TimeCamp tasks response: {type(data)}")

    def create_task(
        self,
        name: str,
        parent_id: int,
        external_task_id: str,
    ) -> Dict[str, Any]:
        data = {
            "name": name,
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
