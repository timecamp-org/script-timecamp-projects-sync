import argparse
import json
import os
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

from fetch_netsuite import load_config, row_value
from src.netsuite_client import NetSuiteClient
from src.timecamp_client import TimeCampClient

DEFAULT_CONFIG_FILE = "netsuite_config.json"
DEFAULT_TASKS_FILE = "tasks.json"
DEFAULT_TIMEBILL_FIELDS = {
    "employee": "employee",
    "date": "tranDate",
    "hours": "hours",
    "project": "customer",
    "project_task": "caseTaskEvent",
    "activity": None,
    "memo": "memo",
}
VALID_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9_-]+$")
CONFIG_PLACEHOLDER_PREFIX = "REPLACE_WITH_"


@dataclass(frozen=True)
class PreparedTimeBill:
    timecamp_entry_id: str
    external_id: str
    payload: Dict[str, Any]


def load_tasks(path: str) -> List[Dict[str, Any]]:
    try:
        tasks = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Tasks file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in tasks file {path}: {exc}") from exc
    if not isinstance(tasks, list):
        raise ValueError(f"{path} must contain a JSON list")
    return tasks


def parse_positive_seconds(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else 0


def format_netsuite_hours(seconds: int, rounding: str = "nearest") -> str:
    if seconds <= 0:
        raise ValueError("TimeBill duration must be positive")
    normalized_rounding = str(rounding or "nearest").strip().casefold()
    if normalized_rounding == "reject":
        if seconds % 60:
            raise ValueError("Duration is not a whole minute")
        minutes = seconds // 60
    elif normalized_rounding == "floor":
        minutes = seconds // 60
    elif normalized_rounding == "ceil":
        minutes = (seconds + 59) // 60
    elif normalized_rounding == "nearest":
        minutes = (seconds + 30) // 60
    else:
        raise ValueError(
            "time_export.duration_rounding must be nearest, floor, ceil, or reject"
        )
    if minutes <= 0:
        raise ValueError("Duration rounds to zero minutes")
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}:{remaining_minutes:02d}"


