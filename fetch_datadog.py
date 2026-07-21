import argparse
import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv


load_dotenv(override=True)


DATADOG_PAGE_SIZE = 100
DATADOG_REQUEST_TIMEOUT = 30
DATADOG_CASES_ROOT_ID = "dd_c"
DATADOG_INCIDENTS_ROOT_ID = "dd_i"
DATADOG_UNASSIGNED_PROJECT_ID = "dd_c_p_unassigned"
DATADOG_UNASSIGNED_SERVICE_ID = "dd_i_s_unassigned"
ACTIVE_CASE_STATUS_GROUPS = {"SG_OPEN", "SG_IN_PROGRESS"}
ACTIVE_INCIDENT_STATES = {"active", "stable"}


class DatadogClient:
    """Client for the Datadog Case Management and Incidents APIs."""

    def __init__(
        self,
        api_key: str,
        app_key: str,
        site: str = "datadoghq.com",
        session: Optional[requests.Session] = None,
    ):
        self.base_url = normalize_datadog_api_url(site)
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "User-Agent": "TimeCamp-Datadog-Sync",
        })

    def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params or {},
            timeout=DATADOG_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def get_case_projects(self) -> List[Dict[str, Any]]:
        """Get all Datadog Case Management projects."""
        data = self._get("/api/v2/cases/projects").get("data", [])
        return data if isinstance(data, list) else []

    def get_cases(self) -> List[Dict[str, Any]]:
        """Get all cases using Datadog's one-based page pagination."""
        cases: List[Dict[str, Any]] = []
        page_number = 1

        while True:
            payload = self._get(
                "/api/v2/cases",
                {
                    "page[size]": DATADOG_PAGE_SIZE,
                    "page[number]": page_number,
                },
            )
            page = payload.get("data", [])
            if not isinstance(page, list) or not page:
                break

            cases.extend(page)
            total = _get_nested(payload, "meta", "page", "total")
            if len(page) < DATADOG_PAGE_SIZE or _reached_total(len(cases), total):
                break

            page_number += 1

        return cases

    def get_incidents(self) -> List[Dict[str, Any]]:
        """Get all incidents using Datadog's offset pagination."""
        incidents: List[Dict[str, Any]] = []
        offset = 0

        while True:
            payload = self._get(
                "/api/v2/incidents",
                {
                    "page[size]": DATADOG_PAGE_SIZE,
                    "page[offset]": offset,
                },
            )
            page = payload.get("data", [])
            if not isinstance(page, list) or not page:
                break

            incidents.extend(page)
            if len(page) < DATADOG_PAGE_SIZE:
                break

            next_offset = _get_nested(payload, "meta", "pagination", "next_offset")
            parsed_next_offset = _optional_int(next_offset)
            if parsed_next_offset is not None and parsed_next_offset > offset:
                offset = parsed_next_offset
            else:
                offset += len(page)

        return incidents


class DatadogFetcher:
    """Fetch Datadog cases and incidents into TimeCamp's tasks.json format."""

    def __init__(self, client: Optional[DatadogClient] = None):
        if client is not None:
            self.client = client
            return

        api_key = os.getenv("DD_API_KEY")
        app_key = os.getenv("DD_APP_KEY")
        site = os.getenv("DD_SITE", "datadoghq.com")

        if not api_key or not app_key:
            raise ValueError("DD_API_KEY and DD_APP_KEY must be set in .env")

        self.client = DatadogClient(api_key, app_key, site)

    def fetch_all_data(self) -> List[Dict[str, Any]]:
        """Fetch and flatten both Datadog hierarchies."""
        print("Fetching Datadog Case Management projects...")
        projects = self.client.get_case_projects()
        print(f"  Found {len(projects)} projects")

        print("Fetching Datadog cases...")
        cases = [case for case in self.client.get_cases() if case_is_active(case)]
        print(f"  Found {len(cases)} active cases")

        print("Fetching Datadog incidents...")
        incidents = [
            incident
            for incident in self.client.get_incidents()
            if incident_is_active(incident)
        ]
        print(f"  Found {len(incidents)} active/stable incidents")

        return build_case_tasks(projects, cases) + build_incident_tasks(incidents)

    def save_to_json(
        self,
        data: List[Dict[str, Any]],
        filename: str = "tasks.json",
    ) -> str:
        """Save flattened data after every API request has succeeded."""
        with open(filename, "w", encoding="utf-8") as output_file:
            json.dump(data, output_file, indent=2, ensure_ascii=False)

        return filename


def normalize_datadog_api_url(site: str) -> str:
    """Convert a Datadog site domain or URL to its HTTPS API base URL."""
    normalized_site = (site or "datadoghq.com").strip().rstrip("/")
    if not normalized_site:
        normalized_site = "datadoghq.com"

    if "://" in normalized_site:
        hostname = urlparse(normalized_site).hostname or ""
    else:
        hostname = normalized_site.split("/", 1)[0]

    hostname = hostname.strip().lower()
    if not hostname:
        raise ValueError("DD_SITE must contain a valid Datadog site domain")

    if not hostname.startswith("api."):
        hostname = f"api.{hostname}"

    return f"https://{hostname}"


