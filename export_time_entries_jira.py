import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.jira_client import (
    JIRA_WORKLOG_PROPERTY_KEY,
    JiraClient,
    JiraIssueTarget,
    generate_jira_org_id,
    is_http_status,
    load_jira_instances,
    parse_jira_issue_external_id,
)
from src.jira_export_state import (
    JiraExportState,
    jira_target_key,
    parse_jira_target_key,
)
from src.time_entry_sync import (
    PreparedRemoteEntry,
    RecoveredRemoteEntry,
    SourceTimeEntry,
    SyncResult,
    SyncRunContext,
    TimeEntrySyncEngine,
    add_sync_cli_arguments,
    filtered_state_file as common_filtered_state_file,
    find_timecamp_user_id,
    parse_iso_date,
    parse_nonnegative_int,
    print_sync_summary,
    resolve_backfill_dates,
)
from src.time_entry_sync_state import SyncMapping
from src.timecamp_client import TimeCampClient


DEFAULT_STATE_FILE = "data/jira_time_entries_state.json"
MIN_JIRA_WORKLOG_SECONDS = 60
TIMECAMP_SERVER_TIMEZONE = ZoneInfo("Europe/Warsaw")
ExportResult = SyncResult


class JiraTimeEntryAdapter:
    adapter_id = "jira"
    target_name = "Jira worklog"
    minimum_duration_seconds = MIN_JIRA_WORKLOG_SECONDS

    def __init__(self, jira_clients: Dict[str, JiraClient]):
        if not jira_clients:
            raise ValueError("At least one Jira client is required")
        self.jira_clients = jira_clients
        self._remote_worklogs: Dict[Tuple[str, str], Dict[str, str]] = {}
        self._loaded_remote_worklog_keys = set()

    def prepare_run(self, context: SyncRunContext) -> None:
        return None

    def recovery_target(
        self,
        entry: SourceTimeEntry,
        task: Mapping[str, Any],
    ) -> Optional[str]:
        target = self._target_for_entry(entry, task)
        if target is None:
            return None
        return jira_target_key(target.instance_id, target.issue_key)

    def prepare_entry(
        self,
        entry: SourceTimeEntry,
        user: Optional[Mapping[str, Any]],
        task: Mapping[str, Any],
    ) -> Optional[PreparedRemoteEntry]:
        target = self._target_for_entry(entry, task)
        if target is None:
            return None
        if user is None:
            raise ValueError(
                f"TimeCamp user details not found for user {entry.user_id}"
            )

        update_payload = build_jira_worklog_payload(entry.raw, dict(user))
        create_payload = build_jira_worklog_payload(
            entry.raw,
            dict(user),
            entry_id=entry.entry_id,
        )
        return PreparedRemoteEntry(
            target_key=jira_target_key(target.instance_id, target.issue_key),
            create_payload=create_payload,
            update_payload=update_payload,
            source_fingerprint=worklog_payload_fingerprint(update_payload),
            target_label=target.issue_key,
        )

    def ineligible_reason(
        self,
        entry: SourceTimeEntry,
        task: Mapping[str, Any],
    ) -> str:
        return "task is not a Jira issue"

    def recover(
        self,
        entry: SourceTimeEntry,
        target_key: str,
    ) -> Optional[RecoveredRemoteEntry]:
        instance_id, issue_key = parse_jira_target_key(target_key)
        cache_key = (instance_id, issue_key)
        if cache_key not in self._loaded_remote_worklog_keys:
            client = self._client(instance_id)
            self._remote_worklogs[cache_key] = client.get_timecamp_worklog_map(
                issue_key
            )
            self._loaded_remote_worklog_keys.add(cache_key)
        worklog_id = self._remote_worklogs[cache_key].get(entry.entry_id)
        if worklog_id is None:
            return None
        return RecoveredRemoteEntry(
            remote_id=worklog_id,
            target_key=target_key,
        )

    def read(self, mapping: SyncMapping) -> Mapping[str, Any]:
        instance_id, issue_key = parse_jira_target_key(mapping.target_key)
        return self._client(instance_id).get_worklog(issue_key, mapping.remote_id)

    def fingerprint_remote(self, payload: Mapping[str, Any]) -> str:
        return worklog_payload_fingerprint(dict(payload))

    def create(self, prepared: PreparedRemoteEntry) -> str:
        instance_id, issue_key = parse_jira_target_key(prepared.target_key)
        response = self._client(instance_id).create_worklog(
            issue_key,
            dict(prepared.create_payload),
            adjust_estimate="leave",
        )
        worklog_id = str(response["id"])
        entry_id = _jira_property_entry_id(prepared.create_payload)
        cache_key = (instance_id, issue_key)
        if entry_id and cache_key in self._loaded_remote_worklog_keys:
            self._remote_worklogs[cache_key][entry_id] = worklog_id
        return worklog_id

    def update(
        self,
        mapping: SyncMapping,
        prepared: PreparedRemoteEntry,
    ) -> None:
        instance_id, issue_key = parse_jira_target_key(mapping.target_key)
        self._client(instance_id).update_worklog(
            issue_key,
            mapping.remote_id,
            dict(prepared.update_payload),
            adjust_estimate="leave",
        )

    def delete(self, mapping: SyncMapping) -> None:
        instance_id, issue_key = parse_jira_target_key(mapping.target_key)
        self._client(instance_id).delete_worklog(
            issue_key,
            mapping.remote_id,
            adjust_estimate="leave",
        )

    def is_missing(self, exc: BaseException) -> bool:
        return is_http_status(exc, 404)

    def describe_mapping(self, mapping: SyncMapping) -> str:
        _instance_id, issue_key = parse_jira_target_key(mapping.target_key)
        return f"Jira worklog {issue_key}/{mapping.remote_id}"

    def _target_for_entry(
        self,
        entry: SourceTimeEntry,
        task: Mapping[str, Any],
    ) -> Optional[JiraIssueTarget]:
        external_task_id = entry.external_task_id or task.get("external_task_id")
        target = parse_jira_issue_external_id(
            external_task_id,
            self.jira_clients.keys(),
        )
        if (
            target is None
            and any(
                str(external_task_id or "").startswith(prefix)
                for instance_id in self.jira_clients
                for prefix in (
                    f"{instance_id}_proj_",
                    f"sync_{instance_id}_proj_",
                )
            )
            and re.search(r"-\d+$", str(external_task_id))
        ):
            raise ValueError(f"Malformed Jira external task id: {external_task_id!r}")
        return target

    def _client(self, instance_id: str) -> JiraClient:
        client = self.jira_clients.get(instance_id)
        if client is None:
            raise ValueError(
                f"Jira instance {instance_id} from state is not configured"
            )
        return client


