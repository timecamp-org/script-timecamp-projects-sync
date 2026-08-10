import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import requests
from jira import JIRA

JIRA_WORKLOG_PROPERTY_KEY = "timecamp.entry"


@dataclass(frozen=True)
class JiraIssueTarget:
    instance_id: str
    issue_key: str


def generate_jira_org_id(url: str) -> str:
    """Return the stable organization id used in TimeCamp external task ids."""
    hash_value = int(hashlib.md5(url.encode()).hexdigest()[:6], 16)
    return f"org_{hash_value % 1000000}"


def build_jira_project_external_id(instance_id: str, project_key: str) -> str:
    return f"{instance_id}_proj_{project_key}"


def build_jira_issue_external_id(
    instance_id: str,
    project_key: str,
    issue_key: str,
) -> str:
    return f"{build_jira_project_external_id(instance_id, project_key)}_{issue_key}"


def parse_jira_issue_external_id(
    external_task_id: Any,
    instance_ids: Iterable[str],
) -> Optional[JiraIssueTarget]:
    """Parse the external id shape emitted by fetch_jira.py."""
    value = str(external_task_id or "")
    for instance_id in instance_ids:
        prefixes = (
            f"{instance_id}_proj_",
            f"sync_{instance_id}_proj_",
        )
        prefix = next(
            (candidate for candidate in prefixes if value.startswith(candidate)),
            None,
        )
        if prefix is None:
            continue

        suffix = value[len(prefix):]
        match = re.fullmatch(
            r"(?P<project>[A-Za-z][A-Za-z0-9_]*)_(?P=project)-(?P<number>\d+)",
            suffix,
        )
        if not match:
            return None

        return JiraIssueTarget(
            instance_id=instance_id,
            issue_key=f"{match.group('project')}-{match.group('number')}",
        )

    return None


def load_jira_instances(raw_json: Optional[str]) -> List[Dict[str, str]]:
    if not raw_json:
        raise ValueError("JIRA_INSTANCES is not set")

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JIRA_INSTANCES is not valid JSON: {exc}") from exc

    if not isinstance(data, list) or not data:
        raise ValueError("JIRA_INSTANCES must contain at least one Jira instance")

    required = ("name", "url", "email", "token")
    instances: List[Dict[str, str]] = []
    seen_instance_ids = set()
    for index, raw_instance in enumerate(data):
        if not isinstance(raw_instance, dict):
            raise ValueError(f"JIRA_INSTANCES item {index + 1} must be an object")

        missing = [
            key
            for key in required
            if not str(raw_instance.get(key, "")).strip()
        ]
        if missing:
            raise ValueError(
                f"JIRA_INSTANCES item {index + 1} is missing: {', '.join(missing)}"
            )

        instance = {key: str(raw_instance[key]).strip() for key in required}
        instance_id = generate_jira_org_id(instance["url"])
        if instance_id in seen_instance_ids:
            raise ValueError(
                f"JIRA_INSTANCES contains duplicate generated id {instance_id}"
            )
        seen_instance_ids.add(instance_id)
        instance["instance_id"] = instance_id
        instances.append(instance)

    return instances


