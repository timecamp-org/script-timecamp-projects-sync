import argparse
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

from fetch_mondaycom import MondayClient, MONDAY_INTEGRATION_TASK_PREFIX
from src.timecamp_client import TimeCampClient


DEFAULT_COLUMN_TITLE = "Time Logged"
DEFAULT_TASKS_FILE = "tasks.json"
MONDAY_NUMBERS_COLUMN_TYPE = "numbers"


@dataclass
class ExportRow:
    source_task_id: str
    monday_item_id: str
    kind: str
    seconds: int

    @property
    def hours(self) -> float:
        return seconds_to_hours(self.seconds)


@dataclass
class PreparedUpdate:
    row: ExportRow
    board_id: str
    board_name: str
    item_name: str
    column_id: str
    current_hours: Optional[float]
    value: str


def seconds_to_hours(seconds: int) -> float:
    return round(seconds / 3600, 2)


def format_hours_value(seconds: int) -> str:
    value = seconds_to_hours(seconds)
    formatted = f"{value:.2f}"
    return formatted.rstrip("0").rstrip(".")


def load_source_tasks(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list of tasks")

    return data


def build_source_index(tasks: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    source_by_id: Dict[str, Dict[str, Any]] = {}
    for task in tasks:
        task_id = task.get("task_id")
        if task_id is None:
            continue
        source_by_id[str(task_id)] = task
    return source_by_id


def is_monday_external_id(value: Any) -> bool:
    return str(value or "").startswith(MONDAY_INTEGRATION_TASK_PREFIX)


def is_monday_item_external_id(value: Any) -> bool:
    raw_value = str(value or "")
    if not raw_value.startswith(MONDAY_INTEGRATION_TASK_PREFIX):
        return False

    suffix = raw_value.removeprefix(MONDAY_INTEGRATION_TASK_PREFIX)
    return suffix.isdigit()


def monday_item_id_from_external_id(external_id: str) -> str:
    if not is_monday_item_external_id(external_id):
        raise ValueError(f"Not a Monday item external id: {external_id}")

    return external_id.removeprefix(MONDAY_INTEGRATION_TASK_PREFIX)


def is_source_item_task(source_task: Dict[str, Any]) -> bool:
    task_id = source_task.get("task_id")
    parent_id = source_task.get("parent_id")
    return (
        is_monday_item_external_id(task_id)
        and parent_id not in (None, 0, "0")
    )


def is_subitem_task(source_task: Dict[str, Any]) -> bool:
    return is_monday_item_external_id(source_task.get("parent_id"))


def parse_duration_seconds(value: Any) -> Optional[int]:
    try:
        seconds = int(float(value or 0))
    except (TypeError, ValueError):
        return None

    return seconds if seconds > 0 else 0


def aggregate_entries_by_source_task(
    entries: Iterable[Dict[str, Any]],
    timecamp_tasks: Iterable[Dict[str, Any]],
    source_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, int], Counter]:
    timecamp_tasks_by_id = {
        str(task.get("task_id")): task
        for task in timecamp_tasks
        if task.get("task_id") is not None
    }
    seconds_by_source_task: Dict[str, int] = defaultdict(int)
    skipped: Counter = Counter()

    for entry in entries:
        seconds = parse_duration_seconds(entry.get("duration"))
        if seconds is None:
            skipped["invalid_duration"] += 1
            continue
        if seconds == 0:
            skipped["zero_duration"] += 1
            continue

        timecamp_task = timecamp_tasks_by_id.get(str(entry.get("task_id")))
        if not timecamp_task:
            skipped["missing_timecamp_task"] += 1
            continue

        external_task_id = timecamp_task.get("external_task_id")
        if not is_monday_external_id(external_task_id):
            skipped["no_monday_external_task_id"] += 1
            continue

        source_task = source_by_id.get(str(external_task_id))
        if not source_task:
            skipped["not_in_tasks_json"] += 1
            continue

        if not is_source_item_task(source_task):
            skipped["non_item_source_task"] += 1
            continue

        seconds_by_source_task[str(external_task_id)] += seconds

    return dict(seconds_by_source_task), skipped


def build_export_rows(
    seconds_by_source_task: Dict[str, int],
    source_by_id: Dict[str, Dict[str, Any]],
    write_zeroes: bool = False,
    include_main_rows: bool = False,
) -> List[ExportRow]:
    subitem_seconds: Dict[str, int] = defaultdict(int)
    main_item_seconds: Dict[str, int] = defaultdict(int)

    for source_task_id, seconds in seconds_by_source_task.items():
        source_task = source_by_id.get(source_task_id)
        if not source_task or not is_source_item_task(source_task):
            continue

        if is_subitem_task(source_task):
            subitem_seconds[source_task_id] += seconds
            if include_main_rows:
                main_item_seconds[str(source_task["parent_id"])] += seconds
        else:
            if include_main_rows:
                main_item_seconds[source_task_id] += seconds

    if write_zeroes:
        for source_task_id, source_task in source_by_id.items():
            if not is_source_item_task(source_task):
                continue
            if is_subitem_task(source_task):
                subitem_seconds.setdefault(source_task_id, 0)
            elif include_main_rows:
                main_item_seconds.setdefault(source_task_id, 0)

    rows: List[ExportRow] = []
    for source_task_id, seconds in subitem_seconds.items():
        if seconds > 0 or write_zeroes:
            rows.append(
                ExportRow(
                    source_task_id=source_task_id,
                    monday_item_id=monday_item_id_from_external_id(source_task_id),
                    kind="subitem",
                    seconds=seconds,
                )
            )

    for source_task_id, seconds in main_item_seconds.items():
        if seconds > 0 or write_zeroes:
            rows.append(
                ExportRow(
                    source_task_id=source_task_id,
                    monday_item_id=monday_item_id_from_external_id(source_task_id),
                    kind="main",
                    seconds=seconds,
                )
            )

    return sorted(rows, key=lambda row: (row.kind, row.source_task_id))


def get_ignored_main_row_time(
    seconds_by_source_task: Dict[str, int],
    source_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[int, int]:
    task_count = 0
    seconds_total = 0

    for source_task_id, seconds in seconds_by_source_task.items():
        source_task = source_by_id.get(source_task_id)
        if not source_task or not is_source_item_task(source_task):
            continue
        if is_subitem_task(source_task):
            continue

        task_count += 1
        seconds_total += seconds

    return task_count, seconds_total


def find_numbers_column_by_title(board: Dict[str, Any], column_title: str) -> Dict[str, Any]:
    normalized_title = column_title.strip().lower()
    same_title_columns = [
        column
        for column in board.get("columns", [])
        if str(column.get("title") or "").strip().lower() == normalized_title
    ]
    numbers_columns = [
        column
        for column in same_title_columns
        if column.get("type") == MONDAY_NUMBERS_COLUMN_TYPE
    ]

    if not same_title_columns:
        raise ValueError(
            f"Board {board.get('id')} ({board.get('name')}) has no "
            f"{column_title!r} column"
        )
    if not numbers_columns:
        found_types = ", ".join(str(column.get("type")) for column in same_title_columns)
        raise ValueError(
            f"Board {board.get('id')} ({board.get('name')}) has "
            f"{column_title!r}, but it is not a numbers column: {found_types}"
        )
    if len(numbers_columns) > 1:
        column_ids = ", ".join(str(column.get("id")) for column in numbers_columns)
        raise ValueError(
            f"Board {board.get('id')} ({board.get('name')}) has multiple "
            f"{column_title!r} numbers columns: {column_ids}"
        )

    return numbers_columns[0]


def parse_monday_number(column_value: Dict[str, Any]) -> Optional[float]:
    text_value = str(column_value.get("text") or "").strip()
    if text_value:
        parsed = parse_float(text_value)
        if parsed is not None:
            return parsed

    raw_value = column_value.get("value")
    if raw_value in (None, ""):
        return None

    try:
        decoded = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (TypeError, json.JSONDecodeError):
        decoded = raw_value

    if isinstance(decoded, dict):
        for key in ("number", "value"):
            parsed = parse_float(decoded.get(key))
            if parsed is not None:
                return parsed

    return parse_float(decoded)


def parse_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def current_item_column_hours(item: Dict[str, Any], column_id: str) -> Optional[float]:
    for column_value in item.get("column_values", []):
        if str(column_value.get("id")) == str(column_id):
            return parse_monday_number(column_value)
    return None


def prepare_updates(
    rows: Iterable[ExportRow],
    monday_items: Iterable[Dict[str, Any]],
    board_columns: Iterable[Dict[str, Any]],
    column_title: str,
) -> Tuple[List[PreparedUpdate], List[ExportRow], Counter]:
    items_by_id = {str(item.get("id")): item for item in monday_items}
    boards_by_id = {str(board.get("id")): board for board in board_columns}
    column_id_by_board_id: Dict[str, str] = {}
    updates: List[PreparedUpdate] = []
    missing_rows: List[ExportRow] = []
    skipped: Counter = Counter()

    for row in rows:
        item = items_by_id.get(row.monday_item_id)
        if not item:
            skipped["missing_monday_item"] += 1
            missing_rows.append(row)
            continue

        board = item.get("board") or {}
        board_id = str(board.get("id") or "")
        board_metadata = boards_by_id.get(board_id)
        if not board_metadata:
            skipped["missing_board_metadata"] += 1
            missing_rows.append(row)
            continue

        if board_id not in column_id_by_board_id:
            column = find_numbers_column_by_title(board_metadata, column_title)
            column_id_by_board_id[board_id] = str(column["id"])

        column_id = column_id_by_board_id[board_id]
        current_hours = current_item_column_hours(item, column_id)
        target_hours = row.hours

        if current_hours is not None and round(current_hours, 2) == target_hours:
            skipped["unchanged"] += 1
            continue

        updates.append(
            PreparedUpdate(
                row=row,
                board_id=board_id,
                board_name=str(board.get("name") or board_metadata.get("name") or ""),
                item_name=str(item.get("name") or ""),
                column_id=column_id,
                current_hours=current_hours,
                value=format_hours_value(row.seconds),
            )
        )

    return updates, missing_rows, skipped


def parse_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} must use YYYY-MM-DD format"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export TimeCamp time totals into Monday.com Time Logged numbers columns."
        )
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        required=True,
        type=parse_date,
        help="Start date, inclusive, in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        required=True,
        type=parse_date,
        help="End date, inclusive, in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--tasks-file",
        default=DEFAULT_TASKS_FILE,
        help=f"Path to tasks.json produced by fetch_mondaycom.py (default: {DEFAULT_TASKS_FILE})",
    )
    parser.add_argument(
        "--column-title",
        default=DEFAULT_COLUMN_TITLE,
        help=f"Monday numbers column title to update (default: {DEFAULT_COLUMN_TITLE})",
    )
    parser.add_argument(
        "--write-zeroes",
        action="store_true",
        help="Also write 0 to synced Monday item rows with no TimeCamp time in the date range",
    )
    parser.add_argument(
        "--include-main-rows",
        action="store_true",
        help=(
            "Also update main Monday rows with direct main-row time plus subitem rollups. "
            "By default only subitem rows are updated."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write values to Monday.com. Without this flag the command is a dry-run.",
    )
    return parser.parse_args()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} must be set in .env")
    return value


