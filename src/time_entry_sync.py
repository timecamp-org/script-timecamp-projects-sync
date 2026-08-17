import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from src.time_entry_sync_state import SyncMapping


class StatePersistenceError(OSError):
    """Distinguish local state failures from HTTP errors inheriting OSError."""


@dataclass(frozen=True)
class SourceTimeEntry:
    entry_id: str
    user_id: Optional[str]
    task_id: Optional[str]
    entry_date: Optional[str]
    start_time: Optional[str]
    duration_seconds: Optional[int]
    description: Any
    external_task_id: Optional[str]
    raw: Mapping[str, Any]

    @classmethod
    def from_raw(
        cls,
        raw: Mapping[str, Any],
        *,
        deleted: bool = False,
    ) -> "SourceTimeEntry":
        entry_id = raw.get("id", raw.get("entry_id"))
        if entry_id is None:
            raise ValueError(f"TimeCamp entry has no id: {raw!r}")

        duration: Optional[int] = None
        if not deleted or raw.get("duration") is not None:
            duration = parse_nonnegative_int(
                raw.get("duration", 0),
                "duration",
                str(entry_id),
            )

        return cls(
            entry_id=str(entry_id),
            user_id=(str(raw["user_id"]) if raw.get("user_id") is not None else None),
            task_id=(str(raw["task_id"]) if raw.get("task_id") is not None else None),
            entry_date=(str(raw["date"]) if raw.get("date") is not None else None),
            start_time=(
                str(raw["start_time"]) if raw.get("start_time") is not None else None
            ),
            duration_seconds=duration,
            description=raw.get("description"),
            external_task_id=(
                str(raw["addons_external_id"])
                if raw.get("addons_external_id") is not None
                else None
            ),
            raw=raw,
        )


@dataclass(frozen=True)
class PreparedRemoteEntry:
    target_key: str
    create_payload: Mapping[str, Any]
    update_payload: Mapping[str, Any]
    source_fingerprint: str
    target_label: str


@dataclass(frozen=True)
class RecoveredRemoteEntry:
    remote_id: str
    target_key: str
    remote_payload: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class SyncRunContext:
    backfill_from: Optional[date]
    backfill_to: Optional[date]
    today: date
    active_entries: Tuple[SourceTimeEntry, ...]
    deleted_entries: Tuple[SourceTimeEntry, ...]
    users_by_id: Mapping[str, Mapping[str, Any]]
    tasks_by_id: Mapping[str, Mapping[str, Any]]


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    moved: int = 0
    recovered: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return self.failed == 0


class SyncState(Protocol):
    cursor_date: Optional[str]

    def get(self, entry_id: Any) -> Optional[SyncMapping]: ...

    def set(self, entry_id: Any, mapping: SyncMapping) -> None: ...

    def remove(self, entry_id: Any) -> None: ...

    def save(self) -> None: ...


class TimeEntryTargetAdapter(Protocol):
    adapter_id: str
    target_name: str
    minimum_duration_seconds: int

    def prepare_run(self, context: SyncRunContext) -> None: ...

    def recovery_target(
        self,
        entry: SourceTimeEntry,
        task: Mapping[str, Any],
    ) -> Optional[str]: ...

    def prepare_entry(
        self,
        entry: SourceTimeEntry,
        user: Optional[Mapping[str, Any]],
        task: Mapping[str, Any],
    ) -> Optional[PreparedRemoteEntry]: ...

    def ineligible_reason(
        self,
        entry: SourceTimeEntry,
        task: Mapping[str, Any],
    ) -> str: ...

    def recover(
        self,
        entry: SourceTimeEntry,
        target_key: str,
    ) -> Optional[RecoveredRemoteEntry]: ...

    def read(self, mapping: SyncMapping) -> Mapping[str, Any]: ...

    def fingerprint_remote(self, payload: Mapping[str, Any]) -> str: ...

    def create(self, prepared: PreparedRemoteEntry) -> str: ...

    def update(
        self,
        mapping: SyncMapping,
        prepared: PreparedRemoteEntry,
    ) -> None: ...

    def delete(self, mapping: SyncMapping) -> None: ...

    def is_missing(self, exc: BaseException) -> bool: ...

    def describe_mapping(self, mapping: SyncMapping) -> str: ...


