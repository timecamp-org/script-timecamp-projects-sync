import argparse
import os
import sys
from typing import Any, Dict, List, Optional


TIMECAMP_ASSIGN_USERS_URL = (
    "https://app.timecamp.com/third_party/api/v3/projects/{task_id}/assign"
)
TIMECAMP_TASKS_URL = "https://app.timecamp.com/third_party/api/tasks"


def parse_comma_separated_ints(value: str) -> List[int]:
    ids = []

    for raw_value in value.split(","):
        raw_value = raw_value.strip()
        if not raw_value:
            continue

        try:
            ids.append(int(raw_value))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"values must be comma-separated integers, got: {value!r}"
            ) from exc

    if not ids:
        raise argparse.ArgumentTypeError("at least one ID is required")

    return list(dict.fromkeys(ids))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assign one or more users to TimeCamp tasks.",
        epilog=(
            "Examples:\n"
            "  python3 helpers/assign_users_to_task.py --user-ids 364263,364264\n"
            "  python3 helpers/assign_users_to_task.py --task-ids 34523534,34523535 --user-ids 364263\n"
            "  python3 helpers/assign_users_to_task.py --task-ids 34523534 --user-ids 364263 --role-id 3"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--task-ids",
        type=parse_comma_separated_ints,
        help=(
            "Comma-separated TimeCamp task IDs. If omitted, all root-level "
            "tasks are used."
        ),
    )
    parser.add_argument(
        "--user-ids",
        required=True,
        type=parse_comma_separated_ints,
        help="Comma-separated TimeCamp user IDs, e.g. 364263,364264.",
    )
    parser.add_argument(
        "--role-id",
        type=int,
        default=3,
        help="Project role ID to assign to all users (default: 3).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request without updating TimeCamp.",
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

    def assign_users(
        self,
        task_id: int,
        user_ids: List[int],
        role_id: int,
    ) -> Dict[str, Any]:
        payload = {
            "userIds": user_ids,
            "roleId": role_id,
        }
        response = self.requests.put(
            TIMECAMP_ASSIGN_USERS_URL.format(task_id=task_id),
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


def is_root_level_task(task: Dict[str, Any]) -> bool:
    return normalize_task_id(task.get("parent_id")) in (None, 0)


def task_name(task: Dict[str, Any]) -> str:
    return str(task.get("name") or "(unnamed task)")


def root_level_task_ids(tasks: List[Dict[str, Any]]) -> List[int]:
    task_ids = []

    for task in tasks:
        if not is_root_level_task(task):
            continue

        task_id = normalize_task_id(task.get("task_id"))
        if task_id is None:
            print(f"Warning: skipping root task without numeric task_id: {task!r}")
            continue

        task_ids.append(task_id)

    return task_ids


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
        client = TimeCampClient(api_token)
        task_ids = args.task_ids

        if task_ids is None:
            tasks = client.get_tasks()
            task_ids = root_level_task_ids(tasks)

            if not task_ids:
                print("No root-level tasks found.")
                return 0

            root_task_names = {
                normalize_task_id(task.get("task_id")): task_name(task)
                for task in tasks
                if is_root_level_task(task)
            }
            print("Selected root-level tasks:")
            for task_id in task_ids:
                print(f"  - {task_id}: {root_task_names.get(task_id, '(unnamed task)')}")
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    print(
        f"{'Would assign' if args.dry_run else 'Assigning'} "
        f"user(s) {args.user_ids} to {len(task_ids)} task(s) with role {args.role_id}."
    )

    if args.dry_run:
        print("Dry run complete.")
        return 0

    failed = 0
    for task_id in task_ids:
        try:
            response = client.assign_users(
                task_id=task_id,
                user_ids=args.user_ids,
                role_id=args.role_id,
            )
            print(f"  - {task_id}: assigned successfully: {response}")
        except Exception as exc:
            failed += 1
            print(f"  - {task_id}: error: {exc}")

    if failed:
        print(f"User assignment complete with {failed} failure(s).")
        return 1

    print("Users assigned successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
