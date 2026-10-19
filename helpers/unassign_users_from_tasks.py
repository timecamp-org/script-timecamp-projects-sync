import argparse
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Tuple


TIMECAMP_API_URL = "https://app.timecamp.com/third_party/api"
REQUEST_TIMEOUT = (10, 120)


def parse_task_ids(value: str) -> List[int]:
    try:
        task_ids = [int(part.strip()) for part in value.split(",")]
        if any(task_id <= 0 for task_id in task_ids):
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "task IDs must be comma-separated positive integers"
        ) from exc
    return list(dict.fromkeys(task_ids))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Unassign all directly assigned users from selected TimeCamp tasks "
            "and all their subtasks, including archived tasks. "
            "Collect all assignments before making any changes."
        )
    )
    parser.add_argument(
        "--task_ids",
        "--task-ids",
        required=True,
        type=parse_task_ids,
        help="Comma-separated task IDs, e.g. 34523534,34523535.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print all planned removals without updating TimeCamp.",
    )
    return parser.parse_args()


class TimeCampClient:
    def __init__(self, api_token: str):
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Missing dependency: requests. Run: pip install -r requirements.txt"
            ) from exc

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )
        self.session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=5,
                    backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET", "PUT"],
                    respect_retry_after_header=True,
                )
            ),
        )

    def get_tasks(self) -> List[Dict[str, Any]]:
        # /tasks returns the complete flat tree and each task's own users.
        # Fetch archived tasks too so no descendants are silently omitted.
        response = self.session.get(
            f"{TIMECAMP_API_URL}/tasks",
            params={"status": "all"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return list(data.values())
        if isinstance(data, list):
            return data
        raise ValueError(f"Unexpected TimeCamp tasks response: {type(data)}")

    def unassign_users_from_task(self, task_id: int, user_ids: List[int]) -> None:
        response = self.session.put(
            f"{TIMECAMP_API_URL}/v3/projects/{task_id}/unassign",
            json={"userIds": user_ids},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()


def task_user_ids(task: Dict[str, Any]) -> List[int]:
    raw_users = task.get("users")
    if isinstance(raw_users, dict):
        users = raw_users.items()
    elif isinstance(raw_users, list):
        users = ((None, user) for user in raw_users)
    else:
        raise ValueError(f"Missing or invalid users for task {task['task_id']}")

    user_ids = set()
    for fallback_id, user in users:
        if not isinstance(user, dict):
            raise ValueError(f"Invalid user assignment for task {task['task_id']}")
        user_id = int(user.get("user_id") or user.get("userId") or fallback_id)
        if user_id <= 0:
            raise ValueError(f"Invalid user ID for task {task['task_id']}")
        user_ids.add(user_id)
    return sorted(user_ids)


def build_unassignment_plan(
    tasks: List[Dict[str, Any]], task_ids: List[int]
) -> List[Tuple[int, List[int]]]:
    tasks_by_id = {}
    children = defaultdict(list)
    for task in tasks:
        task_id = int(task["task_id"])
        tasks_by_id[task_id] = task
        children[int(task.get("parent_id") or 0)].append(task_id)

    missing = sorted(set(task_ids) - tasks_by_id.keys())
    if missing:
        raise ValueError(f"Task IDs not found or inaccessible: {missing}")

    plan = []
    state = {}
    for root_id in task_ids:
        # Iterative postorder handles deep trees and removes parents last.
        stack = [(root_id, False)]
        while stack:
            task_id, exiting = stack.pop()
            if exiting:
                plan.append((task_id, task_user_ids(tasks_by_id[task_id])))
                state[task_id] = "done"
                continue
            if state.get(task_id) == "done":
                continue
            if state.get(task_id) == "visiting":
                raise ValueError(f"Cycle in task hierarchy at task {task_id}")
            state[task_id] = "visiting"
            stack.append((task_id, True))
            stack.extend((child_id, False) for child_id in reversed(children[task_id]))

    return plan


def unassign_users(client: TimeCampClient, task_ids: List[int], dry_run: bool) -> int:
    print("Fetching all tasks and assignments...", flush=True)
    try:
        plan = build_unassignment_plan(client.get_tasks(), task_ids)
    except Exception as exc:
        print(f"Error collecting assignments: {exc}. No assignments changed.")
        return 1

    total_assignments = sum(len(user_ids) for _, user_ids in plan)
    pending = [(task_id, user_ids) for task_id, user_ids in plan if user_ids]
    print(
        f"Collected {total_assignments} user assignment(s) across {len(plan)} task(s). "
        f"{len(pending)} task(s) have users to unassign.",
        flush=True,
    )

    removed = 0
    for index, (task_id, user_ids) in enumerate(pending, start=1):
        if dry_run:
            print(f"  [{index}/{len(pending)}] Task {task_id}: would unassign {user_ids}")
            continue
        try:
            client.unassign_users_from_task(task_id, user_ids)
        except Exception as exc:
            # Keep ancestor assignments intact so the failed subtree stays accessible.
            print(
                f"Error unassigning task {task_id}: {exc}. "
                f"Stopped after removing {removed}/{total_assignments} assignment(s). "
                "Rerun the helper to collect remaining assignments and retry.",
                flush=True,
            )
            return 1
        removed += len(user_ids)
        print(
            f"  [{index}/{len(pending)}] Task {task_id}: "
            f"unassigned {len(user_ids)} user(s).",
            flush=True,
        )

    print("Dry run complete." if dry_run else f"Removed {removed} user assignment(s).")
    return 0


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
        print("Error: TIMECAMP_API_TOKEN is not set.")
        return 1
    try:
        client = TimeCampClient(api_token)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 1
    try:
        return unassign_users(client, args.task_ids, args.dry_run)
    finally:
        client.session.close()


if __name__ == "__main__":
    sys.exit(main())