class TimeEntrySyncEngine:
    """Destination-neutral one-to-one TimeCamp entry mirror."""

    def __init__(
        self,
        timecamp_client: Any,
        adapter: TimeEntryTargetAdapter,
        state: SyncState,
        *,
        dry_run: bool = False,
        today: Optional[date] = None,
        user_ids: Optional[List[int]] = None,
    ):
        self.timecamp_client = timecamp_client
        self.adapter = adapter
        self.state = state
        self.dry_run = dry_run
        self.today = today or date.today()
        self.user_ids = (
            sorted(set(int(user_id) for user_id in user_ids))
            if user_ids is not None
            else None
        )
        if user_ids is not None and not self.user_ids:
            raise ValueError("The TimeCamp user filter cannot be empty")
        self.result = SyncResult()
        self._manual_backfill = False
        self._recovered_payloads: Dict[str, Mapping[str, Any]] = {}

    def run(
        self,
        backfill_from: Optional[date] = None,
        backfill_to: Optional[date] = None,
    ) -> SyncResult:
        self._validate_run_dates(backfill_from, backfill_to)
        self._manual_backfill = backfill_from is not None
        previous_cursor = self.state.cursor_date

        if previous_cursor is not None:
            cursor = parse_iso_date(previous_cursor, "state cursor_date")
            if cursor > self.today:
                raise ValueError(
                    f"State cursor {cursor} is later than today {self.today}"
                )

        tasks = self.timecamp_client.get_tasks()
        tasks_by_id = {
            str(task.get("task_id")): task
            for task in tasks
            if task.get("task_id") is not None
        }
        entries, deletions = self._load_source_entries(
            backfill_from,
            backfill_to,
            previous_cursor,
        )
        active_by_id = deduplicate_by_entry_id(entries, deleted=False)
        deleted_by_id = deduplicate_by_entry_id(deletions, deleted=True)
        for deleted_id in deleted_by_id:
            active_by_id.pop(deleted_id, None)

        all_user_ids = sorted(
            {
                int(entry.user_id)
                for entry in (*active_by_id.values(), *deleted_by_id.values())
                if entry.user_id is not None
            }
        )
        user_details = self.timecamp_client.get_user_details(all_user_ids)
        users_by_id = {
            str(user.get("user_id")): user
            for user in user_details
            if user.get("user_id") is not None
        }

        context = SyncRunContext(
            backfill_from=backfill_from,
            backfill_to=backfill_to,
            today=self.today,
            active_entries=tuple(active_by_id.values()),
            deleted_entries=tuple(deleted_by_id.values()),
            users_by_id=users_by_id,
            tasks_by_id=tasks_by_id,
        )
        self.adapter.prepare_run(context)

        print(
            f"Processing {len(active_by_id)} active and "
            f"{len(deleted_by_id)} deleted TimeCamp entries..."
        )
        for entry_id, entry in sorted(active_by_id.items(), key=entry_sort_key):
            try:
                self._process_active(entry, users_by_id, tasks_by_id)
            except StatePersistenceError:
                raise
            except Exception as exc:
                self._record_failure(entry_id, exc)

        for entry_id, entry in sorted(deleted_by_id.items(), key=entry_sort_key):
            try:
                self._process_deleted(entry, tasks_by_id)
            except StatePersistenceError:
                raise
            except Exception as exc:
                self._record_failure(entry_id, exc)

        if self.result.successful and not self.dry_run:
            if backfill_from is None or previous_cursor is None:
                self.state.cursor_date = self.today.isoformat()
                self._save_state()
        elif self.result.failed:
            print("Cursor was not advanced because at least one entry failed.")
        return self.result

    def _validate_run_dates(
        self,
        backfill_from: Optional[date],
        backfill_to: Optional[date],
    ) -> None:
        if (backfill_from is None) != (backfill_to is None):
            raise ValueError("Both --from and --to must be provided")
        if backfill_from and backfill_from > backfill_to:
            raise ValueError("--from must be before or equal to --to")
        if self.state.cursor_date is None and backfill_from is None:
            raise ValueError(
                "The first run requires --from and --to to establish the backfill"
            )

    def _load_source_entries(
        self,
        backfill_from: Optional[date],
        backfill_to: Optional[date],
        previous_cursor: Optional[str],
    ) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
        if backfill_from is not None:
            print(
                "Loading TimeCamp entries dated "
                f"{backfill_from} through {backfill_to}..."
            )
            kwargs: Dict[str, Any] = {}
            if self.user_ids is not None:
                kwargs["user_ids"] = self.user_ids
            entries = self.timecamp_client.get_time_entries(
                backfill_from,
                backfill_to,
                **kwargs,
            )
            deletions: List[Mapping[str, Any]] = []
        else:
            print(
                "Loading TimeCamp entries modified "
                f"{previous_cursor} through {self.today}..."
            )
            entry_kwargs: Dict[str, Any] = {
                "modify_from": previous_cursor,
                "modify_to": self.today,
            }
            deletion_kwargs: Dict[str, Any] = {}
            if self.user_ids is not None:
                entry_kwargs["user_ids"] = self.user_ids
                deletion_kwargs["user_ids"] = self.user_ids
            entries = self.timecamp_client.get_time_entries(**entry_kwargs)
            deletions = self.timecamp_client.get_time_entry_deletions(
                previous_cursor,
                self.today,
                **deletion_kwargs,
            )

        if self.user_ids is not None:
            allowed = {str(user_id) for user_id in self.user_ids}
            entries = [
                entry for entry in entries if str(entry.get("user_id")) in allowed
            ]
            deletions = [
                entry for entry in deletions if str(entry.get("user_id")) in allowed
            ]
        return entries, deletions

    def _process_active(
        self,
        entry: SourceTimeEntry,
        users_by_id: Mapping[str, Mapping[str, Any]],
        tasks_by_id: Mapping[str, Mapping[str, Any]],
    ) -> None:
        task = tasks_by_id.get(entry.task_id or "", {})
        mapping = self.state.get(entry.entry_id)
        recovery_target = self.adapter.recovery_target(entry, task)
        if mapping is None and recovery_target is not None:
            mapping = self._recover(entry, recovery_target)

        duration = entry.duration_seconds or 0
        if duration < self.adapter.minimum_duration_seconds:
            if mapping is None:
                self._skip(
                    entry.entry_id,
                    f"duration is below {self.adapter.target_name}'s minimum",
                )
            else:
                self._delete_mapping(entry.entry_id, mapping, "entry is too short")
            return

        user = users_by_id.get(entry.user_id or "")
        prepared = self.adapter.prepare_entry(entry, user, task)
        if prepared is None:
            if mapping is None:
                self._skip(
                    entry.entry_id,
                    self.adapter.ineligible_reason(entry, task),
                )
            else:
                self._delete_mapping(
                    entry.entry_id,
                    mapping,
                    f"entry moved off {self.adapter.target_name}",
                )
            return

        if mapping is None:
            self._create(entry.entry_id, prepared, action="create")
            return
        if mapping.target_key != prepared.target_key:
            self._move(entry.entry_id, mapping, prepared)
            return
        self._update(entry.entry_id, mapping, prepared)

    def _process_deleted(
        self,
        entry: SourceTimeEntry,
        tasks_by_id: Mapping[str, Mapping[str, Any]],
    ) -> None:
        mapping = self.state.get(entry.entry_id)
        if mapping is None:
            task = tasks_by_id.get(entry.task_id or "", {})
            target = self.adapter.recovery_target(entry, task)
            if target is not None:
                mapping = self._recover(entry, target)
        if mapping is None:
            self._skip(
                entry.entry_id,
                f"deleted entry has no exported {self.adapter.target_name} entry",
            )
            return
        self._delete_mapping(entry.entry_id, mapping, "TimeCamp entry deleted")

    def _recover(
        self,
        entry: SourceTimeEntry,
        target_key: str,
    ) -> Optional[SyncMapping]:
        recovered = self.adapter.recover(entry, target_key)
        if recovered is None:
            return None
        mapping = SyncMapping(recovered.target_key, recovered.remote_id)
        if recovered.remote_payload is not None:
            self._recovered_payloads[entry.entry_id] = recovered.remote_payload
        self.result.recovered += 1
        print(
            f"Recovering TimeCamp entry {entry.entry_id} from "
            f"{self.adapter.describe_mapping(mapping)}"
        )
        if not self.dry_run:
            self.state.set(entry.entry_id, mapping)
            self._save_state()
        return mapping

    def _create(
        self,
        entry_id: str,
        prepared: PreparedRemoteEntry,
        *,
        action: str,
    ) -> None:
        verb = "Would create" if self.dry_run else "Creating"
        print(
            f"{verb} {self.adapter.target_name} for TimeCamp entry "
            f"{entry_id} on {prepared.target_label}"
        )
        if self.dry_run:
            if action == "move":
                self.result.moved += 1
            else:
                self.result.created += 1
            return

        remote_id = str(self.adapter.create(prepared))
        if not remote_id:
            raise ValueError(f"{self.adapter.target_name} create returned an empty id")
        self.state.set(
            entry_id,
            SyncMapping(
                prepared.target_key,
                remote_id,
                prepared.source_fingerprint,
            ),
        )
        self._save_state()
        if action == "move":
            self.result.moved += 1
        else:
            self.result.created += 1

    def _update(
        self,
        entry_id: str,
        mapping: SyncMapping,
        prepared: PreparedRemoteEntry,
    ) -> None:
        if (
            mapping.source_fingerprint == prepared.source_fingerprint
            and not self._manual_backfill
        ):
            self._unchanged(entry_id, mapping)
            return

        if self._manual_backfill or mapping.source_fingerprint is None:
            remote = self._recovered_payloads.pop(entry_id, None)
            if remote is None:
                try:
                    remote = self.adapter.read(mapping)
                except Exception as exc:
                    if not self.adapter.is_missing(exc):
                        raise
                    self._recreate_missing(entry_id, mapping, prepared)
                    return
            if self.adapter.fingerprint_remote(remote) == prepared.source_fingerprint:
                if (
                    not self.dry_run
                    and mapping.source_fingerprint != prepared.source_fingerprint
                ):
                    mapping = SyncMapping(
                        mapping.target_key,
                        mapping.remote_id,
                        prepared.source_fingerprint,
                    )
                    self.state.set(entry_id, mapping)
                    self._save_state()
                self._unchanged(entry_id, mapping)
                return

        verb = "Would update" if self.dry_run else "Updating"
        print(
            f"{verb} {self.adapter.describe_mapping(mapping)} from "
            f"TimeCamp entry {entry_id}"
        )
        if self.dry_run:
            self.result.updated += 1
            return
        try:
            self.adapter.update(mapping, prepared)
        except Exception as exc:
            if not self.adapter.is_missing(exc):
                raise
            self._recreate_missing(entry_id, mapping, prepared)
            return

        self.state.set(
            entry_id,
            SyncMapping(
                mapping.target_key,
                mapping.remote_id,
                prepared.source_fingerprint,
            ),
        )
        self._save_state()
        self.result.updated += 1

    def _recreate_missing(
        self,
        entry_id: str,
        mapping: SyncMapping,
        prepared: PreparedRemoteEntry,
    ) -> None:
        print(
            f"{self.adapter.describe_mapping(mapping)} no longer exists; recreating it"
        )
        if not self.dry_run:
            self.state.remove(entry_id)
            self._save_state()
        self._create(entry_id, prepared, action="create")

    def _move(
        self,
        entry_id: str,
        old_mapping: SyncMapping,
        prepared: PreparedRemoteEntry,
    ) -> None:
        verb = "Would move" if self.dry_run else "Moving"
        print(
            f"{verb} TimeCamp entry {entry_id} from "
            f"{self.adapter.describe_mapping(old_mapping)} to "
            f"{prepared.target_label}"
        )
        if self.dry_run:
            self.result.moved += 1
            return
        self._delete_remote(old_mapping)
        self.state.remove(entry_id)
        self._save_state()
        self._create(entry_id, prepared, action="move")

    def _delete_mapping(
        self,
        entry_id: str,
        mapping: SyncMapping,
        reason: str,
    ) -> None:
        verb = "Would delete" if self.dry_run else "Deleting"
        print(
            f"{verb} {self.adapter.describe_mapping(mapping)} for TimeCamp entry "
            f"{entry_id}: {reason}"
        )
        if not self.dry_run:
            self._delete_remote(mapping)
            self.state.remove(entry_id)
            self._save_state()
        self.result.deleted += 1

    def _delete_remote(self, mapping: SyncMapping) -> None:
        try:
            self.adapter.delete(mapping)
        except Exception as exc:
            if not self.adapter.is_missing(exc):
                raise

    def _save_state(self) -> None:
        try:
            self.state.save()
        except OSError as exc:
            raise StatePersistenceError(str(exc)) from exc

    def _skip(self, entry_id: str, reason: str) -> None:
        print(f"Skipping TimeCamp entry {entry_id}: {reason}")
        self.result.skipped += 1

    def _unchanged(self, entry_id: str, mapping: SyncMapping) -> None:
        print(
            f"Skipping unchanged TimeCamp entry {entry_id}; "
            f"{self.adapter.describe_mapping(mapping)} already matches"
        )
        self.result.unchanged += 1

    def _record_failure(self, entry_id: str, exc: Exception) -> None:
        detail = str(exc).strip() or exc.__class__.__name__
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is not None and f"HTTP {status_code}" not in detail:
            detail = f"HTTP {status_code}: {detail}"
        message = f"TimeCamp entry {entry_id} failed: {detail}"
        print(f"Error: {message}")
        self.result.failed += 1
        self.result.errors.append(message)


