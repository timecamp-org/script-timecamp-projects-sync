import argparse
import os
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

from src.assigned_users import (
    build_assigned_user_sync_result,
    get_task_assigned_users,
    sync_users_to_task,
)
from src.mandatory_tags import (
    ensure_mandatory_tags,
    get_desired_mandatory_tag_assignments,
    get_task_mandatory_tags,
    sync_mandatory_tags_to_task,
)
from src.timecamp_client import (
    TimeCampClient,
    TimeCampRateLimitError,
    normalize_timecamp_task_name,
)

# Load environment variables
if load_dotenv:
    load_dotenv(override=True)

TIMECAMP_API_TOKEN = os.getenv('TIMECAMP_API_TOKEN')
TIMECAMP_TASK_ID = os.getenv('TIMECAMP_TASK_ID')
TIMECAMP_SYNC_ACTIONS = os.getenv('TIMECAMP_SYNC_ACTIONS')
TIMECAMP_SYNC_EXTERNAL_ID_PREFIX = os.getenv('TIMECAMP_SYNC_EXTERNAL_ID_PREFIX')
TIMECAMP_STRICT_USER_SYNC = os.getenv('TIMECAMP_STRICT_USER_SYNC')
TIMECAMP_MAX_MANDATORY_TAGS_TO_ADD = os.getenv('TIMECAMP_MAX_MANDATORY_TAGS_TO_ADD')
TIMECAMP_MANDATORY_TAG_CACHE_FILE = os.getenv(
    'TIMECAMP_MANDATORY_TAG_CACHE_FILE',
    'data/timecamp_mandatory_tag_cache.json',
)

DEFAULT_TASKS_FILE = "tasks.json"
DEFAULT_HIERARCHY_PREVIEW_LIMIT = 50
TASK_PROGRESS_LOG_INTERVAL = 100
DEFAULT_SYNC_ACTIONS = {
    "tasks",
    "names",
    "estimates",
    "archive",
    "tags",
    "mandatory_tags",
    "users",
}
SYNC_ACTION_ORDER = (
    "tasks",
    "names",
    "estimates",
    "tags",
    "mandatory_tags",
    "users",
    "archive",
)
SYNC_ACTION_DESCRIPTIONS = {
    "tasks": "Create missing TimeCamp tasks",
    "names": "Update changed TimeCamp task names",
    "estimates": "Sync source estimates to TimeCamp task hour budgets",
    "tags": "Create or restore mandatory tag lists and tags",
    "mandatory_tags": "Assign mandatory tags to TimeCamp tasks",
    "users": "Assign users to TimeCamp tasks",
    "archive": "Archive TimeCamp tasks missing from source data",
}
SYNC_ACTION_ALIASES = {
    "create_tasks": "tasks",
    "task_creation": "tasks",
    "name": "names",
    "task_names": "names",
    "rename_tasks": "names",
    "estimate": "estimates",
    "task_estimates": "estimates",
    "budgets": "estimates",
    "archive_tasks": "archive",
    "tag": "tags",
    "meandatory_tags": "mandatory_tags",
    "mandatory_tag_assignments": "mandatory_tags",
    "user": "users",
    "assigned_users": "users",
    "user_assignments": "users",
}


def get_enabled_sync_actions():
    """Return enabled sync action names from TIMECAMP_SYNC_ACTIONS."""
    if not TIMECAMP_SYNC_ACTIONS:
        return set(DEFAULT_SYNC_ACTIONS)

    enabled_actions = set()
    unknown_actions = []

    for raw_action in TIMECAMP_SYNC_ACTIONS.split(","):
        action = raw_action.strip().casefold().replace("-", "_")
        if not action:
            continue

        action = SYNC_ACTION_ALIASES.get(action, action)
        if action not in DEFAULT_SYNC_ACTIONS:
            unknown_actions.append(raw_action.strip())
            continue

        enabled_actions.add(action)

    if unknown_actions:
        print(
            "Warning: ignoring unknown TIMECAMP_SYNC_ACTIONS value(s): "
            f"{', '.join(unknown_actions)}"
        )

    return enabled_actions


def get_optional_env_int(value):
    if value is None:
        return None

    normalized_value = value.strip()
    if not normalized_value:
        return None

    try:
        parsed_value = int(normalized_value)
    except ValueError:
        print(f"Warning: invalid integer env value '{value}', ignoring limit")
        return None

    if parsed_value < 0:
        print(f"Warning: negative integer env value '{value}', ignoring limit")
        return None

    return parsed_value


