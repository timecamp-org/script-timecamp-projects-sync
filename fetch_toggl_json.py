import argparse
import json
from datetime import datetime
from typing import Any, Dict, List, Optional


def load_projects(filename: str) -> List[Dict[str, Any]]:
    """Load Toggl projects exported as JSON."""
    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"{filename} must contain a JSON array of Toggl projects")

    projects = []
    for project in data:
        if isinstance(project, dict):
            projects.append(project)
        else:
            print(f"Warning: skipping non-object project entry: {project!r}")

    return projects


def client_task_id(project: Dict[str, Any]) -> Optional[str]:
    """Return a stable task ID for the project's Toggl client."""
    client_id = project.get("client_id") or project.get("cid")
    if client_id:
        return f"toggl_client_{client_id}"

    return None


def project_task_id(project: Dict[str, Any]) -> str:
    """Return a stable task ID for the Toggl project."""
    project_id = project.get("id")
    if not project_id:
        raise ValueError(f"Project is missing id: {project!r}")

    return f"toggl_project_{project_id}"


def build_task_structure(
    projects: List[Dict[str, Any]],
    active_only: bool = True,
) -> List[Dict[str, Any]]:
    """Convert Toggl projects into sync_projects.py flat task structure."""
    flattened_data: List[Dict[str, Any]] = []
    client_names: Dict[str, str] = {}

    for project in projects:
        if active_only and (
            project.get("active") is False or project.get("status") == "archived"
        ):
            continue

        client_id = client_task_id(project)
        client_name = project.get("client_name")

        if client_id and client_name and client_id not in client_names:
            client_names[client_id] = client_name

    for task_id, name in sorted(
        client_names.items(),
        key=lambda item: item[1].lower(),
    ):
        flattened_data.append({
            "name": name,
            "task_id": task_id,
            "parent_id": 0
        })

    for project in sorted(projects, key=lambda item: str(item.get("name", "")).lower()):
        if active_only and (
            project.get("active") is False or project.get("status") == "archived"
        ):
            continue

        name = project.get("name")
        if not name:
            print(f"Warning: skipping project without name: {project!r}")
            continue

        parent_id = client_task_id(project)
        if parent_id not in client_names:
            parent_id = 0

        flattened_data.append({
            "name": name,
            "task_id": project_task_id(project),
            "parent_id": parent_id
        })

    return flattened_data


def save_to_json(data: List[Dict[str, Any]], filename: str) -> str:
    """Save data to JSON file."""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Toggl projects JSON into tasks.json format for TimeCamp sync."
    )
    parser.add_argument(
        "projects_json",
        help="Path to Toggl projects JSON export, e.g. projects.json",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="tasks.json",
        help="Output file path (default: tasks.json)",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include inactive or archived projects in the output",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    print("Starting Toggl JSON conversion...")
    print(f"Started at: {datetime.now()}")

    projects = load_projects(args.projects_json)
    data = build_task_structure(projects, active_only=not args.include_archived)
    filename = save_to_json(data, args.output)

    clients = len([
        item
        for item in data
        if item["parent_id"] == 0
        and str(item["task_id"]).startswith("toggl_client_")
    ])
    projects_count = len([
        item
        for item in data
        if str(item["task_id"]).startswith("toggl_project_")
    ])

    print(f"\nConversion completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")
    print("\nSummary:")
    print(f"  Clients: {clients}")
    print(f"  Projects: {projects_count}")
    print(f"  Total items: {len(data)}")

    if data:
        print("\nStructure preview:")
        for item in data[:20]:
            indent = "" if item["parent_id"] == 0 else "  "
            level = (
                "[CLIENT]"
                if str(item["task_id"]).startswith("toggl_client_")
                else "[PROJECT]"
            )
            print(
                f"  {indent}{level} {item['name']} "
                f"(ID: {item['task_id']}, Parent: {item['parent_id']})"
            )

        if len(data) > 20:
            print(f"  ... and {len(data) - 20} more items")


if __name__ == "__main__":
    main()