def deduplicate_by_entry_id(
    entries: Iterable[Mapping[str, Any]],
    *,
    deleted: bool,
) -> Dict[str, SourceTimeEntry]:
    result: Dict[str, SourceTimeEntry] = {}
    for raw_entry in entries:
        entry = SourceTimeEntry.from_raw(raw_entry, deleted=deleted)
        result[entry.entry_id] = entry
    return result


def entry_sort_key(item: Tuple[str, Any]) -> Tuple[int, Any]:
    try:
        return (0, int(item[0]))
    except ValueError:
        return (1, item[0])


def canonical_fingerprint(payload: Mapping[str, Any], *, version: str = "v1") -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{version}:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def normalize_person_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.casefold().split())


def match_users_by_email_then_name(
    timecamp_users: Iterable[Mapping[str, Any]],
    destination_users: Iterable[Mapping[str, Any]],
    *,
    destination_id_field: str = "id",
    destination_email_field: str = "email",
    destination_name_fields: Tuple[str, ...] = ("name",),
) -> Tuple[Dict[str, str], List[str]]:
    by_email: Dict[str, List[str]] = {}
    by_name: Dict[str, List[str]] = {}
    for destination_user in destination_users:
        destination_id = destination_user.get(destination_id_field)
        if destination_id is None:
            continue
        destination_id_string = str(destination_id)
        email = (
            str(destination_user.get(destination_email_field) or "").strip().casefold()
        )
        if email:
            by_email.setdefault(email, []).append(destination_id_string)
        name = normalize_person_name(
            " ".join(
                str(destination_user.get(field) or "").strip()
                for field in destination_name_fields
            )
        )
        if name:
            by_name.setdefault(name, []).append(destination_id_string)

    mapping: Dict[str, str] = {}
    fallback_messages: List[str] = []
    for user in timecamp_users:
        timecamp_id = user.get("user_id")
        if timecamp_id is None:
            continue
        email = str(user.get("email") or "").strip().casefold()
        email_matches = by_email.get(email, []) if email else []
        if len(email_matches) > 1:
            raise ValueError(
                f"Multiple destination users have TimeCamp email {email!r}"
            )
        if email_matches:
            mapping[str(timecamp_id)] = email_matches[0]
            continue

        display_name = normalize_person_name(
            user.get("display_name") or user.get("name") or user.get("user_name")
        )
        name_matches = by_name.get(display_name, []) if display_name else []
        if len(name_matches) > 1:
            raise ValueError(
                f"Ambiguous destination user name for TimeCamp user "
                f"{timecamp_id}: {display_name!r}"
            )
        if name_matches:
            mapping[str(timecamp_id)] = name_matches[0]
            fallback_messages.append(
                f"Matched TimeCamp user {timecamp_id} by name: {display_name}"
            )
    return mapping, fallback_messages


