from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .timecamp_client import TimeCampClient


DEFAULT_ASSIGN_ROLE_ID = 3


@dataclass
class AssignedUserSyncResult:
    users_by_email: Dict[str, int]
    users_by_username: Dict[str, int]


@dataclass
class TaskUserSyncResult:
    assigned: int = 0
    unassigned: int = 0


def get_task_assigned_users(task: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    raw_value = task.get("assigned_users")
    if not isinstance(raw_value, dict):
        return {}

    assigned_users: Dict[str, Dict[str, str]] = {}
    for external_user_id, user_data in raw_value.items():
        if not isinstance(user_data, dict):
            continue

        email = str(user_data.get("email") or "").strip().casefold()
        username = _normalize_name(user_data.get("username"))
        if not email and not username:
            continue

        assigned_users[str(external_user_id)] = {
            "email": email,
            "username": username,
        }

    return assigned_users


def build_assigned_user_sync_result(
    client: TimeCampClient,
    tasks: Iterable[Dict[str, Any]],
) -> AssignedUserSyncResult:
    users_by_email, users_by_username = _load_timecamp_users(client)

    for task in tasks:
        for user in get_task_assigned_users(task).values():
            user_id, match_type = resolve_timecamp_user_id(
                users_by_email,
                users_by_username,
                user,
            )
            if user_id is not None:
                continue

            email = user.get("email") or "(no email)"
            username = user.get("username") or "(no username)"
            print(
                "Warning: TimeCamp user not found for "
                f"email={email}, username={username}"
            )

    return AssignedUserSyncResult(
        users_by_email=users_by_email,
        users_by_username=users_by_username,
    )


def assign_users_to_task(
    client: TimeCampClient,
    timecamp_task_id: Any,
    source_task: Dict[str, Any],
    user_sync_result: AssignedUserSyncResult,
    role_id: int = DEFAULT_ASSIGN_ROLE_ID,
) -> int:
    return sync_users_to_task(
        client=client,
        timecamp_task_id=timecamp_task_id,
        source_task=source_task,
        user_sync_result=user_sync_result,
        role_id=role_id,
    ).assigned


def sync_users_to_task(
    client: TimeCampClient,
    timecamp_task_id: Any,
    source_task: Dict[str, Any],
    user_sync_result: AssignedUserSyncResult,
    role_id: int = DEFAULT_ASSIGN_ROLE_ID,
    strict: bool = False,
    current_assigned_users: Optional[Any] = None,
) -> TaskUserSyncResult:
    assigned_users = get_task_assigned_users(source_task)
    user_ids: List[int] = []
    unresolved_user_count = 0

    for user in assigned_users.values():
        user_id, _match_type = resolve_timecamp_user_id(
            user_sync_result.users_by_email,
            user_sync_result.users_by_username,
            user,
        )
        if user_id is not None:
            user_ids.append(user_id)
        else:
            unresolved_user_count += 1

    user_ids = list(dict.fromkeys(user_ids))
    desired_user_ids = set(user_ids)
    current_direct_user_ids: Optional[set[int]] = None
    if current_assigned_users is not None:
        current_direct_user_ids = _task_users_map_user_ids(current_assigned_users)

    assigned_count = 0
    if current_direct_user_ids is None:
        user_ids_to_assign = user_ids
    else:
        user_ids_to_assign = [
            user_id for user_id in user_ids if user_id not in current_direct_user_ids
        ]

    if user_ids_to_assign:
        client.assign_users_to_task(
            task_id=timecamp_task_id,
            user_ids=user_ids_to_assign,
            role_id=role_id,
        )
        assigned_count = len(user_ids_to_assign)

    unassigned_count = 0
    if strict and unresolved_user_count == 0:
        if current_direct_user_ids is None:
            current_direct_user_ids = _direct_assigned_user_ids(
                client.get_project_assigned_users(timecamp_task_id),
                timecamp_task_id,
            )
        user_ids_to_unassign = sorted(current_direct_user_ids - desired_user_ids)
        if user_ids_to_unassign:
            client.unassign_users_from_task(
                task_id=timecamp_task_id,
                user_ids=user_ids_to_unassign,
            )
            unassigned_count = len(user_ids_to_unassign)

    return TaskUserSyncResult(
        assigned=assigned_count,
        unassigned=unassigned_count,
    )


def resolve_timecamp_user_id(
    users_by_email: Dict[str, int],
    users_by_username: Dict[str, int],
    user: Dict[str, str],
) -> Tuple[Optional[int], Optional[str]]:
    email = user.get("email")
    if email:
        user_id = users_by_email.get(email)
        if user_id is not None:
            return user_id, "email"

    username = user.get("username")
    if username:
        user_id = users_by_username.get(username)
        if user_id is not None:
            return user_id, "username"

    return None, None


def _load_timecamp_users(
    client: TimeCampClient,
) -> Tuple[Dict[str, int], Dict[str, int]]:
    users_by_email: Dict[str, int] = {}
    users_by_username: Dict[str, int] = {}

    for user in client.get_users():
        user_id = _user_id(user)
        if user_id is None:
            continue

        email = str(user.get("email") or "").strip().casefold()
        if email:
            users_by_email[email] = user_id

        for name_field in ("display_name", "name", "username"):
            username = _normalize_name(user.get(name_field))
            if username:
                users_by_username[username] = user_id

    return users_by_email, users_by_username


def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _direct_assigned_user_ids(
    assigned_users: List[Dict[str, Any]],
    timecamp_task_id: Any,
) -> set[int]:
    task_id = _int_or_none(timecamp_task_id)
    if task_id is None:
        return set()

    user_ids: set[int] = set()
    for assigned_user in assigned_users:
        assigned_task_id = _int_or_none(
            assigned_user.get("taskId") or assigned_user.get("task_id")
        )
        if assigned_task_id != task_id:
            continue

        user_id = _user_id(assigned_user)
        if user_id is not None:
            user_ids.add(user_id)

    return user_ids


def _task_users_map_user_ids(raw_users: Any) -> set[int]:
    user_ids: set[int] = set()

    if isinstance(raw_users, dict):
        user_items = raw_users.items()
        for fallback_user_id, user in user_items:
            user_id = None
            if isinstance(user, dict):
                user_id = _user_id(user)
            if user_id is None:
                user_id = _int_or_none(fallback_user_id)
            if user_id is not None:
                user_ids.add(user_id)
        return user_ids

    if isinstance(raw_users, list):
        for user in raw_users:
            if not isinstance(user, dict):
                continue
            user_id = _user_id(user)
            if user_id is not None:
                user_ids.add(user_id)

    return user_ids


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _user_id(user: Dict[str, Any]) -> Optional[int]:
    for key in ("user_id", "id", "userId"):
        value = user.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None
