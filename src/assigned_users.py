from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .timecamp_client import TimeCampClient


DEFAULT_ASSIGN_ROLE_ID = 3


@dataclass
class AssignedUserSyncResult:
    users_by_email: Dict[str, int]
    users_by_username: Dict[str, int]


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
    assigned_users = get_task_assigned_users(source_task)
    user_ids: List[int] = []

    for user in assigned_users.values():
        user_id, _match_type = resolve_timecamp_user_id(
            user_sync_result.users_by_email,
            user_sync_result.users_by_username,
            user,
        )
        if user_id is not None:
            user_ids.append(user_id)

    user_ids = list(dict.fromkeys(user_ids))
    if not user_ids:
        return 0

    client.assign_users_to_task(
        task_id=timecamp_task_id,
        user_ids=user_ids,
        role_id=role_id,
    )
    return len(user_ids)


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