def parse_nonnegative_int(value: Any, field_name: str, entry_id: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid {field_name} for TimeCamp entry {entry_id}: {value!r}"
        ) from exc
    if parsed < 0:
        raise ValueError(
            f"Invalid {field_name} for TimeCamp entry {entry_id}: {value!r}"
        )
    return parsed


def parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value!r}; expected YYYY-MM-DD") from exc


def find_timecamp_user_id(users: Iterable[Mapping[str, Any]], email: str) -> int:
    normalized_email = email.strip().casefold()
    if not normalized_email:
        raise ValueError("--user-email cannot be empty")
    matches = [
        user
        for user in users
        if str(user.get("email", "")).strip().casefold() == normalized_email
    ]
    if not matches:
        raise ValueError(f"TimeCamp user not found for email {email!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple TimeCamp users found for email {email!r}")
    try:
        return int(matches[0]["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"TimeCamp user {email!r} has no valid user_id") from exc


def filtered_state_file(adapter_id: str, email: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", email.strip().lower()).strip("_")
    if not slug:
        raise ValueError("Cannot build a state filename for an empty user email")
    return f"data/{adapter_id}_time_entries_state.{slug}.json"


def print_sync_summary(result: SyncResult, *, dry_run: bool) -> None:
    mode = "Dry run" if dry_run else "Export"
    print(f"\n{mode} summary:")
    print(f"- Created: {result.created}")
    print(f"- Updated: {result.updated}")
    print(f"- Moved: {result.moved}")
    print(f"- Deleted: {result.deleted}")
    print(f"- Recovered mappings: {result.recovered}")
    print(f"- Unchanged: {result.unchanged}")
    print(f"- Skipped: {result.skipped}")
    print(f"- Failed: {result.failed}")


def add_sync_cli_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_state_file: str,
    legacy_positional_dates: bool = False,
) -> None:
    if legacy_positional_dates:
        parser.add_argument(
            "start_date",
            nargs="?",
            help="Legacy backfill start date",
        )
        parser.add_argument(
            "end_date",
            nargs="?",
            help="Legacy backfill end date",
        )
    parser.add_argument("--from", dest="flag_from", help="Backfill start date")
    parser.add_argument("--to", dest="flag_to", help="Backfill end date")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without writing destination data or sync state",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=f"State path (default: {default_state_file})",
    )
    parser.add_argument(
        "--user-email",
        default=None,
        help=(
            "Export only one TimeCamp user. Without --state-file, this uses a "
            "separate per-user state file."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Load exporter configuration from this dotenv file",
    )


def resolve_backfill_dates(
    args: argparse.Namespace,
) -> Tuple[Optional[date], Optional[date]]:
    positional_from = getattr(args, "start_date", None)
    positional_to = getattr(args, "end_date", None)
    has_positional = positional_from is not None or positional_to is not None
    has_flags = args.flag_from is not None or args.flag_to is not None
    if has_positional and has_flags:
        raise ValueError("Use positional dates or --from/--to, not both")
    raw_from = positional_from if has_positional else args.flag_from
    raw_to = positional_to if has_positional else args.flag_to
    if (raw_from is None) != (raw_to is None):
        raise ValueError("Both start and end dates must be provided")
    if raw_from is None:
        return None, None
    start = parse_iso_date(raw_from, "start date")
    end = parse_iso_date(raw_to, "end date")
    if start > end:
        raise ValueError("Start date must be before or equal to end date")
    return start, end
