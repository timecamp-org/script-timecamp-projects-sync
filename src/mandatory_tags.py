from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .timecamp_client import TimeCampClient


MANDATORY_TAG_KEYS = ("mandatory_tags", "meandatory_tags")


@dataclass(frozen=True)
class TagDefinition:
    tag_list_id: int
    tag_id: int


@dataclass
class MandatoryTagSyncResult:
    tags: Dict[Tuple[str, str], TagDefinition]
    created_tag_lists: int = 0
    created_tags: int = 0
    restored_tag_lists: int = 0
    restored_tags: int = 0

    def get(self, tag_list_name: str, tag_name: str) -> TagDefinition:
        return self.tags[(_normalize_key(tag_list_name), _normalize_key(tag_name))]


def get_task_mandatory_tags(task: Dict[str, Any]) -> Dict[str, List[str]]:
    raw_value = {}
    for key in MANDATORY_TAG_KEYS:
        if task.get(key):
            raw_value = task[key]
            break

    if not isinstance(raw_value, dict):
        return {}

    mandatory_tags: Dict[str, List[str]] = {}
    for raw_list_name, raw_tags in raw_value.items():
        list_name = str(raw_list_name).strip()
        tag_names = _normalize_tag_names(raw_tags)

        if list_name and tag_names:
            mandatory_tags[list_name] = tag_names

    return mandatory_tags


