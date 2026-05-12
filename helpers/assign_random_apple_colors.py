import argparse
import os
import random
import sys
from typing import Any, Dict, List, Optional, Sequence


TIMECAMP_TASKS_URL = "https://app.timecamp.com/third_party/api/tasks"
TIMECAMP_TASK_COLOR_URL = "https://app.timecamp.com/third_party/api/task_color"

TIMECAMP_TO_APPLE_COLOR_MAP = {
    "#F9C947": "#FFCC00",
    "#E5B35C": "#FF9500",
    "#F4AEBB": "#FF2D55",
    "#725E5E": "#8E8E93",
    "#8EA2D6": "#5AC8FA",
    "#5E76CC": "#007AFF",
    "#2C448E": "#5856D6",
    "#388EF8": "#007AFF",
    "#063DC5": "#5856D6",
    "#4B5A74": "#8E8E93",
    "#42826E": "#34C759",
    "#398E9D": "#5AC8FA",
    "#76B6BB": "#64D2FF",
    "#59C694": "#30D158",
    "#68E3B1": "#30D158",
}

APPLE_COLORS = tuple(dict.fromkeys(TIMECAMP_TO_APPLE_COLOR_MAP.values()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assign random Apple palette colors to direct child tasks in TimeCamp."
        ),
        epilog=(
            "Examples:\n"
            "  python3 helpers/assign_random_apple_colors.py --dry-run\n"
            "  python3 helpers/assign_random_apple_colors.py --parent-id 0 --seed 42\n"
            "  python3 helpers/assign_random_apple_colors.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--parent-id",
        type=int,
        help=(
            "TimeCamp parent task whose direct children should be colored. "
            "Defaults to TIMECAMP_TASK_ID from .env, or 0 for root tasks."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print color assignments without updating TimeCamp.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived direct child tasks.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Optional random seed for repeatable color assignments.",
    )

    return parser.parse_args()


class TimeCampClient:
    def __init__(self, api_token: str):
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Missing dependency: requests. Run: pip install -r requirements.txt"
            ) from exc

        self.requests = requests
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def get_tasks(self) -> List[Dict[str, Any]]:
        response = self.requests.get(TIMECAMP_TASKS_URL, headers=self.headers)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            return list(data.values())
        if isinstance(data, list):
            return data

        raise ValueError(f"Unexpected TimeCamp tasks response: {type(data)}")

    def set_task_color(
        self,
        task_id: int,
        color: str,
        is_for_all_subtasks: bool = True,
    ) -> Dict[str, Any]:
        payload = {
            "task_id": task_id,
            "color": color,
            "is_for_all_subtasks": is_for_all_subtasks,
        }
        response = self.requests.put(
            TIMECAMP_TASK_COLOR_URL,
            headers=self.headers,
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def normalize_task_id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def configured_parent_id() -> int:
    value = os.getenv("TIMECAMP_TASK_ID")
    if not value or value.strip() == "":
        return 0

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"TIMECAMP_TASK_ID must be numeric, got: {value!r}") from exc


def task_name(task: Dict[str, Any]) -> str:
    return str(task.get("name") or "(unnamed task)")


def is_archived(task: Dict[str, Any]) -> bool:
    archived = task.get("archived")
    if isinstance(archived, str):
        return archived.lower() in {"1", "true", "yes"}

    return bool(archived)


def is_direct_child(task: Dict[str, Any], parent_id: int) -> bool:
    task_parent_id = normalize_task_id(task.get("parent_id"))

    if parent_id == 0:
        return task_parent_id in (None, 0)

    return task_parent_id == parent_id


def select_level1_tasks(
    tasks: List[Dict[str, Any]],
    parent_id: int,
    include_archived: bool,
) -> List[Dict[str, Any]]:
    selected = []

    for task in tasks:
        if not is_direct_child(task, parent_id):
            continue
        if not include_archived and is_archived(task):
            continue
        if normalize_task_id(task.get("task_id")) is None:
            print(f"Warning: skipping task without numeric task_id: {task!r}")
            continue

        selected.append(task)

    return sorted(selected, key=lambda item: task_name(item).lower())


def assign_colors(
    tasks: Sequence[Dict[str, Any]],
    rng: random.Random,
) -> List[Dict[str, Any]]:
    assignments = []

    for task in tasks:
        task_id = normalize_task_id(task.get("task_id"))
        if task_id is None:
            continue

        assignments.append({
            "task_id": task_id,
            "name": task_name(task),
            "color": rng.choice(APPLE_COLORS),
        })

    return assignments


def main() -> int:
    args = parse_args()

    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        load_dotenv = None

    if load_dotenv:
        load_dotenv(override=True)

    api_token = os.getenv("TIMECAMP_API_TOKEN")
    if not api_token:
        print(
            "Error: TIMECAMP_API_TOKEN is not set. "
            "If you rely on .env, run: pip install -r requirements.txt"
        )
        return 1

    try:
        parent_id = args.parent_id if args.parent_id is not None else configured_parent_id()
        client = TimeCampClient(api_token)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1

    tasks = client.get_tasks()
    level1_tasks = select_level1_tasks(
        tasks,
        parent_id=parent_id,
        include_archived=args.include_archived,
    )

    if not level1_tasks:
        print(f"No level 1 tasks found under parent {parent_id}.")
        return 0

    rng = random.Random(args.seed)
    assignments = assign_colors(level1_tasks, rng)

    action = "Would update" if args.dry_run else "Updating"
    print(
        f"{action} {len(assignments)} level 1 task color(s) "
        f"under parent {parent_id}:"
    )

    failed = 0
    for assignment in assignments:
        print(
            f"  - {assignment['task_id']}: {assignment['name']} "
            f"-> {assignment['color']}"
        )

        if args.dry_run:
            continue

        try:
            client.set_task_color(
                task_id=assignment["task_id"],
                color=assignment["color"],
                is_for_all_subtasks=True,
            )
        except Exception as exc:
            failed += 1
            print(f"    Error: {exc}")

    if args.dry_run:
        print("Dry run complete.")
        return 0

    if failed:
        print(f"Color assignment complete with {failed} failure(s).")
        return 1

    print("Color assignment complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