def project_task_id(project_id: Any) -> str:
    return f"dd_c_p_{project_id}"


def case_task_id(case_id: Any) -> str:
    return f"dd_c_{case_id}"


def service_task_id(service_name: str) -> str:
    normalized_name = normalize_service_name(service_name).casefold()
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:12]
    return f"dd_i_s_{digest}"


def incident_task_id(incident_id: Any) -> str:
    return f"dd_i_{incident_id}"


def normalize_service_name(service_name: Any) -> str:
    return " ".join(str(service_name).split())


def case_is_active(case: Dict[str, Any]) -> bool:
    attributes = case.get("attributes") or {}
    if attributes.get("archived_at"):
        return False

    status_group = str(attributes.get("status_group") or "").upper()
    return status_group in ACTIVE_CASE_STATUS_GROUPS


def incident_state(incident: Dict[str, Any]) -> str:
    attributes = incident.get("attributes") or {}
    state = attributes.get("state")
    if state is None:
        state = _get_nested(attributes, "fields", "state", "value")
    return str(state or "").strip().casefold()


def incident_is_active(incident: Dict[str, Any]) -> bool:
    attributes = incident.get("attributes") or {}
    if attributes.get("archived"):
        return False
    return incident_state(incident) in ACTIVE_INCIDENT_STATES


def incident_services(incident: Dict[str, Any]) -> List[str]:
    raw_value = _get_nested(incident, "attributes", "fields", "services", "value")
    if isinstance(raw_value, str):
        values: Iterable[Any] = [raw_value]
    elif isinstance(raw_value, list):
        values = raw_value
    else:
        values = []

    services_by_key: Dict[str, str] = {}
    for value in values:
        service_name = normalize_service_name(value)
        if not service_name:
            continue
        key = service_name.casefold()
        current_name = services_by_key.get(key)
        if current_name is None or service_name < current_name:
            services_by_key[key] = service_name

    return sorted(services_by_key.values(), key=lambda name: (name.casefold(), name))


