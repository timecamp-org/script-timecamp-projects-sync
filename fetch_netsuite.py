import argparse
import json
import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv

from src.netsuite_client import NetSuiteClient

DEFAULT_CONFIG_FILE = "netsuite_config.json"
DEFAULT_OUTPUT_FILE = "tasks.json"
NETSUITE_PROJECT_PREFIX = "netsuite_project_"
NETSUITE_PROJECT_TASK_PREFIX = "netsuite_project_task_"
DEFAULT_CLASSIFICATION_TAG_LIST = "CAPEX / OPEX"


def project_external_id(project_id: Any) -> str:
    return f"{NETSUITE_PROJECT_PREFIX}{project_id}"


def project_task_external_id(project_task_id: Any) -> str:
    return f"{NETSUITE_PROJECT_TASK_PREFIX}{project_task_id}"


def row_value(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    wanted = key.casefold()
    for raw_key, value in row.items():
        if str(raw_key).casefold() == wanted:
            return value
    return default


def optional_id(value: Any) -> Optional[str]:
    if value in (None, "", 0, "0"):
        return None
    return str(value).strip() or None


def required_id(row: Dict[str, Any], key: str, record_label: str) -> str:
    value = optional_id(row_value(row, key))
    if value is None:
        raise ValueError(f"{record_label} SuiteQL row has no {key}: {row!r}")
    return value


def required_name(row: Dict[str, Any], record_label: str) -> str:
    value = str(row_value(row, "name") or "").strip()
    if not value:
        raise ValueError(f"{record_label} SuiteQL row has no name: {row!r}")
    return value


def is_inactive(row: Dict[str, Any]) -> bool:
    value = row_value(row, "is_inactive")
    if isinstance(value, str):
        return value.strip().casefold() in {"t", "true", "yes", "1"}
    return bool(value)


def load_config(path: str) -> Dict[str, Any]:
    config_path = Path(path)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"NetSuite config not found: {config_path}. "
            "Copy netsuite_config.example.json first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in NetSuite config {config_path}: {exc}"
        ) from exc

    if not isinstance(config, dict):
        raise ValueError("NetSuite config must contain a JSON object")
    return config


def classification_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("classification") or {}
    if not isinstance(raw, dict):
        raise ValueError("classification config must be an object")
    return raw


def normalize_classification(
    value: Any,
    config: Dict[str, Any],
) -> Optional[str]:
    if value is None or not str(value).strip():
        default = config.get("default")
        if default is None or not str(default).strip():
            return None
        value = default

    normalized = " ".join(str(value).strip().split())
    value_map = config.get("value_map") or {}
    if not isinstance(value_map, dict):
        raise ValueError("classification.value_map must be an object")

    mapped_values = {
        str(raw_key).strip().casefold(): str(mapped).strip()
        for raw_key, mapped in value_map.items()
    }
    classification = mapped_values.get(normalized.casefold(), normalized.upper())
    allowed_values = config.get("allowed_values", ["CAPEX", "OPEX"])
    allowed_by_key = {
        str(allowed).strip().casefold(): str(allowed).strip()
        for allowed in allowed_values
    }
    canonical = allowed_by_key.get(classification.casefold())
    if canonical is None:
        raise ValueError(
            f"Unknown CAPEX/OPEX value {value!r}; configure classification.value_map"
        )
    return canonical