def collect_mandatory_tags(tasks: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    collected: Dict[str, List[str]] = {}

    for task in tasks:
        for tag_list_name, tag_names in get_task_mandatory_tags(task).items():
            existing_names = collected.setdefault(tag_list_name, [])
            for tag_name in tag_names:
                if _normalize_key(tag_name) not in {
                    _normalize_key(existing_name) for existing_name in existing_names
                }:
                    existing_names.append(tag_name)

    return collected


def ensure_mandatory_tags(
    client: TimeCampClient,
    tasks: Iterable[Dict[str, Any]],
) -> MandatoryTagSyncResult:
    required_tags = collect_mandatory_tags(tasks)
    result = MandatoryTagSyncResult(tags={})

    if not required_tags:
        return result

    tag_lists = _load_tag_lists_by_name(client)

    for tag_list_name, tag_names in required_tags.items():
        normalized_list_name = _normalize_key(tag_list_name)
        tag_list = tag_lists.get(normalized_list_name)

        if tag_list is None:
            tag_list_id = client.create_tag_list(tag_list_name)
            tag_list = {
                "id": tag_list_id,
                "name": tag_list_name,
                "archived": 0,
                "tags": {},
            }
            tag_lists[normalized_list_name] = tag_list
            result.created_tag_lists += 1
            print(f"Created TimeCamp tag list: {tag_list_name}")
        elif _is_archived(tag_list):
            client.update_tag_list(_object_id(tag_list), archived=0)
            tag_list["archived"] = 0
            result.restored_tag_lists += 1
            print(f"Restored archived TimeCamp tag list: {tag_list_name}")

        tag_list_id = _object_id(tag_list)
        tags_by_name = _load_tags_by_name(client, tag_list)

        for tag_name in tag_names:
            normalized_tag_name = _normalize_key(tag_name)
            tag = tags_by_name.get(normalized_tag_name)

            if tag is None:
                tag_id = client.create_tag(tag_list_id, tag_name)
                tag = {
                    "id": tag_id,
                    "name": tag_name,
                    "archived": 0,
                }
                tags_by_name[normalized_tag_name] = tag
                result.created_tags += 1
                print(f"Created TimeCamp tag: {tag_list_name} / {tag_name}")
            elif _is_archived(tag):
                client.update_tag(_object_id(tag), archived=0)
                tag["archived"] = 0
                result.restored_tags += 1
                print(f"Restored archived TimeCamp tag: {tag_list_name} / {tag_name}")

            result.tags[(normalized_list_name, normalized_tag_name)] = TagDefinition(
                tag_list_id=tag_list_id,
                tag_id=_object_id(tag),
            )

    return result


def assign_mandatory_tags_to_task(
    client: TimeCampClient,
    timecamp_task_id: Any,
    source_task: Dict[str, Any],
    tag_sync_result: MandatoryTagSyncResult,
    max_tags_to_add: Optional[int] = None,
) -> int:
    mandatory_tags = get_task_mandatory_tags(source_task)
    assigned_tags = 0
    current_assignments = client.get_task_tags(timecamp_task_id)
    assigned_tags_by_id = _assigned_tags_by_id(current_assignments)
    assigned_tag_ids = set(assigned_tags_by_id)
    pending_assignments = []
    tags_to_add_count = 0

    for tag_list_name, tag_names in mandatory_tags.items():
        tag_definitions = [
            tag_sync_result.get(tag_list_name, tag_name)
            for tag_name in tag_names
        ]
        if not tag_definitions:
            continue

        tags_payload = [
            {
                "tag_id": tag_definition.tag_id,
                "mandatory": True,
            }
            for tag_definition in tag_definitions
        ]
        tag_list_id = tag_definitions[0].tag_list_id
        tags_to_add = [
            tag for tag in tags_payload if int(tag["tag_id"]) not in assigned_tag_ids
        ]
        tags_to_update = [
            tag
            for tag in tags_payload
            if int(tag["tag_id"]) in assigned_tag_ids
            and not _is_truthy_flag(assigned_tags_by_id[int(tag["tag_id"])].get("mandatory"))
        ]
        tags_to_add_count += len(tags_to_add)
        pending_assignments.append((tag_list_id, tags_to_add, tags_to_update))

    if max_tags_to_add is not None and tags_to_add_count > max_tags_to_add:
        print(
            "Skipping mandatory tags for TimeCamp task "
            f"{timecamp_task_id}: {tags_to_add_count} tags would be added "
            f"(limit: {max_tags_to_add})"
        )
        return 0

    for tag_list_id, tags_to_add, tags_to_update in pending_assignments:
        if _has_direct_tag_list_assignment(current_assignments, tag_list_id):
            client.remove_tag_list_from_task(timecamp_task_id, tag_list_id)

        if tags_to_add:
            client.add_tags_to_task(timecamp_task_id, tags_to_add)
            assigned_tags += len(tags_to_add)

        if tags_to_update:
            client.update_task_tags(timecamp_task_id, tags_to_update)
            assigned_tags += len(tags_to_update)

    return assigned_tags


def _load_tag_lists_by_name(client: TimeCampClient) -> Dict[str, Dict[str, Any]]:
    tag_lists = client.get_tag_lists(include_tags=True)
    tag_lists_by_name: Dict[str, Dict[str, Any]] = {}

    for fallback_id, tag_list in tag_lists.items():
        if not isinstance(tag_list, dict):
            continue

        tag_list.setdefault("id", fallback_id)
        name = str(tag_list.get("name") or "").strip()
        if name:
            tag_lists_by_name[_normalize_key(name)] = tag_list

    return tag_lists_by_name


def _load_tags_by_name(
    client: TimeCampClient,
    tag_list: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    raw_tags = tag_list.get("tags")

    if not isinstance(raw_tags, dict):
        raw_tags = client.get_tag_list_tags(_object_id(tag_list))
        tag_list["tags"] = raw_tags

    tags_by_name: Dict[str, Dict[str, Any]] = {}
    for fallback_id, tag in raw_tags.items():
        if not isinstance(tag, dict):
            continue

        tag.setdefault("id", fallback_id)
        name = str(tag.get("name") or "").strip()
        if name:
            tags_by_name[_normalize_key(name)] = tag

    return tags_by_name


def _normalize_tag_names(raw_tags: Any) -> List[str]:
    if isinstance(raw_tags, str):
        raw_values = [raw_tags]
    elif isinstance(raw_tags, list):
        raw_values = raw_tags
    else:
        raw_values = [raw_tags]

    tag_names: List[str] = []
    seen = set()

    for raw_value in raw_values:
        tag_name = str(raw_value).strip()
        normalized_tag_name = _normalize_key(tag_name)

        if tag_name and normalized_tag_name not in seen:
            tag_names.append(tag_name)
            seen.add(normalized_tag_name)

    return tag_names


def _normalize_key(value: str) -> str:
    return value.strip().casefold()


def _object_id(value: Dict[str, Any]) -> int:
    return int(value.get("id") or value.get("tag_id") or value.get("tagListId"))


def _is_archived(value: Dict[str, Any]) -> bool:
    try:
        return int(value.get("archived", 0)) == 1
    except (TypeError, ValueError):
        return False


def _assigned_tags_by_id(assignments: Dict[str, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    tags_by_id: Dict[int, Dict[str, Any]] = {}

    for tag_list in assignments.values():
        for tag in tag_list.get("tags", []):
            if tag.get("inherit"):
                continue

            tag_id = tag.get("id") or tag.get("tag_id") or tag.get("tagId")
            if tag_id is not None:
                tags_by_id[int(tag_id)] = tag

    return tags_by_id


def _is_truthy_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    return bool(value)


def _has_direct_tag_list_assignment(
    assignments: Dict[str, Dict[str, Any]],
    tag_list_id: int,
) -> bool:
    tag_list = assignments.get(str(tag_list_id)) or assignments.get(tag_list_id)
    if not isinstance(tag_list, dict):
        return False

    return not tag_list.get("inherit") and not tag_list.get("hasAssignedTags")