def print_skipped_counter(title: str, skipped: Counter) -> None:
    if not skipped:
        return

    print(title)
    for key, count in sorted(skipped.items()):
        print(f"  - {key}: {count}")


def main() -> None:
    load_dotenv(override=True)
    args = parse_args()

    if args.from_date > args.to_date:
        raise ValueError("--from must be before or equal to --to")

    monday_token = require_env("MONDAY_API_TOKEN")
    timecamp_token = require_env("TIMECAMP_API_TOKEN")

    source_tasks = load_source_tasks(args.tasks_file)
    source_by_id = build_source_index(source_tasks)

    timecamp_client = TimeCampClient(timecamp_token)
    monday_client = MondayClient(monday_token)

    print("Loading TimeCamp tasks and entries...")
    timecamp_tasks = timecamp_client.get_tasks()
    entries = timecamp_client.get_time_entries(args.from_date, args.to_date)

    seconds_by_source_task, entry_skips = aggregate_entries_by_source_task(
        entries=entries,
        timecamp_tasks=timecamp_tasks,
        source_by_id=source_by_id,
    )
    rows = build_export_rows(
        seconds_by_source_task=seconds_by_source_task,
        source_by_id=source_by_id,
        write_zeroes=args.write_zeroes,
        include_main_rows=args.include_main_rows,
    )
    ignored_main_task_count, ignored_main_seconds = get_ignored_main_row_time(
        seconds_by_source_task=seconds_by_source_task,
        source_by_id=source_by_id,
    )

    print("Loading Monday item and board metadata...")
    item_ids = [row.monday_item_id for row in rows]
    monday_items = monday_client.get_items_by_ids(item_ids)
    board_ids = sorted({
        str((item.get("board") or {}).get("id"))
        for item in monday_items
        if (item.get("board") or {}).get("id")
    })
    board_columns = monday_client.get_board_columns(board_ids)

    updates, missing_rows, update_skips = prepare_updates(
        rows=rows,
        monday_items=monday_items,
        board_columns=board_columns,
        column_title=args.column_title,
    )

    print("\nSummary:")
    print(f"- Date range: {args.from_date} to {args.to_date}")
    print(f"- TimeCamp entries read: {len(entries)}")
    print(f"- TimeCamp tasks read: {len(timecamp_tasks)}")
    print(f"- Synced Monday tasks with time: {len(seconds_by_source_task)}")
    print(f"- Monday rows calculated: {len(rows)}")
    print(f"- Monday rows needing update: {len(updates)}")
    print(
        "- Main rows: "
        f"{'included' if args.include_main_rows else 'not updated; Monday should tally subitems'}"
    )
    if not args.include_main_rows and ignored_main_task_count:
        print(
            "- Direct main-row time ignored: "
            f"{ignored_main_task_count} task(s), {seconds_to_hours(ignored_main_seconds):.2f}h"
        )
    print(f"- Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print_skipped_counter("- Entry skips:", entry_skips)
    print_skipped_counter("- Update skips:", update_skips)

    if missing_rows:
        print("- Missing Monday rows:")
        for row in missing_rows[:20]:
            print(f"  - {row.source_task_id}: {row.hours:.2f}h")
        if len(missing_rows) > 20:
            print(f"  - ... and {len(missing_rows) - 20} more")

    if updates:
        print("\nUpdates:")
        for update in updates[:50]:
            previous = (
                "" if update.current_hours is None else f"{update.current_hours:.2f}"
            )
            print(
                f"  - {update.row.kind} {update.item_name} "
                f"({update.board_name}, item {update.row.monday_item_id}, "
                f"column {update.column_id}): {previous or 'empty'} -> "
                f"{update.row.hours:.2f}h"
            )
        if len(updates) > 50:
            print(f"  - ... and {len(updates) - 50} more")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write these values to Monday.com.")
        return

    failed_updates = []
    print("\nWriting Monday values...")
    for update in updates:
        try:
            monday_client.change_simple_column_value(
                board_id=update.board_id,
                item_id=update.row.monday_item_id,
                column_id=update.column_id,
                value=update.value,
            )
        except Exception as exc:
            failed_updates.append((update, exc))
            print(
                f"  FAILED {update.item_name} "
                f"(item {update.row.monday_item_id}): {exc}"
            )
            continue

        print(
            f"  Updated {update.item_name} "
            f"(item {update.row.monday_item_id}) -> {update.row.hours:.2f}h"
        )

    if failed_updates:
        raise RuntimeError(f"{len(failed_updates)} Monday update(s) failed")

    print(f"\nDone. Updated {len(updates)} Monday row(s).")


if __name__ == "__main__":
    main()