def build_employee_mapping(
    timecamp_users: Iterable[Dict[str, Any]],
    netsuite_employees: Iterable[Dict[str, Any]],
    explicit_mapping: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    if explicit_mapping is not None and not isinstance(explicit_mapping, dict):
        raise ValueError("time_export.employee_mapping must be an object")
    explicit = {
        str(timecamp_id): str(netsuite_id)
        for timecamp_id, netsuite_id in (explicit_mapping or {}).items()
        if netsuite_id not in (None, "") and str(netsuite_id).strip()
    }
    employees_by_email: Dict[str, List[str]] = {}
    for employee in netsuite_employees:
        employee_id = row_value(employee, "id")
        email = str(row_value(employee, "email") or "").strip().casefold()
        if employee_id in (None, "") or not email:
            continue
        employees_by_email.setdefault(email, []).append(str(employee_id))

    result = dict(explicit)
    for user in timecamp_users:
        user_id = user.get("user_id") or user.get("id")
        if user_id in (None, "") or str(user_id) in result:
            continue
        email = str(user.get("email") or "").strip().casefold()
        matches = employees_by_email.get(email, [])
        if len(matches) == 1:
            result[str(user_id)] = matches[0]
    return result


def reference(record_id: Any) -> Dict[str, str]:
    return {"id": str(record_id)}


def _configured_fields(export_config: Dict[str, Any]) -> Dict[str, Optional[str]]:
    raw_fields = export_config.get("fields") or {}
    if not isinstance(raw_fields, dict):
        raise ValueError("time_export.fields must be an object")
    fields = dict(DEFAULT_TIMEBILL_FIELDS)
    for key, value in raw_fields.items():
        fields[str(key)] = str(value).strip() if value else None
    for required in ("employee", "date", "hours", "project"):
        if not fields.get(required):
            raise ValueError(f"time_export.fields.{required} must be configured")
    return fields


def _apply_classification(
    payload: Dict[str, Any],
    classification: Optional[str],
    export_config: Dict[str, Any],
) -> None:
    if not classification:
        return
    raw_config = export_config.get("classification")
    if not isinstance(raw_config, dict):
        raise ValueError(
            "Time entry has CAPEX/OPEX but time_export.classification is not configured"
        )
    mode = str(raw_config.get("mode") or "").strip().casefold()
    if mode in {"project", "omit"}:
        return
    if mode != "field":
        raise ValueError(
            "time_export.classification.mode must be field, project, or omit"
        )

    field = str(raw_config.get("field") or "").strip()
    if not field:
        raise ValueError("time_export.classification.field must be configured")
    if field.startswith(CONFIG_PLACEHOLDER_PREFIX):
        raise ValueError("Replace the placeholder CAPEX/OPEX NetSuite field")
    value_map = raw_config.get("value_map") or {}
    if not isinstance(value_map, dict):
        raise ValueError("time_export.classification.value_map must be an object")
    mapped_value = None
    for raw_name, raw_value in value_map.items():
        if str(raw_name).strip().casefold() == classification.casefold():
            mapped_value = raw_value
            break
    if mapped_value in (None, ""):
        raise ValueError(f"No NetSuite value mapped for {classification}")
    if str(mapped_value).startswith(CONFIG_PLACEHOLDER_PREFIX):
        raise ValueError(f"Replace the placeholder NetSuite value for {classification}")
    value_format = str(raw_config.get("value_format") or "id").strip().casefold()
    if value_format == "id":
        payload[field] = reference(mapped_value)
    elif value_format == "raw":
        payload[field] = mapped_value
    else:
        raise ValueError("time_export.classification.value_format must be id or raw")


def prepare_timebills(
    entries: Iterable[Dict[str, Any]],
    timecamp_tasks: Iterable[Dict[str, Any]],
    source_tasks: Iterable[Dict[str, Any]],
    employee_mapping: Dict[str, str],
    config: Dict[str, Any],
) -> Tuple[List[PreparedTimeBill], Counter, List[str]]:
    export_config = config.get("time_export") or {}
    if not isinstance(export_config, dict):
        raise ValueError("time_export config must be an object")
    fields = _configured_fields(export_config)
    fixed_fields = export_config.get("fixed_fields") or {}
    if not isinstance(fixed_fields, dict):
        raise ValueError("time_export.fixed_fields must be an object")
    external_id_prefix = str(export_config.get("external_id_prefix") or "timecamp-")
    rounding = str(export_config.get("duration_rounding") or "nearest")

    timecamp_tasks_by_id = {
        str(task.get("task_id")): task
        for task in timecamp_tasks
        if task.get("task_id") not in (None, "")
    }
    source_tasks_by_id = {
        str(task.get("task_id")): task
        for task in source_tasks
        if task.get("task_id") not in (None, "")
    }

    prepared: List[PreparedTimeBill] = []
    skipped: Counter = Counter()
    errors: List[str] = []
    for entry in entries:
        entry_id = str(entry.get("id") or "").strip()
        seconds = parse_positive_seconds(entry.get("duration"))
        if not entry_id:
            skipped["missing_entry_id"] += 1
            continue
        if seconds is None:
            errors.append(f"TimeCamp entry {entry_id}: invalid duration")
            continue
        if seconds == 0:
            skipped["zero_duration"] += 1
            continue

        timecamp_task = timecamp_tasks_by_id.get(str(entry.get("task_id")))
        if not timecamp_task:
            skipped["missing_timecamp_task"] += 1
            continue
        external_task_id = str(timecamp_task.get("external_task_id") or "")
        source_task = source_tasks_by_id.get(external_task_id)
        netsuite_data = source_task.get("netsuite") if source_task else None
        if not isinstance(netsuite_data, dict):
            skipped["non_netsuite_task"] += 1
            continue

        user_id = str(entry.get("user_id") or "")
        employee_id = employee_mapping.get(user_id)
        if not employee_id:
            errors.append(
                f"TimeCamp entry {entry_id}: no NetSuite employee mapping "
                f"for user {user_id}"
            )
            continue
        project_id = netsuite_data.get("project_id")
        if project_id in (None, ""):
            errors.append(
                f"TimeCamp entry {entry_id}: source task has no NetSuite project_id"
            )
            continue
        try:
            entry_date = date.fromisoformat(str(entry.get("date") or ""))
            hours = format_netsuite_hours(seconds, rounding)
        except ValueError as exc:
            errors.append(f"TimeCamp entry {entry_id}: {exc}")
            continue

        external_id = f"{external_id_prefix}{entry_id}"
        if not VALID_EXTERNAL_ID.fullmatch(external_id):
            errors.append(
                f"TimeCamp entry {entry_id}: generated external ID contains "
                "unsupported characters"
            )
            continue

        payload = deepcopy(fixed_fields)
        payload[fields["employee"]] = reference(employee_id)
        payload[fields["date"]] = entry_date.isoformat()
        payload[fields["hours"]] = hours
        payload[fields["project"]] = reference(project_id)

        project_task_id = netsuite_data.get("project_task_id")
        if fields.get("project_task") and project_task_id not in (None, ""):
            payload[fields["project_task"]] = reference(project_task_id)

        activity_id = netsuite_data.get("activity_id")
        if activity_id in (None, ""):
            activity_id = export_config.get("default_activity_id")
        if fields.get("activity") and activity_id not in (None, ""):
            payload[fields["activity"]] = reference(activity_id)

        description = str(entry.get("description") or "").strip()
        if fields.get("memo") and description:
            payload[fields["memo"]] = description

        try:
            _apply_classification(
                payload,
                netsuite_data.get("capex_opex"),
                export_config,
            )
        except ValueError as exc:
            errors.append(f"TimeCamp entry {entry_id}: {exc}")
            continue

        prepared.append(
            PreparedTimeBill(
                timecamp_entry_id=entry_id,
                external_id=external_id,
                payload=payload,
            )
        )

    return prepared, skipped, errors


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export TimeCamp time entries to NetSuite TimeBill records."
    )
    parser.add_argument("--from", dest="start_date", required=True)
    parser.add_argument("--to", dest="end_date", required=True)
    parser.add_argument(
        "--config",
        default=os.getenv("NETSUITE_CONFIG_FILE", DEFAULT_CONFIG_FILE),
    )
    parser.add_argument("--tasks", default=DEFAULT_TASKS_FILE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to NetSuite. Without this flag the command is a dry-run.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    load_dotenv(override=True)
    args = parse_args(argv)
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if start_date > end_date:
        raise ValueError("--from must be before or equal to --to")

    config = load_config(args.config)
    export_config = config.get("time_export") or {}
    employees_query = str(export_config.get("employees_query") or "").strip()
    explicit_mapping = export_config.get("employee_mapping") or {}
    if not employees_query and not explicit_mapping:
        raise ValueError(
            "Configure time_export.employees_query or time_export.employee_mapping"
        )

    timecamp_token = os.getenv("TIMECAMP_API_TOKEN")
    if not timecamp_token:
        raise ValueError("TIMECAMP_API_TOKEN must be set in .env")
    timecamp = TimeCampClient(timecamp_token)
    netsuite = NetSuiteClient.from_env()
    source_tasks = load_tasks(args.tasks)

    print(f"Loading TimeCamp entries from {start_date} to {end_date}...")
    entries = timecamp.get_time_entries(start_date, end_date)
    timecamp_tasks = timecamp.get_tasks()
    timecamp_users = timecamp.get_users()
    employees = netsuite.suiteql(employees_query) if employees_query else []
    employee_mapping = build_employee_mapping(
        timecamp_users,
        employees,
        explicit_mapping,
    )
    prepared, skipped, errors = prepare_timebills(
        entries,
        timecamp_tasks,
        source_tasks,
        employee_mapping,
        config,
    )

    print(f"Prepared {len(prepared)} NetSuite TimeBill upsert(s)")
    for reason, count in sorted(skipped.items()):
        print(f"  Skipped {reason}: {count}")
    if errors:
        print(f"Blocked by {len(errors)} mapping/validation error(s):")
        for error in errors[:50]:
            print(f"  - {error}")
        if len(errors) > 50:
            print(f"  - ... and {len(errors) - 50} more")
        raise SystemExit(1)

    if not args.apply:
        print("Dry-run only; add --apply to write records to NetSuite.")
        for item in prepared[:20]:
            print(
                f"  {item.external_id}: {json.dumps(item.payload, ensure_ascii=False)}"
            )
        return

    record_type = str(export_config.get("record_type") or "timebill").strip()
    for index, item in enumerate(prepared, start=1):
        netsuite.upsert_record(record_type, item.external_id, item.payload)
        print(f"Upserted {index}/{len(prepared)}: {item.external_id}")


if __name__ == "__main__":
    main()
