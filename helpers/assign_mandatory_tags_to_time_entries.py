from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


MANDATORY_TAG_KEYS = ("mandatory_tags", "meandatory_tags")


@dataclass(frozen=True)
class RequiredTag:
    tag_list_id: int
    tag_id: int
    tag_list_name: str
    tag_name: str


@dataclass
class Summary:
    scanned: int = 0
    no_task: int = 0
    no_resolved_mandatory_tags_for_task: int = 0
    already_tagged: int = 0
    updated: int = 0
    failed: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assign missing mandatory tags to TimeCamp time entries using "
            "meandatory_tags/mandatory_tags from tasks.json."
        ),
        epilog=(
            "Examples:\n"
            "  python3 helpers/assign_mandatory_tags_to_time_entries.py --from 2026-06-01 --to 2026-06-05 --dry-run\n"
            "  python3 helpers/assign_mandatory_tags_to_time_entries.py --from 2026-06-01 --to 2026-06-05"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        required=True,
        type=parse_date,
        help="Start date, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        required=True,
        type=parse_date,
        help="End date, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--tasks-file",
        default="tasks.json",
        help="Path to source tasks JSON file (default: tasks.json).",
    )
    parser.add_argument(
        "--dry-run",
        "--dry-mode",
        action="store_true",
        dest="dry_run",
        help="Print changes without updating TimeCamp.",
    )
    return parser.parse_args()


def parse_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def main() -> int:
    args = parse_args()
    if args.from_date > args.to_date:
        print("Error: from_date must be before or equal to to_date.")
        return 1

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
        source_tasks = load_tasks_from_json(args.tasks_file)
    except Exception as exc:
        print(f"Error loading {args.tasks_file}: {exc}")
        return 1

    from src.timecamp_client import TimeCampClient

    client = TimeCampClient(api_token)

    try:
        user_ids = load_user_ids(client)
        if not user_ids:
            print("Error: no TimeCamp users found; cannot scan all account users.")
            return 1

        print(f"Loaded {len(user_ids)} TimeCamp user(s).")
        timecamp_tasks = client.get_tasks()
        tag_definitions = load_tag_definitions(client, source_tasks, args.dry_run)
        required_tags_by_task_id = build_required_tags_by_timecamp_task_id(
            source_tasks=source_tasks,
            timecamp_tasks=timecamp_tasks,
            tag_definitions=tag_definitions,
        )
        tag_lists_by_id = load_tag_lists_by_id(client)

        entries = client.get_time_entries(
            args.from_date,
            args.to_date,
            user_ids=user_ids,
            opt_fields="tags",
        )
    except Exception as exc:
        print(f"Error loading TimeCamp data: {exc}")
        return 1

    print(
        f"{'Dry run: scanning' if args.dry_run else 'Scanning'} "
        f"{len(entries)} time entries from {args.from_date} to {args.to_date}."
    )

    summary = assign_missing_mandatory_tags(
        client=client,
        entries=entries,
        required_tags_by_task_id=required_tags_by_task_id,
        tag_lists_by_id=tag_lists_by_id,
        dry_run=args.dry_run,
    )

    print("\nSummary:")
    print(f"- Scanned: {summary.scanned}")
    print(f"- Skipped without task_id: {summary.no_task}")
    print(
        "- Entries whose task has no resolved mandatory tags from "
        f"{args.tasks_file}: {summary.no_resolved_mandatory_tags_for_task}"
    )
    print(f"- Already had required tag list(s): {summary.already_tagged}")
    print(f"- {'Would update' if args.dry_run else 'Updated'}: {summary.updated}")
    if summary.failed:
        print(f"- Failed: {summary.failed}")
        return 1

    return 0


