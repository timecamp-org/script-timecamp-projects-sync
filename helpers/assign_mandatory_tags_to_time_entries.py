from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
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
    replaced: int = 0
    kept_other_tags: int = 0
    locked: int = 0
    failed: int = 0
    changes: Counter = field(default_factory=Counter)
    change_seconds: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class EntryTagChange:
    tags_to_add: Tuple[RequiredTag, ...]
    tag_ids_to_remove: Tuple[int, ...]
    removed_tag_names: Tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.tags_to_add and not self.tag_ids_to_remove


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
    parser.add_argument(
        "--replace-mismatched",
        action="store_true",
        help=(
            "Also replace tags from a mandatory tag list that differ from the "
            "required tags. Without it, entries that already have any tag from "
            "the list are left as they are."
        ),
    )
    parser.add_argument(
        "--replaceable-tags",
        help=(
            "With --replace-mismatched: comma-separated tag names that may be "
            "replaced, for example placeholder values. An entry that has any "
            "other mismatched tag from the list is left as it is. Without this "
            "option every mismatched tag is replaced."
        ),
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
        # Archived tasks keep their time entries, so their entries need tags too.
        timecamp_tasks = client.get_tasks(include_archived=True)
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
        replace_mismatched=args.replace_mismatched,
        replaceable_tag_names=parse_tag_name_list(args.replaceable_tags),
    )

    print("\nSummary:")
    print(f"- Scanned: {summary.scanned}")
    print(f"- Skipped without task_id: {summary.no_task}")
    print(
        "- Entries whose task has no resolved mandatory tags from "
        f"{args.tasks_file}: {summary.no_resolved_mandatory_tags_for_task}"
    )
    print(f"- Already had required tag(s): {summary.already_tagged}")
    print(f"- {'Would update' if args.dry_run else 'Updated'}: {summary.updated}")
    if args.replace_mismatched:
        print(
            f"- {'Would replace' if args.dry_run else 'Replaced'} mismatched tags on: "
            f"{summary.replaced}"
        )
    if summary.kept_other_tags:
        print(
            "- Kept entries with a mismatched tag that is not replaceable: "
            f"{summary.kept_other_tags}"
        )
    if summary.locked:
        print(f"- Skipped locked entries that need a change: {summary.locked}")
    print_change_summary(summary, dry_run=args.dry_run)
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
    timecamp_tasks_by_external_id: Dict[str, List[Dict[str, Any]]] = {}
    for task in timecamp_tasks:
        external_task_id = str(task.get("external_task_id") or "")
        if not external_task_id:
            continue
        if (
            external_task_id.startswith("sync_")
            or external_task_id in source_external_ids
        ):
            timecamp_tasks_by_external_id.setdefault(external_task_id, []).append(task)

    required_tags_by_task_id: Dict[int, List[RequiredTag]] = {}
    missing_timecamp_tasks: List[str] = []
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
        timecamp_task_ids = [
            task_id
            for task_id in (
                normalize_int(timecamp_task.get("task_id"))
                for timecamp_task in timecamp_tasks_by_external_id.get(external_task_id, [])
            )
            if task_id is not None
        ]
        if not timecamp_task_ids:
            missing_timecamp_tasks.append(
                f"{source_task.get('name', '(unnamed task)')} "
                f"(external_task_id={external_task_id})"
            )
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
            for timecamp_task_id in timecamp_task_ids:
                required_tags_by_task_id[timecamp_task_id] = required_tags

    if missing_timecamp_tasks:
        print(
            "Warning: no TimeCamp task found for "
            f"{len(missing_timecamp_tasks)} source task(s) with mandatory tags, e.g. "
            + "; ".join(missing_timecamp_tasks[:5])
        )

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
    replace_mismatched: bool = False,
    replaceable_tag_names: Optional[List[str]] = None,
) -> Summary:
    summary = Summary()
    replaceable_keys = (
        {normalize_key(name) for name in replaceable_tag_names}
        if replaceable_tag_names
        else None
    )

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

        change = plan_entry_tag_change(
            entry_tags,
            required_tags,
            tag_lists_by_id,
            replace_mismatched=replace_mismatched,
        )
        if change.is_empty:
            summary.already_tagged += 1
            continue

        if replaceable_keys is not None and any(
            normalize_key(name) not in replaceable_keys
            for name in change.removed_tag_names
        ):
            summary.kept_other_tags += 1
            continue

        if is_truthy(entry.get("locked")):
            summary.locked += 1
            print(f"  - Entry {entry_id}: locked, skipped {describe_change(change)}")
            continue

        action = "Would change" if dry_run else "Changing"
        print(f"  - Entry {entry_id}: {action} {describe_change(change)}")
        record_change(summary, entry, change)

        if dry_run:
            count_change(summary, change)
            continue

        try:
            if change.tags_to_add:
                added_tag_ids = client.add_tags_to_entry(
                    entry_id,
                    [tag.tag_id for tag in change.tags_to_add],
                )
                not_added = get_not_added_tag_ids(change, added_tag_ids)
                if not_added:
                    # TimeCamp silently skips tags the entry's task does not allow.
                    # Keep the old tag so the entry is never left without one.
                    summary.failed += 1
                    print(
                        "    Error: TimeCamp did not add tag(s) "
                        f"{', '.join(str(tag_id) for tag_id in not_added)}; "
                        "is the tag allowed on the task? Old tags kept."
                    )
                    continue
            # Remove only after the new tag is on the entry.
            if change.tag_ids_to_remove:
                client.remove_tags_from_entry(entry_id, list(change.tag_ids_to_remove))
            count_change(summary, change)
        except Exception as exc:
            summary.failed += 1
            print(f"    Error: {exc}")

    return summary