def get_strict_user_sync_enabled(cli_enabled=False):
    if cli_enabled:
        return True

    if TIMECAMP_STRICT_USER_SYNC is None:
        return False

    value = TIMECAMP_STRICT_USER_SYNC.strip().casefold()
    if not value:
        return False

    if value in {"1", "true", "yes", "on"}:
        return True

    if value in {"0", "false", "no", "off"}:
        return False

    print(
        "Warning: invalid TIMECAMP_STRICT_USER_SYNC value "
        f"'{TIMECAMP_STRICT_USER_SYNC}', treating as disabled"
    )
    return False


def ordered_sync_actions(actions):
    return [action for action in SYNC_ACTION_ORDER if action in actions]


def print_sync_action_plan(enabled_actions):
    omitted_actions = DEFAULT_SYNC_ACTIONS - enabled_actions

    print("\nSync action plan:")
    print("Will run:")
    if enabled_actions:
        for action in ordered_sync_actions(enabled_actions):
            print(f"  - {SYNC_ACTION_DESCRIPTIONS[action]}")
    else:
        print("  - (none)")

    print("Will skip:")
    if omitted_actions:
        for action in ordered_sync_actions(omitted_actions):
            print(f"  - {SYNC_ACTION_DESCRIPTIONS[action]}")
    else:
        print("  - (none)")


def stop_on_rate_limit(exc):
    print(f"Stopping sync because TimeCamp returned 429 Too Many Requests: {exc}")
    raise SystemExit(1)


def get_api_metrics_snapshot(client):
    snapshot = getattr(client, "get_api_metrics_snapshot", None)
    if not callable(snapshot):
        return {"counts": {}, "seconds": {}}

    metrics = snapshot()
    if not isinstance(metrics, dict):
        return {"counts": {}, "seconds": {}}

    return {
        "counts": dict(metrics.get("counts") or {}),
        "seconds": dict(metrics.get("seconds") or {}),
    }


def api_metrics_delta(start_metrics, end_metrics):
    start_counts = start_metrics.get("counts", {})
    end_counts = end_metrics.get("counts", {})
    start_seconds = start_metrics.get("seconds", {})
    end_seconds = end_metrics.get("seconds", {})
    keys = set(start_counts) | set(end_counts) | set(start_seconds) | set(end_seconds)
    counts = {}
    seconds = {}

    for key in keys:
        count_delta = int(end_counts.get(key, 0)) - int(start_counts.get(key, 0))
        seconds_delta = float(end_seconds.get(key, 0.0)) - float(start_seconds.get(key, 0.0))
        if count_delta > 0:
            counts[key] = count_delta
            seconds[key] = max(0.0, seconds_delta)

    return {"counts": counts, "seconds": seconds}


def api_call_count_delta(start_metrics, end_metrics):
    delta = api_metrics_delta(start_metrics, end_metrics)
    return sum(delta["counts"].values())


def format_seconds(seconds):
    return f"{max(0.0, seconds):.2f}s"


def load_mandatory_tag_assignment_cache(cache_file):
    if not cache_file:
        return {"version": 1, "tasks": {}}

    path = Path(cache_file)
    if not path.exists():
        return {"version": 1, "tasks": {}}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read mandatory tag cache {path}: {exc}")
        return {"version": 1, "tasks": {}}

    if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
        print(f"Warning: ignoring invalid mandatory tag cache {path}")
        return {"version": 1, "tasks": {}}

    return {
        "version": 1,
        "tasks": dict(data["tasks"]),
    }


def save_mandatory_tag_assignment_cache(cache_file, cache):
    if not cache_file:
        return

    path = Path(cache_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": cache.get("tasks", {}),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def mandatory_tag_cache_entry_matches(
    cache,
    external_id,
    timecamp_task,
    desired_tag_assignments,
):
    entry = cache.get("tasks", {}).get(external_id)
    if not isinstance(entry, dict):
        return False

    return (
        str(entry.get("timecamp_task_id")) == str(timecamp_task.get("task_id"))
        and str(entry.get("timecamp_modify_time") or "")
        == str(timecamp_task.get("modify_time") or "")
        and entry.get("desired_tag_assignments") == desired_tag_assignments
    )


def record_mandatory_tag_cache_entry(
    cache,
    external_id,
    timecamp_task,
    desired_tag_assignments,
):
    cache.setdefault("tasks", {})[external_id] = {
        "timecamp_task_id": str(timecamp_task.get("task_id")),
        "timecamp_modify_time": str(timecamp_task.get("modify_time") or ""),
        "desired_tag_assignments": desired_tag_assignments,
    }


def load_internal_project_tag_assignments(client, parent_id):
    assignments_by_task_id = {}
    page = 1

    while True:
        response = client.get_internal_projects(
            parent_id=parent_id,
            status="active",
            include=["tags"],
            page=page,
        )
        projects = response.get("data", [])
        if not isinstance(projects, list):
            raise ValueError(
                "Unexpected TimeCamp internal projects data: "
                f"{type(projects)}"
            )

        for project in projects:
            if not isinstance(project, dict):
                continue

            task_id = project.get("taskId") or project.get("task_id") or project.get("id")
            if task_id is None:
                continue

            assignments_by_task_id[str(task_id)] = internal_project_tag_assignments(
                project
            )

        pagination = response.get("pagination", {})
        if not isinstance(pagination, dict):
            break

        total_pages = _int_or_none(pagination.get("totalPages")) or page
        if page >= total_pages:
            break

        page += 1

    return assignments_by_task_id


