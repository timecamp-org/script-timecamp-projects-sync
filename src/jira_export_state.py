from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from src.time_entry_sync_state import SyncMapping, TimeEntrySyncState


LEGACY_STATE_VERSION = 1
TARGET_SEPARATOR = "|"


@dataclass(frozen=True)
class JiraWorklogMapping:
    instance_id: str
    issue_key: str
    worklog_id: str
    source_fingerprint: Optional[str] = None


class JiraExportState:
    """Compatibility facade over the universal v2 time-entry sync state."""

    def __init__(
        self,
        path: Any,
        cursor_date: Optional[str] = None,
        entries: Optional[Dict[str, JiraWorklogMapping]] = None,
        *,
        _sync_state: Optional[TimeEntrySyncState] = None,
    ):
        if _sync_state is not None:
            self._sync_state = _sync_state
        else:
            sync_entries = {
                str(entry_id): jira_mapping_to_sync(mapping)
                for entry_id, mapping in (entries or {}).items()
            }
            self._sync_state = TimeEntrySyncState(
                path,
                "jira",
                cursor_date,
                sync_entries,
            )

    @classmethod
    def load(cls, path: Any) -> "JiraExportState":
        try:
            sync_state = TimeEntrySyncState.load(
                path,
                "jira",
                legacy_version=LEGACY_STATE_VERSION,
                legacy_decoder=_decode_legacy_jira_state,
            )
        except ValueError as exc:
            if "Unsupported sync state version" in str(exc):
                raise ValueError(
                    str(exc).replace(
                        "Unsupported sync state version",
                        "Unsupported Jira export state version",
                    )
                ) from exc
            raise
        return cls(path, _sync_state=sync_state)

    @property
    def path(self) -> Path:
        return self._sync_state.path

    @property
    def cursor_date(self) -> Optional[str]:
        return self._sync_state.cursor_date

    @cursor_date.setter
    def cursor_date(self, value: Optional[str]) -> None:
        self._sync_state.cursor_date = value

    @property
    def entries(self) -> Dict[str, JiraWorklogMapping]:
        return {
            entry_id: sync_mapping_to_jira(mapping)
            for entry_id, mapping in self._sync_state.entries.items()
        }

    def get(self, entry_id: Any) -> Optional[JiraWorklogMapping]:
        mapping = self._sync_state.get(entry_id)
        return sync_mapping_to_jira(mapping) if mapping is not None else None

    def set(self, entry_id: Any, mapping: JiraWorklogMapping) -> None:
        self._sync_state.set(entry_id, jira_mapping_to_sync(mapping))

    def get_sync(self, entry_id: Any) -> Optional[SyncMapping]:
        return self._sync_state.get(entry_id)

    def set_sync(self, entry_id: Any, mapping: SyncMapping) -> None:
        self._sync_state.set(entry_id, mapping)

    def remove(self, entry_id: Any) -> None:
        self._sync_state.remove(entry_id)

    def save(self) -> None:
        self._sync_state.save()

    def as_sync_state(self) -> "JiraSyncStateView":
        return JiraSyncStateView(self)


class JiraSyncStateView:
    """Expose generic mappings while honoring facade save overrides in tests."""

    def __init__(self, state: JiraExportState):
        self.state = state

    @property
    def cursor_date(self) -> Optional[str]:
        return self.state.cursor_date

    @cursor_date.setter
    def cursor_date(self, value: Optional[str]) -> None:
        self.state.cursor_date = value

    def get(self, entry_id: Any) -> Optional[SyncMapping]:
        return self.state.get_sync(entry_id)

    def set(self, entry_id: Any, mapping: SyncMapping) -> None:
        self.state.set_sync(entry_id, mapping)

    def remove(self, entry_id: Any) -> None:
        self.state.remove(entry_id)

    def save(self) -> None:
        self.state.save()


def jira_target_key(instance_id: str, issue_key: str) -> str:
    normalized_instance = str(instance_id).strip()
    normalized_issue = str(issue_key).strip()
    if not normalized_instance or not normalized_issue:
        raise ValueError("Jira target requires an instance id and issue key")
    if TARGET_SEPARATOR in normalized_instance or TARGET_SEPARATOR in normalized_issue:
        raise ValueError("Jira target fields cannot contain '|'")
    return f"{normalized_instance}{TARGET_SEPARATOR}{normalized_issue}"


def parse_jira_target_key(target_key: str) -> tuple[str, str]:
    parts = str(target_key).split(TARGET_SEPARATOR, 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid Jira target key in state: {target_key!r}")
    return parts[0], parts[1]


def jira_mapping_to_sync(mapping: JiraWorklogMapping) -> SyncMapping:
    return SyncMapping(
        target_key=jira_target_key(mapping.instance_id, mapping.issue_key),
        remote_id=str(mapping.worklog_id),
        source_fingerprint=mapping.source_fingerprint,
    )


def sync_mapping_to_jira(mapping: SyncMapping) -> JiraWorklogMapping:
    instance_id, issue_key = parse_jira_target_key(mapping.target_key)
    return JiraWorklogMapping(
        instance_id=instance_id,
        issue_key=issue_key,
        worklog_id=mapping.remote_id,
        source_fingerprint=mapping.source_fingerprint,
    )


def _decode_legacy_jira_state(
    raw: Dict[str, Any],
    path: Path,
) -> Dict[str, SyncMapping]:
    raw_entries = raw.get("entries", {})
    if not isinstance(raw_entries, dict):
        raise ValueError(f"Invalid entries in Jira export state {path}")

    entries: Dict[str, SyncMapping] = {}
    for entry_id, mapping in raw_entries.items():
        if not isinstance(mapping, dict):
            raise ValueError(f"Invalid mapping for TimeCamp entry {entry_id} in {path}")
        instance_id = str(mapping.get("instance_id") or "").strip()
        issue_key = str(mapping.get("issue_key") or "").strip()
        worklog_id = str(mapping.get("worklog_id") or "").strip()
        if not instance_id or not issue_key or not worklog_id:
            raise ValueError(
                f"Incomplete mapping for TimeCamp entry {entry_id} in {path}"
            )
        fingerprint = mapping.get("source_fingerprint")
        if fingerprint is not None:
            fingerprint = str(fingerprint).strip()
            if not fingerprint:
                raise ValueError(
                    f"Invalid source_fingerprint for TimeCamp entry "
                    f"{entry_id} in {path}"
                )
        entries[str(entry_id)] = SyncMapping(
            target_key=jira_target_key(instance_id, issue_key),
            remote_id=worklog_id,
            source_fingerprint=fingerprint,
        )
    return entries