def build_case_tasks(
    projects: List[Dict[str, Any]],
    cases: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = [{
        "name": "Datadog Cases",
        "task_id": DATADOG_CASES_ROOT_ID,
        "parent_id": 0,
    }]

    projects_by_id = {
        str(project["id"]): project
        for project in projects
        if project.get("id") is not None
    }
    grouped_cases: Dict[Optional[str], List[Dict[str, Any]]] = {
        project_id: [] for project_id in projects_by_id
    }

    for case in cases:
        project_id = _case_project_id(case)
        group_id = project_id if project_id in projects_by_id else None
        grouped_cases.setdefault(group_id, []).append(case)

    sorted_projects = sorted(
        projects_by_id.items(),
        key=lambda item: (
            str((item[1].get("attributes") or {}).get("name") or "").casefold(),
            item[0],
        ),
    )
    for project_id, project in sorted_projects:
        attributes = project.get("attributes") or {}
        project_name = attributes.get("name") or attributes.get("key") or f"Project {project_id}"
        parent_task_id = project_task_id(project_id)
        tasks.append({
            "name": project_name,
            "task_id": parent_task_id,
            "parent_id": DATADOG_CASES_ROOT_ID,
        })
        tasks.extend(_case_leaf_tasks(grouped_cases.get(project_id, []), parent_task_id))

    unassigned_cases = grouped_cases.get(None, [])
    if unassigned_cases:
        tasks.append({
            "name": "Unassigned project",
            "task_id": DATADOG_UNASSIGNED_PROJECT_ID,
            "parent_id": DATADOG_CASES_ROOT_ID,
        })
        tasks.extend(_case_leaf_tasks(unassigned_cases, DATADOG_UNASSIGNED_PROJECT_ID))

    return tasks


def build_incident_tasks(incidents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = [{
        "name": "Datadog Incidents",
        "task_id": DATADOG_INCIDENTS_ROOT_ID,
        "parent_id": 0,
    }]
    grouped_incidents: Dict[Optional[str], List[Dict[str, Any]]] = {}
    service_display_names: Dict[str, str] = {}

    for incident in sorted(incidents, key=_incident_sort_key):
        services = incident_services(incident)
        if not services:
            grouped_incidents.setdefault(None, []).append(incident)
            continue

        service_name = services[0]
        service_key = service_name.casefold()
        grouped_incidents.setdefault(service_key, []).append(incident)
        current_name = service_display_names.get(service_key)
        if current_name is None or service_name < current_name:
            service_display_names[service_key] = service_name

    for service_key in sorted(
        service_display_names,
        key=lambda key: (service_display_names[key].casefold(), service_display_names[key]),
    ):
        service_name = service_display_names[service_key]
        parent_task_id = service_task_id(service_name)
        tasks.append({
            "name": service_name,
            "task_id": parent_task_id,
            "parent_id": DATADOG_INCIDENTS_ROOT_ID,
        })
        tasks.extend(
            _incident_leaf_tasks(grouped_incidents.get(service_key, []), parent_task_id)
        )

    unassigned_incidents = grouped_incidents.get(None, [])
    if unassigned_incidents:
        tasks.append({
            "name": "Unassigned service",
            "task_id": DATADOG_UNASSIGNED_SERVICE_ID,
            "parent_id": DATADOG_INCIDENTS_ROOT_ID,
        })
        tasks.extend(
            _incident_leaf_tasks(unassigned_incidents, DATADOG_UNASSIGNED_SERVICE_ID)
        )

    return tasks


def _case_leaf_tasks(
    cases: List[Dict[str, Any]],
    parent_id: str,
) -> List[Dict[str, Any]]:
    tasks = []
    for case in sorted(cases, key=_case_sort_key):
        case_id = case.get("id")
        if case_id is None:
            continue
        attributes = case.get("attributes") or {}
        key = str(attributes.get("key") or "").strip()
        title = str(attributes.get("title") or "").strip()
        fallback = f"Case {key or case_id}"
        tasks.append({
            "name": _prefixed_name(key, title, fallback),
            "task_id": case_task_id(case_id),
            "parent_id": parent_id,
        })
    return tasks


def _incident_leaf_tasks(
    incidents: List[Dict[str, Any]],
    parent_id: str,
) -> List[Dict[str, Any]]:
    tasks = []
    for incident in sorted(incidents, key=_incident_sort_key):
        incident_id = incident.get("id")
        if incident_id is None:
            continue
        attributes = incident.get("attributes") or {}
        public_id = attributes.get("public_id")
        public_key = f"INC-{public_id}" if public_id is not None else ""
        title = str(attributes.get("title") or "").strip()
        fallback = f"Incident {public_key or incident_id}"
        tasks.append({
            "name": _prefixed_name(public_key, title, fallback),
            "task_id": incident_task_id(incident_id),
            "parent_id": parent_id,
        })
    return tasks


def _prefixed_name(public_key: str, title: str, fallback: str) -> str:
    if public_key and title:
        return f"[{public_key}] {title}"
    if title:
        return title
    return fallback


def _case_project_id(case: Dict[str, Any]) -> Optional[str]:
    project_id = _get_nested(case, "relationships", "project", "data", "id")
    return str(project_id) if project_id is not None else None


def _case_sort_key(case: Dict[str, Any]):
    attributes = case.get("attributes") or {}
    return (
        str(attributes.get("key") or "").casefold(),
        str(attributes.get("title") or "").casefold(),
        str(case.get("id") or ""),
    )


def _incident_sort_key(incident: Dict[str, Any]):
    attributes = incident.get("attributes") or {}
    public_id = _optional_int(attributes.get("public_id"))
    return (
        public_id is None,
        public_id if public_id is not None else 0,
        str(attributes.get("title") or "").casefold(),
        str(incident.get("id") or ""),
    )


def _get_nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reached_total(item_count: int, total: Any) -> bool:
    parsed_total = _optional_int(total)
    return parsed_total is not None and item_count >= parsed_total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch active Datadog cases and incidents into tasks.json format "
            "for TimeCamp sync."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        default="tasks.json",
        help="Output file path (default: tasks.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("Starting Datadog data fetch...")
    print(f"Started at: {datetime.now()}")

    fetcher = DatadogFetcher()
    data = fetcher.fetch_all_data()
    filename = fetcher.save_to_json(data, args.output)

    project_count = sum(
        1
        for item in data
        if str(item["task_id"]).startswith("dd_c_p_")
        and item["task_id"] != DATADOG_UNASSIGNED_PROJECT_ID
    )
    case_count = sum(
        1 for item in data if str(item["task_id"]).startswith("dd_c_")
        and not str(item["task_id"]).startswith("dd_c_p_")
    )
    service_count = sum(
        1
        for item in data
        if str(item["task_id"]).startswith("dd_i_s_")
        and item["task_id"] != DATADOG_UNASSIGNED_SERVICE_ID
    )
    incident_count = sum(
        1
        for item in data
        if str(item["task_id"]).startswith("dd_i_")
        and not str(item["task_id"]).startswith("dd_i_s_")
        and item["task_id"] != DATADOG_INCIDENTS_ROOT_ID
    )

    print(f"\nData fetch completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")
    print("\nSummary:")
    print(f"  Case projects: {project_count}")
    print(f"  Active cases: {case_count}")
    print(f"  Incident services: {service_count}")
    print(f"  Active/stable incidents: {incident_count}")
    print(f"  Total items: {len(data)}")

    if data:
        print("\nStructure preview:")
        for item in data[:20]:
            print(
                f"  {item['name']} "
                f"(ID: {item['task_id']}, Parent: {item['parent_id']})"
            )
        if len(data) > 20:
            print(f"  ... and {len(data) - 20} more items")


if __name__ == "__main__":
    main()