class JiraTimeEntryExporter:
    """Compatibility wrapper around the universal sync engine."""

    def __init__(
        self,
        timecamp_client: TimeCampClient,
        jira_clients: Dict[str, JiraClient],
        state: JiraExportState,
        *,
        dry_run: bool = False,
        today: Optional[date] = None,
        user_ids: Optional[List[int]] = None,
    ):
        self.adapter = JiraTimeEntryAdapter(jira_clients)
        self.engine = TimeEntrySyncEngine(
            timecamp_client,
            self.adapter,
            state.as_sync_state(),
            dry_run=dry_run,
            today=today,
            user_ids=user_ids,
        )
        self.result = self.engine.result

    def run(
        self,
        backfill_from: Optional[date] = None,
        backfill_to: Optional[date] = None,
    ) -> SyncResult:
        return self.engine.run(backfill_from, backfill_to)


def build_jira_worklog_payload(
    entry: Dict[str, Any],
    user: Dict[str, Any],
    *,
    entry_id: Optional[str] = None,
) -> Dict[str, Any]:
    duration = parse_nonnegative_int(
        entry.get("duration"),
        "duration",
        str(entry.get("id", "")),
    )
    payload: Dict[str, Any] = {
        "started": build_jira_started_at(entry, user),
        "timeSpentSeconds": duration,
        "comment": build_jira_comment(entry, user),
    }
    if entry_id is not None:
        payload["properties"] = [
            {
                "key": JIRA_WORKLOG_PROPERTY_KEY,
                "value": {"entryId": str(entry_id)},
            }
        ]
    return payload