def estimate_seconds(row: Dict[str, Any]) -> Optional[int]:
    raw_seconds = row_value(row, "original_estimate_seconds")
    raw_hours = row_value(row, "estimated_work_hours")
    if raw_seconds in (None, "") and raw_hours in (None, ""):
        return None

    try:
        value = Decimal(
            str(raw_seconds if raw_seconds not in (None, "") else raw_hours)
        )
    except InvalidOperation as exc:
        raise ValueError(f"Invalid NetSuite project task estimate: {row!r}") from exc
    if value < 0:
        raise ValueError(f"NetSuite project task estimate cannot be negative: {row!r}")
    if raw_seconds in (None, ""):
        value *= Decimal(3600)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_task_structure(
    project_rows: Iterable[Dict[str, Any]],
    project_task_rows: Iterable[Dict[str, Any]],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    class_config = classification_config(config)
    tag_list_name = str(
        class_config.get("tag_list_name") or DEFAULT_CLASSIFICATION_TAG_LIST
    ).strip()
    if not tag_list_name:
        raise ValueError("classification.tag_list_name must not be empty")

    projects_by_id: Dict[str, Dict[str, Any]] = {}
    for row in project_rows:
        if is_inactive(row):
            continue
        project_id = required_id(row, "id", "Project")
        if project_id in projects_by_id:
            raise ValueError(f"Duplicate NetSuite project id: {project_id}")
        projects_by_id[project_id] = row

    project_tasks_by_id: Dict[str, Dict[str, Any]] = {}
    task_project_ids: Dict[str, str] = {}
    for row in project_task_rows:
        if is_inactive(row):
            continue
        task_id = required_id(row, "id", "Project task")
        project_id = required_id(row, "project_id", "Project task")
        if task_id in project_tasks_by_id:
            raise ValueError(f"Duplicate NetSuite project task id: {task_id}")
        if project_id not in projects_by_id:
            raise ValueError(
                f"NetSuite project task {task_id} references missing active "
                f"project {project_id}"
            )
        project_tasks_by_id[task_id] = row
        task_project_ids[task_id] = project_id

    project_classifications: Dict[str, Optional[str]] = {}
    visiting_projects: set[str] = set()

    def get_project_classification(project_id: str) -> Optional[str]:
        if project_id in project_classifications:
            return project_classifications[project_id]
        if project_id in visiting_projects:
            raise ValueError(f"Cycle in NetSuite project hierarchy at {project_id}")
        visiting_projects.add(project_id)
        row = projects_by_id[project_id]
        direct_value = row_value(row, "capex_opex")
        if direct_value not in (None, ""):
            value = normalize_classification(direct_value, class_config)
        else:
            parent_id = optional_id(row_value(row, "parent_id"))
            value = (
                get_project_classification(parent_id)
                if parent_id in projects_by_id
                else normalize_classification(None, class_config)
            )
        visiting_projects.remove(project_id)
        project_classifications[project_id] = value
        return value

    task_classifications: Dict[str, Optional[str]] = {}
    visiting_tasks: set[str] = set()

    def get_task_classification(task_id: str) -> Optional[str]:
        if task_id in task_classifications:
            return task_classifications[task_id]
        if task_id in visiting_tasks:
            raise ValueError(f"Cycle in NetSuite project task hierarchy at {task_id}")
        visiting_tasks.add(task_id)
        row = project_tasks_by_id[task_id]
        direct_value = row_value(row, "capex_opex")
        parent_id = optional_id(row_value(row, "parent_id"))
        if direct_value not in (None, ""):
            value = normalize_classification(direct_value, class_config)
        elif parent_id in project_tasks_by_id:
            if task_project_ids[parent_id] != task_project_ids[task_id]:
                raise ValueError(
                    f"NetSuite project task {task_id} has a parent from another project"
                )
            value = get_task_classification(parent_id)
        else:
            value = get_project_classification(task_project_ids[task_id])
        visiting_tasks.remove(task_id)
        task_classifications[task_id] = value
        return value

    output: List[Dict[str, Any]] = []
    for project_id in sorted(projects_by_id, key=_id_sort_key):
        row = projects_by_id[project_id]
        parent_project_id = optional_id(row_value(row, "parent_id"))
        parent_id: Any = 0
        if parent_project_id in projects_by_id and parent_project_id != project_id:
            parent_id = project_external_id(parent_project_id)

        external_id = project_external_id(project_id)
        classification = get_project_classification(project_id)
        if class_config.get("required") and not classification:
            raise ValueError(
                f"NetSuite project {project_id} has no required "
                "CAPEX/OPEX classification"
            )
        task: Dict[str, Any] = {
            "name": required_name(row, "Project"),
            "task_id": external_id,
            "external_task_id": external_id,
            "parent_id": parent_id,
            "netsuite": {
                "project_id": project_id,
                "project_task_id": None,
                "activity_id": optional_id(row_value(row, "activity_id")),
                "capex_opex": classification,
            },
        }
        if classification:
            task["mandatory_tags"] = {tag_list_name: [classification]}
        output.append(task)

    for task_id in sorted(project_tasks_by_id, key=_id_sort_key):
        row = project_tasks_by_id[task_id]
        project_id = task_project_ids[task_id]
        parent_task_id = optional_id(row_value(row, "parent_id"))
        if parent_task_id in project_tasks_by_id:
            if task_project_ids[parent_task_id] != project_id:
                raise ValueError(
                    f"NetSuite project task {task_id} has a parent from another project"
                )
            parent_id = project_task_external_id(parent_task_id)
        else:
            parent_id = project_external_id(project_id)

        external_id = project_task_external_id(task_id)
        classification = get_task_classification(task_id)
        if class_config.get("required") and not classification:
            raise ValueError(
                f"NetSuite project task {task_id} has no required "
                "CAPEX/OPEX classification"
            )
        task = {
            "name": required_name(row, "Project task"),
            "task_id": external_id,
            "external_task_id": external_id,
            "parent_id": parent_id,
            "netsuite": {
                "project_id": project_id,
                "project_task_id": task_id,
                "activity_id": optional_id(row_value(row, "activity_id")),
                "capex_opex": classification,
            },
        }
        estimated_seconds = estimate_seconds(row)
        if estimated_seconds is not None:
            task["original_estimate_seconds"] = estimated_seconds
        if classification:
            task["mandatory_tags"] = {tag_list_name: [classification]}
        output.append(task)

    _validate_output_hierarchy(output)
    return output


def _id_sort_key(value: str) -> tuple:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value.casefold())