def internal_project_tag_assignments(project):
    assignments = {}
    raw_tag_lists = project.get("tag_lists") or project.get("tagLists") or {}
    raw_tags = project.get("tags") or {}

    if isinstance(raw_tag_lists, dict):
        for raw_tag_list_id, mandatory in raw_tag_lists.items():
            tag_list_id = str(raw_tag_list_id)
            assignments[tag_list_id] = {
                "id": tag_list_id,
                "mandatory": mandatory,
                "inherit": 0,
                "hasAssignedTags": False,
                "tags": [],
            }

    if isinstance(raw_tags, dict):
        for raw_tag_id, tag in raw_tags.items():
            if not isinstance(tag, dict):
                continue

            tag_list_id = (
                tag.get("tagListId")
                or tag.get("tag_list_id")
                or tag.get("list_id")
            )
            if tag_list_id is None:
                continue

            tag_list_key = str(tag_list_id)
            tag_list = assignments.setdefault(
                tag_list_key,
                {
                    "id": tag_list_key,
                    "mandatory": 0,
                    "inherit": 0,
                    "hasAssignedTags": False,
                    "tags": [],
                },
            )
            tag_list["hasAssignedTags"] = True
            tag_list["tags"].append(
                {
                    "id": str(raw_tag_id),
                    "mandatory": tag.get("mandatory", 0),
                    "inherit": 0,
                    "tagList": {
                        "id": tag_list_key,
                    },
                }
            )

    return assignments


def _int_or_none(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_source_original_estimate_seconds(task):
    value = task.get("original_estimate_seconds")
    if value is None:
        return None

    if isinstance(value, bool):
        raise ValueError("original_estimate_seconds must be a non-negative integer")

    try:
        estimate_seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "original_estimate_seconds must be a non-negative integer"
        ) from exc

    if estimate_seconds < 0 or str(estimate_seconds) != str(value).strip():
        raise ValueError("original_estimate_seconds must be a non-negative integer")

    return estimate_seconds


def timecamp_estimate_matches(timecamp_task, estimate_seconds):
    return (
        _int_or_none(timecamp_task.get("budgeted")) == estimate_seconds
        and str(timecamp_task.get("budget_unit") or "").casefold() == "hours"
    )


def timecamp_name_matches(timecamp_task, source_name):
    return (
        str(timecamp_task.get("name") or "")
        == normalize_timecamp_task_name(source_name)
    )


def print_api_metrics_delta(label, start_metrics, end_metrics):
    delta = api_metrics_delta(start_metrics, end_metrics)
    counts = delta["counts"]
    if not counts:
        print(f"{label}: no tracked API calls")
        return

    total_count = sum(counts.values())
    parts = []
    for key in sorted(counts):
        parts.append(
            f"{key}={counts[key]} ({format_seconds(delta['seconds'].get(key, 0.0))})"
        )

    print(f"{label}: total={total_count}; {', '.join(parts)}")


def get_timecamp_parent_task_id():
    """Return configured parent task ID, or 0 to create tasks at root level."""
    if not TIMECAMP_TASK_ID or TIMECAMP_TASK_ID.strip() == "0":
        return 0
    return TIMECAMP_TASK_ID

def get_source_external_task_id(task):
    """Return the TimeCamp external_task_id for a source task."""
    if task.get('external_task_id'):
        return str(task['external_task_id'])

    task_id = str(task['task_id'])

    # Monday IDs are already compatible with the native TimeCamp integration.
    if task_id.startswith('monday_'):
        return task_id

    return f"sync_{task_id}"


def is_timecamp_task_in_sync_scope(
    external_id,
    source_external_ids,
    configured_prefix=None,
):
    """Return whether a TimeCamp task is owned by this synchronization run."""
    if not external_id:
        return False

    external_id = str(external_id)
    prefix = str(configured_prefix or "").strip()
    if prefix:
        return external_id.startswith(prefix)

    return external_id.startswith('sync_') or external_id in source_external_ids


def validate_source_external_id_scope(source_external_ids, configured_prefix=None):
    prefix = str(configured_prefix or "").strip()
    if not prefix:
        return

    out_of_scope = sorted(
        external_id
        for external_id in source_external_ids
        if not str(external_id).startswith(prefix)
    )
    if out_of_scope:
        preview = ", ".join(out_of_scope[:5])
        raise ValueError(
            "Source external_task_id values do not match "
            f"TIMECAMP_SYNC_EXTERNAL_ID_PREFIX={prefix!r}: {preview}"
        )