def worklog_payload_fingerprint(payload: Dict[str, Any]) -> str:
    try:
        started = datetime.fromisoformat(str(payload["started"]))
        if started.tzinfo is None:
            raise ValueError("started timestamp has no UTC offset")
        normalized = {
            "started": started.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "timeSpentSeconds": int(payload["timeSpentSeconds"]),
            "comment": payload["comment"],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Cannot fingerprint incomplete Jira worklog payload: {payload!r}"
        ) from exc
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"v1:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def build_jira_started_at(entry: Dict[str, Any], user: Dict[str, Any]) -> str:
    entry_date = parse_iso_date(str(entry.get("date", "")), "entry date")
    try:
        entry_time = time.fromisoformat(str(entry.get("start_time", "")))
    except ValueError as exc:
        raise ValueError(
            f"Invalid start_time for TimeCamp entry {entry.get('id')}: "
            f"{entry.get('start_time')!r}"
        ) from exc

    try:
        user_offset_seconds = int(user["time_zone"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"TimeCamp user {user.get('user_id')} has no valid time_zone"
        ) from exc
    server_offset = datetime.combine(
        entry_date,
        entry_time,
        tzinfo=TIMECAMP_SERVER_TIMEZONE,
    ).utcoffset()
    if server_offset is None:
        raise ValueError("Cannot determine the TimeCamp server timezone offset")
    offset_seconds = int(server_offset.total_seconds()) + user_offset_seconds
    if abs(offset_seconds) > 14 * 60 * 60:
        raise ValueError(
            f"TimeCamp user {user.get('user_id')} has invalid time_zone "
            f"{user_offset_seconds}"
        )

    sign = "+" if offset_seconds >= 0 else "-"
    absolute_offset = abs(offset_seconds)
    offset_hours, remainder = divmod(absolute_offset, 3600)
    offset_minutes = remainder // 60
    return (
        f"{entry_date.isoformat()}T{entry_time.strftime('%H:%M:%S')}.000"
        f"{sign}{offset_hours:02d}{offset_minutes:02d}"
    )


def build_jira_comment(
    entry: Dict[str, Any],
    user: Dict[str, Any],
) -> Dict[str, Any]:
    display_name = str(user.get("display_name") or entry.get("user_name") or "").strip()
    email = str(user.get("email") or "").strip()
    if not display_name or not email:
        raise ValueError(
            f"TimeCamp user {user.get('user_id')} must have a display name and email"
        )

    paragraphs = [adf_paragraph(f"TimeCamp user: {display_name} <{email}>")]
    note = entry.get("description")
    if note not in (None, ""):
        note_text = (
            note if isinstance(note, str) else json.dumps(note, ensure_ascii=False)
        )
        paragraphs.extend(adf_paragraph(line) for line in note_text.split("\n"))
    return {"type": "doc", "version": 1, "content": paragraphs}


def adf_paragraph(text: str) -> Dict[str, Any]:
    paragraph: Dict[str, Any] = {"type": "paragraph", "content": []}
    if text:
        paragraph["content"].append({"type": "text", "text": text})
    return paragraph


def _jira_property_entry_id(payload: Mapping[str, Any]) -> Optional[str]:
    for prop in payload.get("properties", []):
        if prop.get("key") != JIRA_WORKLOG_PROPERTY_KEY:
            continue
        value = prop.get("value")
        if isinstance(value, Mapping) and value.get("entryId") is not None:
            return str(value["entryId"])
    return None


def filtered_state_file(email: str) -> str:
    return common_filtered_state_file("jira", email)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mirror TimeCamp time entries to Jira worklogs. The first run requires "
            "--from and --to; later runs use the saved modification cursor."
        )
    )
    add_sync_cli_arguments(
        parser,
        default_state_file=DEFAULT_STATE_FILE,
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_dotenv(dotenv_path=args.env_file, override=True)
    try:
        backfill_from, backfill_to = resolve_backfill_dates(args)

        timecamp_token = os.getenv("TIMECAMP_API_TOKEN", "").strip()
        if not timecamp_token:
            raise ValueError("TIMECAMP_API_TOKEN is not set")
        timecamp_client = TimeCampClient(timecamp_token)

        filtered_user_ids = None
        if args.user_email:
            filtered_user_ids = [
                find_timecamp_user_id(timecamp_client.get_users(), args.user_email)
            ]
            print(
                f"Restricting export to TimeCamp user {args.user_email} "
                f"(id {filtered_user_ids[0]})."
            )

        instances = load_jira_instances(os.getenv("JIRA_INSTANCES"))
        jira_clients = {
            generate_jira_org_id(instance["url"]): JiraClient(
                instance["url"],
                instance["email"],
                instance["token"],
            )
            for instance in instances
        }
        if args.state_file:
            state_path = Path(args.state_file)
        elif args.user_email:
            state_path = Path(filtered_state_file(args.user_email))
        else:
            state_path = Path(os.getenv("JIRA_EXPORT_STATE_FILE") or DEFAULT_STATE_FILE)
        state = JiraExportState.load(state_path)
        result = JiraTimeEntryExporter(
            timecamp_client,
            jira_clients,
            state,
            dry_run=args.dry_run,
            user_ids=filtered_user_ids,
        ).run(backfill_from, backfill_to)
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    print_sync_summary(result, dry_run=args.dry_run)
    return 0 if result.successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