def _validate_output_hierarchy(tasks: List[Dict[str, Any]]) -> None:
    tasks_by_id = {str(task["task_id"]): task for task in tasks}
    for task in tasks:
        seen = set()
        current = task
        while current.get("parent_id") not in (None, 0, "0"):
            current_id = str(current["task_id"])
            if current_id in seen:
                raise ValueError(f"Cycle in generated hierarchy at {current_id}")
            seen.add(current_id)
            parent_id = str(current["parent_id"])
            parent = tasks_by_id.get(parent_id)
            if parent is None:
                raise ValueError(
                    f"Generated task {current_id} references missing parent {parent_id}"
                )
            current = parent


class NetSuiteFetcher:
    def __init__(
        self,
        config: Dict[str, Any],
        client: Optional[NetSuiteClient] = None,
    ):
        self.config = config
        self.client = client or NetSuiteClient.from_env()

    def fetch_all_data(self) -> List[Dict[str, Any]]:
        suiteql = self.config.get("suiteql") or {}
        if not isinstance(suiteql, dict):
            raise ValueError("suiteql config must be an object")
        projects_query = str(suiteql.get("projects") or "").strip()
        project_tasks_query = str(suiteql.get("project_tasks") or "").strip()
        if not projects_query or not project_tasks_query:
            raise ValueError(
                "suiteql.projects and suiteql.project_tasks must be configured"
            )

        print("Fetching projects from NetSuite...")
        projects = self.client.suiteql(projects_query)
        print(f"  Found {len(projects)} project rows")
        print("Fetching project tasks from NetSuite...")
        project_tasks = self.client.suiteql(project_tasks_query)
        print(f"  Found {len(project_tasks)} project task rows")
        return build_task_structure(projects, project_tasks, self.config)

    def save_to_json(self, data: List[Dict[str, Any]], filename: str) -> str:
        Path(filename).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return filename


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch NetSuite projects and project tasks for TimeCamp sync."
    )
    parser.add_argument(
        "--config",
        default=os.getenv("NETSUITE_CONFIG_FILE", DEFAULT_CONFIG_FILE),
        help=f"NetSuite mapping JSON (default: {DEFAULT_CONFIG_FILE})",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Output tasks JSON (default: {DEFAULT_OUTPUT_FILE})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    load_dotenv(override=True)
    args = parse_args(argv)
    config = load_config(args.config)
    fetcher = NetSuiteFetcher(config)
    data = fetcher.fetch_all_data()
    fetcher.save_to_json(data, args.output)
    projects = sum(
        1
        for task in data
        if str(task["task_id"]).startswith(NETSUITE_PROJECT_PREFIX)
        and not str(task["task_id"]).startswith(NETSUITE_PROJECT_TASK_PREFIX)
    )
    print(f"Saved {len(data)} items to {args.output}")
    print(f"  Projects: {projects}")
    print(f"  Project tasks: {len(data) - projects}")
    print("  Users synchronized: 0 (intentionally out of scope)")


if __name__ == "__main__":
    main()