def load_tasks_from_json(filename: str) -> List[Dict[str, Any]]:
    with open(filename, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("expected a JSON array of tasks")

    return [task for task in data if isinstance(task, dict)]


def load_user_ids(client: Any) -> List[int]:
    user_ids: List[int] = []
    for user in client.get_users():
        user_id = normalize_int(
            user.get("user_id") or user.get("id") or user.get("userId")
        )
        if user_id is not None:
            user_ids.append(user_id)

    return list(dict.fromkeys(user_ids))


def load_tag_definitions(
    client: Any,
    source_tasks: List[Dict[str, Any]],
    dry_run: bool,
) -> Dict[Tuple[str, str], Tuple[int, int]]:
    if not dry_run:
        from src.mandatory_tags import ensure_mandatory_tags

        sync_result = ensure_mandatory_tags(client, source_tasks)
        return {
            key: (tag_definition.tag_list_id, tag_definition.tag_id)
            for key, tag_definition in sync_result.tags.items()
        }

    return load_existing_tag_definitions(client)


def load_existing_tag_definitions(client: Any) -> Dict[Tuple[str, str], Tuple[int, int]]:
    tag_definitions: Dict[Tuple[str, str], Tuple[int, int]] = {}
    tag_lists = client.get_tag_lists(include_tags=True)

    for fallback_list_id, tag_list in tag_lists.items():
        if not isinstance(tag_list, dict) or is_archived(tag_list):
            continue

        tag_list_id = normalize_int(
            tag_list.get("id")
            or tag_list.get("tag_list_id")
            or tag_list.get("tagListId")
            or fallback_list_id
        )
        tag_list_name = str(tag_list.get("name") or "").strip()
        if tag_list_id is None or not tag_list_name:
            continue

        raw_tags = tag_list.get("tags")
        if not isinstance(raw_tags, dict):
            raw_tags = client.get_tag_list_tags(tag_list_id)

        for fallback_tag_id, tag in raw_tags.items():
            if not isinstance(tag, dict) or is_archived(tag):
                continue

            tag_id = normalize_int(
                tag.get("id") or tag.get("tag_id") or tag.get("tagId") or fallback_tag_id
            )
            tag_name = str(tag.get("name") or "").strip()
            if tag_id is None or not tag_name:
                continue

            tag_definitions[(normalize_key(tag_list_name), normalize_key(tag_name))] = (
                tag_list_id,
                tag_id,
            )

    return tag_definitions


def build_required_tags_by_timecamp_task_id(
    source_tasks: List[Dict[str, Any]],
    timecamp_tasks: List[Dict[str, Any]],
    tag_definitions: Dict[Tuple[str, str], Tuple[int, int]],
) -> Dict[int, List[RequiredTag]]:
    source_external_ids = {get_source_external_task_id(task) for task in source_tasks}
    timecamp_tasks_by_external_id = {
        str(task.get("external_task_id")): task
        for task in timecamp_tasks
        if task.get("external_task_id")
        and (
            str(task.get("external_task_id")).startswith("sync_")
            or str(task.get("external_task_id")) in source_external_ids
        )
    }
    required_tags_by_task_id: Dict[int, List[RequiredTag]] = {}
    source_tasks_by_task_id = {
        str(task.get("task_id")): task for task in source_tasks if task.get("task_id")
    }
    effective_tags_cache: Dict[str, Dict[str, List[str]]] = {}

    for source_task in source_tasks:
        mandatory_tags = get_effective_task_mandatory_tags(
            source_task=source_task,
            source_tasks_by_task_id=source_tasks_by_task_id,
            cache=effective_tags_cache,
        )
        if not mandatory_tags:
            continue

        external_task_id = get_source_external_task_id(source_task)
        timecamp_task = timecamp_tasks_by_external_id.get(external_task_id)
        if not timecamp_task:
            print(
                "Warning: TimeCamp task not found for "
                f"{source_task.get('name', '(unnamed task)')} "
                f"(external_task_id={external_task_id})"
            )
            continue

        timecamp_task_id = normalize_int(timecamp_task.get("task_id"))
        if timecamp_task_id is None:
            continue

        required_tags: List[RequiredTag] = []
        seen_tag_ids = set()
        for tag_list_name, tag_names in mandatory_tags.items():
            for tag_name in tag_names:
                tag_definition = tag_definitions.get(
                    (normalize_key(tag_list_name), normalize_key(tag_name))
                )
                if tag_definition is None:
                    print(f"Warning: tag not found: {tag_list_name} / {tag_name}")
                    continue

                tag_list_id, tag_id = tag_definition
                if tag_id in seen_tag_ids:
                    continue

                required_tags.append(
                    RequiredTag(
                        tag_list_id=tag_list_id,
                        tag_id=tag_id,
                        tag_list_name=tag_list_name,
                        tag_name=tag_name,
                    )
                )
                seen_tag_ids.add(tag_id)

        if required_tags:
            required_tags_by_task_id[timecamp_task_id] = required_tags

    print(
        "Resolved mandatory tags from tasks.json for "
        f"{len(required_tags_by_task_id)} TimeCamp task(s)."
    )
    return required_tags_by_task_id


def get_effective_task_mandatory_tags(
    source_task: Dict[str, Any],
    source_tasks_by_task_id: Dict[str, Dict[str, Any]],
    cache: Dict[str, Dict[str, List[str]]],
    stack: Optional[set] = None,
) -> Dict[str, List[str]]:
    task_id = str(source_task.get("task_id") or "")
    if not task_id:
        return get_task_mandatory_tags(source_task)

    if task_id in cache:
        return cache[task_id]

    if stack is None:
        stack = set()
    if task_id in stack:
        print(f"Warning: cycle detected while resolving mandatory tags for {task_id}")
        return {}

    stack.add(task_id)
    effective_tags: Dict[str, List[str]] = {}
    parent_id = source_task.get("parent_id")
    parent_task = (
        source_tasks_by_task_id.get(str(parent_id))
        if parent_id not in (None, "", 0, "0")
        else None
    )

    if parent_task:
        effective_tags.update(
            get_effective_task_mandatory_tags(
                source_task=parent_task,
                source_tasks_by_task_id=source_tasks_by_task_id,
                cache=cache,
                stack=stack,
            )
        )

    for tag_list_name, tag_names in get_task_mandatory_tags(source_task).items():
        effective_tags[tag_list_name] = tag_names

    stack.remove(task_id)
    cache[task_id] = effective_tags
    return effective_tags


def get_source_external_task_id(task: Dict[str, Any]) -> str:
    if task.get("external_task_id"):
        return str(task["external_task_id"])

    task_id = str(task["task_id"])
    if task_id.startswith("monday_"):
        return task_id

    return f"sync_{task_id}"


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
        tag_list_name = str(raw_list_name).strip()
        tag_names = normalize_tag_names(raw_tags)
        if tag_list_name and tag_names:
            mandatory_tags[tag_list_name] = tag_names

    return mandatory_tags


def normalize_tag_names(raw_tags: Any) -> List[str]:
    raw_values = raw_tags if isinstance(raw_tags, list) else [raw_tags]
    tag_names: List[str] = []
    seen = set()

    for raw_value in raw_values:
        tag_name = str(raw_value).strip()
        normalized_tag_name = normalize_key(tag_name)
        if tag_name and normalized_tag_name not in seen:
            tag_names.append(tag_name)
            seen.add(normalized_tag_name)

    return tag_names


def load_tag_lists_by_id(client: Any) -> Dict[int, str]:
    tag_lists_by_id: Dict[int, str] = {}
    for fallback_id, tag_list in client.get_tag_lists(include_tags=False).items():
        if not isinstance(tag_list, dict):
            continue

        tag_list_id = normalize_int(
            tag_list.get("id") or tag_list.get("tag_list_id") or fallback_id
        )
        tag_list_name = str(tag_list.get("name") or "").strip()
        if tag_list_id is not None and tag_list_name:
            tag_lists_by_id[tag_list_id] = tag_list_name

    return tag_lists_by_id


def assign_missing_mandatory_tags(
    client: Any,
    entries: Iterable[Dict[str, Any]],
    required_tags_by_task_id: Dict[int, List[RequiredTag]],
    tag_lists_by_id: Dict[int, str],
    dry_run: bool,
) -> Summary:
    summary = Summary()

    for entry in entries:
        summary.scanned += 1
        entry_id = entry.get("id")
        task_id = normalize_int(entry.get("task_id"))

        if task_id is None or task_id == 0:
            summary.no_task += 1
            continue

        required_tags = required_tags_by_task_id.get(task_id)
        if not required_tags:
            summary.no_resolved_mandatory_tags_for_task += 1
            continue

        entry_tags = entry.get("tags")
        if not isinstance(entry_tags, list):
            entry_tags = load_entry_tags(client, entry_id)

        missing_tags = [
            required_tag
            for required_tag in required_tags
            if not has_tag_from_list(entry_tags, required_tag, tag_lists_by_id)
        ]
        if not missing_tags:
            summary.already_tagged += 1
            continue

        action = "Would assign" if dry_run else "Assigning"
        readable_tags = ", ".join(
            f"{tag.tag_list_name}={tag.tag_name} ({tag.tag_id})"
            for tag in missing_tags
        )
        print(f"  - Entry {entry_id}: {action} {readable_tags}")

        if dry_run:
            summary.updated += 1
            continue

        try:
            client.add_tags_to_entry(entry_id, [tag.tag_id for tag in missing_tags])
            summary.updated += 1
        except Exception as exc:
            summary.failed += 1
            print(f"    Error: {exc}")

    return summary


def load_entry_tags(client: Any, entry_id: Any) -> List[Dict[str, Any]]:
    entry_tags = client.get_entry_tags(entry_id)
    tags = entry_tags.get(str(entry_id)) or entry_tags.get(entry_id) or []
    if isinstance(tags, list):
        return tags
    return []


def has_tag_from_list(
    entry_tags: List[Dict[str, Any]],
    required_tag: RequiredTag,
    tag_lists_by_id: Dict[int, str],
) -> bool:
    required_tag_list_key = normalize_key(required_tag.tag_list_name)

    for tag in entry_tags:
        tag_list_id = normalize_int(tag.get("tagListId") or tag.get("tag_list_id"))
        if tag_list_id == required_tag.tag_list_id:
            return True

        tag_list_name = (
            tag.get("tagListName")
            or tag.get("tag_list_name")
            or (tag_lists_by_id.get(tag_list_id) if tag_list_id is not None else None)
        )
        if normalize_key(tag_list_name) == required_tag_list_key:
            return True

    return False


def normalize_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def is_archived(value: Dict[str, Any]) -> bool:
    try:
        return int(value.get("archived", 0)) == 1
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(main())