def load_tasks_from_json(filename=DEFAULT_TASKS_FILE):
    """Load hierarchical tasks from JSON file"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: {filename} not found. Generate the hierarchical tasks file first.")
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing {filename}: {e}")
        return []

def sync_hierarchical_tasks_to_timecamp(
    enabled_actions=None,
    input_file=DEFAULT_TASKS_FILE,
    strict_user_sync=False,
):
    """Main sync function to sync hierarchical task data from tasks.json to TimeCamp"""
    if enabled_actions is None:
        enabled_actions = get_enabled_sync_actions()
        print_sync_action_plan(enabled_actions)
    max_mandatory_tags_to_add = get_optional_env_int(TIMECAMP_MAX_MANDATORY_TAGS_TO_ADD)
    if strict_user_sync and "users" in enabled_actions:
        print(
            "Strict user sync enabled: direct TimeCamp user assignments missing "
            "from the source JSON will be removed."
        )

    # Load hierarchical task data from JSON file
    azure_tasks = load_tasks_from_json(input_file)
    if not azure_tasks:
        return

    source_external_ids = {
        get_source_external_task_id(task)
        for task in azure_tasks
    }
    validate_source_external_id_scope(
        source_external_ids,
        TIMECAMP_SYNC_EXTERNAL_ID_PREFIX,
    )

    client = TimeCampClient(TIMECAMP_API_TOKEN)
    api_metrics_before_setup = get_api_metrics_snapshot(client)
    
    # Get existing TimeCamp tasks
    timecamp_entries = client.get_tasks()

    # Ensure all mandatory tag lists/tags exist before tasks are assigned to them.
    mandatory_tag_sync = None
    if "tags" in enabled_actions or "mandatory_tags" in enabled_actions:
        mandatory_tag_sync = ensure_mandatory_tags(client, azure_tasks)

    mandatory_tag_assignment_cache = None
    mandatory_tag_cache_dirty = False
    if "mandatory_tags" in enabled_actions:
        mandatory_tag_assignment_cache = load_mandatory_tag_assignment_cache(
            TIMECAMP_MANDATORY_TAG_CACHE_FILE
        )
        print(
            "Mandatory tag assignment cache: "
            f"{TIMECAMP_MANDATORY_TAG_CACHE_FILE} "
            f"({len(mandatory_tag_assignment_cache.get('tasks', {}))} task(s))"
        )

    assigned_user_sync = None
    if "users" in enabled_actions:
        assigned_user_sync = build_assigned_user_sync_result(client, azure_tasks)

    # Create mapping of existing TimeCamp tasks by external_task_id
    timecamp_tasks_map = {}
    for entry in timecamp_entries:
        external_id = entry.get('external_task_id')
        if is_timecamp_task_in_sync_scope(
            external_id,
            source_external_ids,
            TIMECAMP_SYNC_EXTERNAL_ID_PREFIX,
        ):
            timecamp_tasks_map[external_id] = entry
    
    print(f"Found {len(timecamp_tasks_map)} existing sync/source tasks in TimeCamp")
    
    # Create mapping of source task_id to TimeCamp task_id for newly created items
    source_to_timecamp_map = {}
    
    # Track which external IDs we encounter (for cleanup later)
    active_external_ids = set()
    
    # Track sync statistics
    created_tasks = 0
    existing_tasks = 0
    archived_tasks = 0
    skipped_missing_tasks = 0
    assigned_mandatory_tags = 0
    skipped_mandatory_tag_cache = 0
    tag_assignment_errors = 0
    assigned_users_count = 0
    unassigned_users_count = 0
    skipped_user_sync_no_users = 0
    user_assignment_errors = 0
    names_updated = 0
    names_current = 0
    name_errors = 0
    estimates_updated = 0
    estimates_current = 0
    estimate_errors = 0
    
    print("Starting hierarchical task synchronization to TimeCamp...")
    api_metrics_before_task_loop = get_api_metrics_snapshot(client)
    print_api_metrics_delta(
        "API calls during setup/preflight",
        api_metrics_before_setup,
        api_metrics_before_task_loop,
    )
    
    # Build hierarchy levels dynamically
    def get_hierarchy_level(task, all_tasks):
        """Calculate hierarchy level (0 = top level)"""
        if task['parent_id'] == 0:
            return 0
        
        # Find parent task
        parent_task = next((t for t in all_tasks if t['task_id'] == task['parent_id']), None)
        if not parent_task:
            return 0  # Orphaned task becomes top level
        
        return get_hierarchy_level(parent_task, all_tasks) + 1
    
    # Add hierarchy level to each task and sort by level
    for task in azure_tasks:
        task['_hierarchy_level'] = get_hierarchy_level(task, azure_tasks)
    
    # Sort tasks by hierarchy level (parents before children)
    azure_tasks_sorted = sorted(azure_tasks, key=lambda x: (x['_hierarchy_level'], x['task_id']))

    print("Preparing task hierarchy and workload details...")
    hierarchy_levels = {task['_hierarchy_level'] for task in azure_tasks_sorted}
    missing_timecamp_task_count = sum(
        1
        for task in azure_tasks_sorted
        if get_source_external_task_id(task) not in timecamp_tasks_map
    )
    missing_task_action = "will create" if "tasks" in enabled_actions else "will skip"
    print(
        "- Source tasks sorted parent-before-child: "
        f"{len(azure_tasks_sorted)} task(s) across {len(hierarchy_levels)} level(s)"
    )
    print(f"- Existing TimeCamp source matches: {len(timecamp_tasks_map)}")
    print(f"- Missing TimeCamp tasks: {missing_timecamp_task_count} ({missing_task_action})")

    if "names" in enabled_actions:
        name_update_candidate_count = sum(
            1
            for task in azure_tasks_sorted
            if (
                get_source_external_task_id(task) in timecamp_tasks_map
                and not timecamp_name_matches(
                    timecamp_tasks_map[get_source_external_task_id(task)],
                    task["name"],
                )
            )
        )
        print(f"- Task name updates needed: {name_update_candidate_count}")

    if "estimates" in enabled_actions:
        estimated_source_task_count = sum(
            1
            for task in azure_tasks_sorted
            if task.get("original_estimate_seconds") is not None
        )
        print(f"- Tasks with source estimates: {estimated_source_task_count}")

    if "mandatory_tags" in enabled_actions:
        mandatory_tag_task_count = sum(
            1 for task in azure_tasks_sorted if get_task_mandatory_tags(task)
        )
        print(f"- Tasks with mandatory tags: {mandatory_tag_task_count}")

    if "users" in enabled_actions:
        assigned_user_task_count = sum(
            1 for task in azure_tasks_sorted if get_task_assigned_users(task)
        )
        print(f"- Tasks with source assigned users: {assigned_user_task_count}")
        if strict_user_sync:
            strict_user_sync_candidate_count = sum(
                1
                for task in azure_tasks_sorted
                if get_task_assigned_users(task)
                or (timecamp_tasks_map.get(get_source_external_task_id(task), {}).get("users", {}))
            )
            print(
                "- Strict user sync candidates: "
                f"{strict_user_sync_candidate_count} task(s)"
            )

    if "archive" in enabled_actions:
        archive_candidate_count = sum(
            1
            for external_id, timecamp_task in timecamp_tasks_map.items()
            if external_id not in source_external_ids and not timecamp_task.get('archived')
        )
        print(f"- Archive candidates: {archive_candidate_count}")

    print("Processing tasks in hierarchy order...")
    task_loop_started_at = perf_counter()
    internal_project_tag_assignments_by_parent = {}
    
    # Process all tasks in hierarchy order
    def log_task_progress(processed_count):
        if (
            processed_count == len(azure_tasks_sorted)
            or processed_count % TASK_PROGRESS_LOG_INTERVAL == 0
        ):
            api_calls = api_call_count_delta(
                api_metrics_before_task_loop,
                get_api_metrics_snapshot(client),
            )
            estimate_progress = ""
            if "estimates" in enabled_actions:
                estimate_progress = (
                    f"estimates_updated={estimates_updated}, "
                    f"estimates_current={estimates_current}, "
                )
            name_progress = ""
            if "names" in enabled_actions:
                name_progress = (
                    f"names_updated={names_updated}, "
                    f"names_current={names_current}, "
                )
            print(
                f"Processed {processed_count}/{len(azure_tasks_sorted)} task(s): "
                f"created={created_tasks}, existing={existing_tasks}, "
                f"missing_skipped={skipped_missing_tasks}, "
                f"{name_progress}"
                f"{estimate_progress}"
                f"mandatory_tags={assigned_mandatory_tags}, "
                f"mandatory_tag_cache_skips={skipped_mandatory_tag_cache}, "
                f"users_assigned={assigned_users_count}, "
                f"users_unassigned={unassigned_users_count}, "
                f"api_calls={api_calls}, "
                f"elapsed={format_seconds(perf_counter() - task_loop_started_at)}"
            )

    for processed_count, task in enumerate(azure_tasks_sorted, start=1):
        external_id = get_source_external_task_id(task)
        active_external_ids.add(external_id)

        if external_id not in timecamp_tasks_map:
            if "tasks" not in enabled_actions:
                skipped_missing_tasks += 1
                log_task_progress(processed_count)
                continue

            # Determine parent TimeCamp task ID.
            if task['parent_id'] == 0:
                parent_timecamp_id = get_timecamp_parent_task_id()
            else:
                parent_timecamp_id = source_to_timecamp_map.get(task['parent_id'])
                if not parent_timecamp_id:
                    print(f"Warning: Parent task not found for {task['name']}, making it top-level")
                    parent_timecamp_id = get_timecamp_parent_task_id()

            # Determine task type for logging
            task_type = "top-level" if task['parent_id'] == 0 else f"level-{task['_hierarchy_level']}"
            print(f"Creating {task_type} task: {task['name']}")
            
            try:
                timecamp_name = normalize_timecamp_task_name(task["name"])
                new_task = client.create_task(
                    name=timecamp_name,
                    parent_id=parent_timecamp_id,
                    external_task_id=external_id
                )
                source_to_timecamp_map[task['task_id']] = new_task['task_id']
                new_task.setdefault("users", {})
                new_task["name"] = timecamp_name
                timecamp_tasks_map[external_id] = new_task
                created_tasks += 1
            except Exception as e:
                if isinstance(e, TimeCampRateLimitError):
                    stop_on_rate_limit(e)
                print(f"Error creating task {task['name']}: {e}")
                log_task_progress(processed_count)
                continue
        else:
            existing_task = timecamp_tasks_map[external_id]
            source_to_timecamp_map[task['task_id']] = existing_task['task_id']
            existing_tasks += 1

        if "names" in enabled_actions:
            try:
                timecamp_task = timecamp_tasks_map[external_id]
                source_name = task["name"]
                timecamp_name = normalize_timecamp_task_name(source_name)
                if timecamp_name_matches(timecamp_task, source_name):
                    names_current += 1
                else:
                    client.update_task_name(
                        timecamp_task["task_id"],
                        timecamp_name,
                    )
                    timecamp_task["name"] = timecamp_name
                    names_updated += 1
            except Exception as e:
                if isinstance(e, TimeCampRateLimitError):
                    stop_on_rate_limit(e)
                name_errors += 1
                print(f"Error syncing name for task {task['name']}: {e}")

        if "estimates" in enabled_actions:
            try:
                estimate_seconds = get_source_original_estimate_seconds(task)
                if estimate_seconds is not None:
                    timecamp_task = timecamp_tasks_map[external_id]
                    if timecamp_estimate_matches(timecamp_task, estimate_seconds):
                        estimates_current += 1
                    else:
                        client.update_task_estimate(
                            timecamp_task["task_id"],
                            estimate_seconds,
                        )
                        timecamp_task["budgeted"] = estimate_seconds
                        timecamp_task["budget_unit"] = "hours"
                        estimates_updated += 1
            except Exception as e:
                if isinstance(e, TimeCampRateLimitError):
                    stop_on_rate_limit(e)
                estimate_errors += 1
                print(f"Error syncing estimate for task {task['name']}: {e}")

        if "mandatory_tags" in enabled_actions and get_task_mandatory_tags(task):
            try:
                timecamp_task = timecamp_tasks_map[external_id]
                desired_tag_assignments = get_desired_mandatory_tag_assignments(
                    task,
                    mandatory_tag_sync,
                )
                if (
                    mandatory_tag_assignment_cache is not None
                    and mandatory_tag_cache_entry_matches(
                        mandatory_tag_assignment_cache,
                        external_id,
                        timecamp_task,
                        desired_tag_assignments,
                    )
                ):
                    skipped_mandatory_tag_cache += 1
                else:
                    current_tag_assignments = None
                    timecamp_parent_id = timecamp_task.get("parent_id")
                    timecamp_task_id = str(source_to_timecamp_map[task['task_id']])
                    if (
                        timecamp_parent_id is not None
                        and callable(getattr(client, "get_internal_projects", None))
                    ):
                        parent_key = str(timecamp_parent_id)
                        if parent_key not in internal_project_tag_assignments_by_parent:
                            internal_project_tag_assignments_by_parent[parent_key] = (
                                load_internal_project_tag_assignments(
                                    client,
                                    timecamp_parent_id,
                                )
                            )

                        parent_tag_assignments = internal_project_tag_assignments_by_parent[
                            parent_key
                        ]
                        if timecamp_task_id in parent_tag_assignments:
                            current_tag_assignments = parent_tag_assignments[
                                timecamp_task_id
                            ]

                    mandatory_tag_result = sync_mandatory_tags_to_task(
                        client=client,
                        timecamp_task_id=timecamp_task_id,
                        source_task=task,
                        tag_sync_result=mandatory_tag_sync,
                        max_tags_to_add=max_mandatory_tags_to_add,
                        current_assignments=current_tag_assignments,
                    )
                    assigned_mandatory_tags += mandatory_tag_result.assigned
                    if (
                        mandatory_tag_assignment_cache is not None
                        and not mandatory_tag_result.skipped_due_to_limit
                    ):
                        record_mandatory_tag_cache_entry(
                            mandatory_tag_assignment_cache,
                            external_id,
                            timecamp_task,
                            desired_tag_assignments,
                        )
                        mandatory_tag_cache_dirty = True
            except Exception as e:
                if isinstance(e, TimeCampRateLimitError):
                    stop_on_rate_limit(e)
                tag_assignment_errors += 1
                print(f"Error assigning mandatory tags to task {task['name']}: {e}")

        if "users" in enabled_actions:
            source_assigned_users = get_task_assigned_users(task)
            current_assigned_users = timecamp_tasks_map[external_id].get("users", {})
            if strict_user_sync and not source_assigned_users and not current_assigned_users:
                skipped_user_sync_no_users += 1
            elif strict_user_sync or source_assigned_users:
                try:
                    user_sync_result = sync_users_to_task(
                        client=client,
                        timecamp_task_id=source_to_timecamp_map[task['task_id']],
                        source_task=task,
                        user_sync_result=assigned_user_sync,
                        strict=strict_user_sync,
                        current_assigned_users=current_assigned_users,
                    )
                    assigned_users_count += user_sync_result.assigned
                    unassigned_users_count += user_sync_result.unassigned
                except Exception as e:
                    if isinstance(e, TimeCampRateLimitError):
                        stop_on_rate_limit(e)
                    user_assignment_errors += 1
                    print(f"Error assigning users to task {task['name']}: {e}")

        log_task_progress(processed_count)

    api_metrics_after_task_loop = get_api_metrics_snapshot(client)
    print(
        "Task processing loop completed in "
        f"{format_seconds(perf_counter() - task_loop_started_at)}"
    )
    print_api_metrics_delta(
        "API calls during task loop",
        api_metrics_before_task_loop,
        api_metrics_after_task_loop,
    )

    # Archive TimeCamp tasks that are no longer in source system
    api_metrics_before_archive = get_api_metrics_snapshot(client)
    archive_started_at = perf_counter()
    if "archive" in enabled_actions:
        for external_id, timecamp_task in timecamp_tasks_map.items():
            if external_id not in active_external_ids and not timecamp_task.get('archived'):
                print(f"Archiving TimeCamp task: {timecamp_task['name']}")
                try:
                    client.archive_task(timecamp_task['task_id'])
                    archived_tasks += 1
                except Exception as e:
                    if isinstance(e, TimeCampRateLimitError):
                        stop_on_rate_limit(e)
                    print(f"Error archiving task {timecamp_task['name']}: {e}")
    if "archive" in enabled_actions:
        print(
            "Archive phase completed in "
            f"{format_seconds(perf_counter() - archive_started_at)}"
        )
        print_api_metrics_delta(
            "API calls during archive phase",
            api_metrics_before_archive,
            get_api_metrics_snapshot(client),
        )

    if mandatory_tag_assignment_cache is not None and mandatory_tag_cache_dirty:
        save_mandatory_tag_assignment_cache(
            TIMECAMP_MANDATORY_TAG_CACHE_FILE,
            mandatory_tag_assignment_cache,
        )
        print(
            "Mandatory tag assignment cache updated: "
            f"{TIMECAMP_MANDATORY_TAG_CACHE_FILE}"
        )
    
    print(f"\nSynchronization completed successfully!")
    print(f"- Created: {created_tasks} new tasks")
    print(f"- Existing source matches: {existing_tasks} tasks")
    print(f"- Archived: {archived_tasks} obsolete tasks")
    if "tasks" not in enabled_actions:
        print("- Task creation skipped because tasks action is disabled")
        print(f"- Missing TimeCamp tasks skipped: {skipped_missing_tasks}")
    if "archive" not in enabled_actions:
        print("- Task archiving skipped because archive action is disabled")
    if "names" in enabled_actions:
        print(f"- Names updated: {names_updated}")
        print(f"- Names already current: {names_current}")
        if name_errors:
            print(f"- Name sync errors: {name_errors}")
    if "estimates" in enabled_actions:
        print(f"- Estimates updated: {estimates_updated}")
        print(f"- Estimates already current: {estimates_current}")
        if estimate_errors:
            print(f"- Estimate sync errors: {estimate_errors}")
    print(f"- Mandatory tags assigned/updated: {assigned_mandatory_tags}")
    if "mandatory_tags" in enabled_actions:
        print(f"- Mandatory tag cache skips: {skipped_mandatory_tag_cache}")
    if tag_assignment_errors:
        print(f"- Mandatory tag assignment errors: {tag_assignment_errors}")
    print(f"- Users assigned: {assigned_users_count}")
    if strict_user_sync:
        print(f"- Users unassigned: {unassigned_users_count}")
        print(
            "- User sync skipped because no source/current users: "
            f"{skipped_user_sync_no_users}"
        )
    if user_assignment_errors:
        print(f"- User assignment errors: {user_assignment_errors}")
    print(f"- Total processed: {len(azure_tasks_sorted)} tasks")

def show_sync_preview(
    input_file=DEFAULT_TASKS_FILE,
    hierarchy_preview_limit=DEFAULT_HIERARCHY_PREVIEW_LIMIT,
):
    """Show a preview of what would be synced without making changes"""
    tasks = load_tasks_from_json(input_file)
    if not tasks:
        return
    
    # Calculate hierarchy levels
    def get_hierarchy_level(task, all_tasks):
        if task['parent_id'] == 0:
            return 0
        parent_task = next((t for t in all_tasks if t['task_id'] == task['parent_id']), None)
        if not parent_task:
            return 0
        return get_hierarchy_level(parent_task, all_tasks) + 1
    
    for task in tasks:
        task['_hierarchy_level'] = get_hierarchy_level(task, tasks)
    
    # Group by hierarchy level
    level_counts = {}
    for task in tasks:
        level = task['_hierarchy_level']
        level_counts[level] = level_counts.get(level, 0) + 1
    
    print("Hierarchical Task Sync Preview:")
    print(f"Would sync {len(tasks)} total tasks:")
    for level in sorted(level_counts.keys()):
        print(f"  - Level {level}: {level_counts[level]} tasks")

    tagged_task_count = sum(1 for task in tasks if get_task_mandatory_tags(task))
    if tagged_task_count:
        print(f"  - Tasks with mandatory tags: {tagged_task_count}")

    assigned_user_task_count = sum(1 for task in tasks if get_task_assigned_users(task))
    if assigned_user_task_count:
        print(f"  - Tasks with assigned users: {assigned_user_task_count}")

    estimated_task_count = sum(
        1 for task in tasks if task.get("original_estimate_seconds") is not None
    )
    if estimated_task_count:
        print(f"  - Tasks with source estimates: {estimated_task_count}")
    
    print("\nHierarchy preview:")
    printed_task_count = 0
    hierarchy_truncated = False
    
    def print_task_hierarchy(task_id, tasks, level=0, printed=None):
        nonlocal printed_task_count, hierarchy_truncated

        if printed is None:
            printed = set()
        
        if (
            hierarchy_preview_limit is not None
            and printed_task_count >= hierarchy_preview_limit
        ):
            hierarchy_truncated = True
            return

        if task_id in printed:
            return
        printed.add(task_id)
        
        task = next((t for t in tasks if t['task_id'] == task_id), None)
        if not task:
            return
        
        indent = "  " * level
        level_marker = f"[L{task['_hierarchy_level']}]"
        print(f"{indent}{level_marker} {task['name']} (ID: {task['task_id']})")
        printed_task_count += 1
        
        # Print children
        children = [t for t in tasks if t['parent_id'] == task_id]
        for child in children:
            print_task_hierarchy(child['task_id'], tasks, level + 1, printed)
            if hierarchy_truncated:
                return
    
    # Start with top-level tasks
    top_level_tasks = [t for t in tasks if t['parent_id'] == 0]
    for task in top_level_tasks:
        print_task_hierarchy(task['task_id'], tasks)
        if hierarchy_truncated:
            break

    if hierarchy_truncated:
        print(
            "... hierarchy preview truncated after "
            f"{hierarchy_preview_limit} task(s)"
        )

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync hierarchical tasks from a JSON file to TimeCamp."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_TASKS_FILE,
        help=f"Input tasks JSON file path (default: {DEFAULT_TASKS_FILE})",
    )
    parser.add_argument(
        "--strict-user-sync",
        action="store_true",
        help=(
            "Remove direct TimeCamp user assignments from synced tasks when "
            "those users are not present in the source JSON assigned_users."
        ),
    )

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    print(f"Starting hierarchical task sync to TimeCamp at {datetime.now()}")
    enabled_actions = get_enabled_sync_actions()
    strict_user_sync = get_strict_user_sync_enabled(args.strict_user_sync)
    print_sync_action_plan(enabled_actions)
    
    # Show preview of what would be synced
    show_sync_preview(args.input)
    
    # Run the actual sync (only if credentials are available)
    if TIMECAMP_API_TOKEN:
        try:
            sync_hierarchical_tasks_to_timecamp(
                enabled_actions,
                args.input,
                strict_user_sync,
            )
        except TimeCampRateLimitError as e:
            stop_on_rate_limit(e)
    else:
        print("\nTo run actual sync, set TIMECAMP_API_TOKEN in .env file")
    
    print(f"Sync finished at {datetime.now()}")


if __name__ == "__main__":
    main()