class JiraClient:
    """Client for Jira Cloud project, issue, and worklog APIs."""

    def __init__(self, server: str, email: str, api_token: str):
        self.server = server.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._validate_authentication()
        self.jira = JIRA(server=self.server, basic_auth=(email, api_token))

    def _validate_authentication(self) -> None:
        try:
            response = self.session.get(
                f"{self.server}/rest/api/3/myself",
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not validate Jira authentication for {self.server}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Jira authentication failed for {self.server} "
                f"(HTTP {response.status_code}). Check the configured email "
                "and API token."
            )

        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not validate Jira authentication for {self.server}: {exc}"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
    ) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.server}/{path.lstrip('/')}",
            params=params,
            json=json_body,
            timeout=timeout,
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _response_json(response: requests.Response) -> Any:
        if not response.content:
            return {}
        return response.json()

    def get_projects(self) -> List[Dict[str, Any]]:
        try:
            projects = self.jira.projects()
            return [self._serialize_project(project) for project in projects]
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch Jira projects from {self.server}: {exc}"
            ) from exc

    def get_issues_for_project(self, project_key: str) -> List[Dict[str, Any]]:
        all_issues = []
        max_results = 100
        next_page_token = None
        fields = [
            "issuetype",
            "summary",
            "status",
            "priority",
            "assignee",
            "reporter",
            "created",
            "updated",
            "project",
            "parent",
            "subtasks",
            "customfield_10014",
            "timetracking",
        ]

        excluded_statuses = ["Done", "Closed", "Resolved", "Completed"]
        excluded_statuses_jql = ", ".join(
            self._quote_jql_value(status) for status in excluded_statuses
        )
        jql = (
            f"project = {self._quote_jql_value(project_key)} "
            f"AND status NOT IN ({excluded_statuses_jql})"
        )

        while True:
            params = {
                "jql": jql,
                "maxResults": max_results,
                "fields": ",".join(fields),
                "expand": "names",
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token

            response = self._request(
                "GET",
                "/rest/api/3/search/jql",
                params=params,
            )
            data = self._response_json(response)
            issues = data.get("issues", [])
            all_issues.extend(self._serialize_issue_json(issue) for issue in issues)

            next_page_token = data.get("nextPageToken")
            if data.get("isLast", True) or not next_page_token:
                break

        return all_issues

    def create_worklog(
        self,
        issue_key: str,
        payload: Dict[str, Any],
        adjust_estimate: str = "leave",
    ) -> Dict[str, Any]:
        response = self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/worklog",
            params={"adjustEstimate": adjust_estimate},
            json_body=payload,
        )
        data = self._response_json(response)
        if not isinstance(data, dict) or not data.get("id"):
            raise ValueError(f"Unexpected Jira worklog response: {data!r}")
        return data

    def update_worklog(
        self,
        issue_key: str,
        worklog_id: str,
        payload: Dict[str, Any],
        adjust_estimate: str = "leave",
    ) -> Dict[str, Any]:
        response = self._request(
            "PUT",
            f"/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
            params={"adjustEstimate": adjust_estimate},
            json_body=payload,
        )
        data = self._response_json(response)
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected Jira worklog response: {data!r}")
        return data

    def get_worklog(
        self,
        issue_key: str,
        worklog_id: str,
    ) -> Dict[str, Any]:
        response = self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
        )
        data = self._response_json(response)
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected Jira worklog response: {data!r}")
        return data

    def delete_worklog(
        self,
        issue_key: str,
        worklog_id: str,
        adjust_estimate: str = "leave",
    ) -> None:
        self._request(
            "DELETE",
            f"/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
            params={"adjustEstimate": adjust_estimate},
        )

    def get_timecamp_worklog_map(self, issue_key: str) -> Dict[str, str]:
        """Return TimeCamp entry ids mapped to worklog ids on one Jira issue."""
        start_at = 0
        max_results = 100
        result: Dict[str, str] = {}

        while True:
            response = self._request(
                "GET",
                f"/rest/api/3/issue/{issue_key}/worklog",
                params={
                    "startAt": start_at,
                    "maxResults": max_results,
                    "expand": "properties",
                },
            )
            data = self._response_json(response)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Unexpected Jira worklog-list response: {data!r}"
                )
            worklogs = data.get("worklogs", [])
            if not isinstance(worklogs, list):
                raise ValueError(
                    f"Unexpected Jira worklog-list response: {data!r}"
                )
            for worklog in worklogs:
                worklog_id = str(worklog.get("id", ""))
                if not worklog_id:
                    continue
                entry_id = self._entry_id_from_expanded_properties(
                    issue_key,
                    worklog_id,
                    worklog.get("properties", []),
                )
                if entry_id:
                    existing_worklog_id = result.get(entry_id)
                    if existing_worklog_id and existing_worklog_id != worklog_id:
                        raise ValueError(
                            f"Jira issue {issue_key} has multiple worklogs for "
                            f"TimeCamp entry {entry_id}: "
                            f"{existing_worklog_id}, {worklog_id}"
                        )
                    result[entry_id] = worklog_id

            total = int(data.get("total", len(worklogs)))
            start_at += len(worklogs)
            if not worklogs or start_at >= total:
                break

        return result

    def _entry_id_from_expanded_properties(
        self,
        issue_key: str,
        worklog_id: str,
        properties: Any,
    ) -> Optional[str]:
        if not isinstance(properties, list):
            return None

        for prop in properties:
            if (
                not isinstance(prop, dict)
                or prop.get("key") != JIRA_WORKLOG_PROPERTY_KEY
            ):
                continue

            value = prop.get("value")
            if value is None:
                response = self._request(
                    "GET",
                    f"/rest/api/3/issue/{issue_key}/worklog/{worklog_id}/properties/"
                    f"{JIRA_WORKLOG_PROPERTY_KEY}",
                )
                property_data = self._response_json(response)
                value = (
                    property_data.get("value")
                    if isinstance(property_data, dict)
                    else None
                )

            if isinstance(value, dict) and value.get("entryId") is not None:
                return str(value["entryId"])

        return None

    @staticmethod
    def _serialize_project(project: Any) -> Dict[str, Any]:
        return {
            "id": project.id,
            "key": project.key,
            "name": project.name,
            "description": getattr(project, "description", ""),
            "lead": getattr(project, "lead", None),
            "project_type_key": getattr(project, "projectTypeKey", ""),
        }

    def _serialize_issue_json(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        fields = issue.get("fields") or {}
        parent = fields.get("parent") or {}
        subtasks = fields.get("subtasks") or []
        epic_link = fields.get("customfield_10014")
        timetracking = fields.get("timetracking") or {}

        serialized = {
            "id": issue.get("id"),
            "key": issue.get("key"),
            "issue_type": self._name(fields.get("issuetype")),
            "summary": fields.get("summary") or "",
            "status": self._name(fields.get("status")),
            "priority": self._name(fields.get("priority")),
            "assignee": self._display_name(fields.get("assignee")),
            "reporter": self._display_name(fields.get("reporter")),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "project_key": (fields.get("project") or {}).get("key", ""),
            "parent": parent.get("key") if parent else None,
            "subtasks": [
                subtask.get("key") for subtask in subtasks if subtask.get("key")
            ],
            "original_estimate": timetracking.get("originalEstimate"),
            "original_estimate_seconds": timetracking.get(
                "originalEstimateSeconds"
            ),
        }

        if epic_link:
            if isinstance(epic_link, dict):
                serialized["epic_link"] = (
                    epic_link.get("key") or epic_link.get("id") or str(epic_link)
                )
            else:
                serialized["epic_link"] = str(epic_link)

        return serialized

    @staticmethod
    def _name(value: Optional[Dict[str, Any]]) -> Optional[str]:
        return value.get("name") if value else None

    @staticmethod
    def _display_name(value: Optional[Dict[str, Any]]) -> Optional[str]:
        return value.get("displayName") if value else None

    @staticmethod
    def _quote_jql_value(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'


def is_http_status(exc: BaseException, status_code: int) -> bool:
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == status_code
    )