def parse_tag_name_list(raw_value: Optional[str]) -> Optional[List[str]]:
    if raw_value is None:
        return None

    return [name.strip() for name in raw_value.split(",") if name.strip()]


def get_not_added_tag_ids(change: EntryTagChange, added_tag_ids: Any) -> List[int]:
    """Return requested tag ids missing from the add-tags response."""
    if not isinstance(added_tag_ids, list):
        return [tag.tag_id for tag in change.tags_to_add]

    confirmed_ids = {normalize_int(tag_id) for tag_id in added_tag_ids}
    return [
        tag.tag_id
        for tag in change.tags_to_add
        if tag.tag_id not in confirmed_ids
    ]


def plan_entry_tag_change(
    entry_tags: List[Dict[str, Any]],
    required_tags: List[RequiredTag],
    tag_lists_by_id: Dict[int, str],
    replace_mismatched: bool = False,
) -> EntryTagChange:
    """Return the tags to add, and with replace_mismatched the tags to remove."""
    if not replace_mismatched:
        return EntryTagChange(
            tags_to_add=tuple(
                required_tag
                for required_tag in required_tags
                if not has_tag_from_list(entry_tags, required_tag, tag_lists_by_id)
            ),
            tag_ids_to_remove=(),
            removed_tag_names=(),
        )

    entry_tag_ids = {
        tag_id
        for tag_id in (get_entry_tag_id(tag) for tag in entry_tags)
        if tag_id is not None
    }
    required_tag_ids = {required_tag.tag_id for required_tag in required_tags}
    tags_to_add = tuple(
        required_tag
        for required_tag in required_tags
        if required_tag.tag_id not in entry_tag_ids
    )

    tag_ids_to_remove: List[int] = []
    removed_tag_names: List[str] = []
    for tag in entry_tags:
        tag_id = get_entry_tag_id(tag)
        if tag_id is None or tag_id in required_tag_ids or tag_id in tag_ids_to_remove:
            continue
        if any(
            is_tag_from_list(tag, required_tag, tag_lists_by_id)
            for required_tag in required_tags
        ):
            tag_ids_to_remove.append(tag_id)
            removed_tag_names.append(str(tag.get("name") or tag_id))

    return EntryTagChange(
        tags_to_add=tags_to_add,
        tag_ids_to_remove=tuple(tag_ids_to_remove),
        removed_tag_names=tuple(removed_tag_names),
    )


def describe_change(change: EntryTagChange) -> str:
    parts = []
    if change.tags_to_add:
        parts.append(
            "add "
            + ", ".join(
                f"{tag.tag_list_name}={tag.tag_name} ({tag.tag_id})"
                for tag in change.tags_to_add
            )
        )
    if change.tag_ids_to_remove:
        parts.append(
            "remove "
            + ", ".join(
                f"{name} ({tag_id})"
                for name, tag_id in zip(
                    change.removed_tag_names,
                    change.tag_ids_to_remove,
                    strict=True,
                )
            )
        )
    return "; ".join(parts)


def record_change(summary: Summary, entry: Dict[str, Any], change: EntryTagChange) -> None:
    key = (
        ", ".join(change.removed_tag_names) or "(none)",
        ", ".join(tag.tag_name for tag in change.tags_to_add) or "(none)",
    )
    summary.changes[key] += 1
    summary.change_seconds[key] += normalize_int(entry.get("duration")) or 0


def count_change(summary: Summary, change: EntryTagChange) -> None:
    if change.tag_ids_to_remove:
        summary.replaced += 1
    else:
        summary.updated += 1


def print_change_summary(summary: Summary, dry_run: bool, limit: int = 40) -> None:
    if not summary.changes:
        return

    print(f"- {'Planned' if dry_run else 'Applied'} changes (removed -> added):")
    for (removed, added), count in sorted(
        summary.changes.items(),
        key=lambda item: (-summary.change_seconds[item[0]], item[0]),
    )[:limit]:
        hours = summary.change_seconds[(removed, added)] / 3600
        print(f"  - {removed} -> {added}: {count} entries, {hours:.1f}h")
    if len(summary.changes) > limit:
        print(f"  - ... and {len(summary.changes) - limit} more change type(s)")


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
    return any(
        is_tag_from_list(tag, required_tag, tag_lists_by_id)
        for tag in entry_tags
    )


def is_tag_from_list(
    tag: Dict[str, Any],
    required_tag: RequiredTag,
    tag_lists_by_id: Dict[int, str],
) -> bool:
    tag_list_id = normalize_int(tag.get("tagListId") or tag.get("tag_list_id"))
    if tag_list_id == required_tag.tag_list_id:
        return True

    tag_list_name = (
        tag.get("tagListName")
        or tag.get("tag_list_name")
        or (tag_lists_by_id.get(tag_list_id) if tag_list_id is not None else None)
    )
    return normalize_key(tag_list_name) == normalize_key(required_tag.tag_list_name)


def get_entry_tag_id(tag: Dict[str, Any]) -> Optional[int]:
    return normalize_int(tag.get("tagId") or tag.get("tag_id") or tag.get("id"))


def is_truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


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
