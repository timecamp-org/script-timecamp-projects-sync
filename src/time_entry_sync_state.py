import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


STATE_VERSION = 2


@dataclass(frozen=True)
class SyncMapping:
    target_key: str
    remote_id: str
    source_fingerprint: Optional[str] = None


LegacyStateDecoder = Callable[[Dict[str, Any], Path], Dict[str, SyncMapping]]


class TimeEntrySyncState:
    """Persistent cursor and one-to-one source/destination mappings."""

    def __init__(
        self,
        path: Any,
        adapter_id: str,
        cursor_date: Optional[str] = None,
        entries: Optional[Dict[str, SyncMapping]] = None,
    ):
        normalized_adapter_id = str(adapter_id).strip()
        if not normalized_adapter_id:
            raise ValueError("adapter_id cannot be empty")
        self.path = Path(path)
        self.adapter_id = normalized_adapter_id
        self.cursor_date = cursor_date
        self.entries = entries or {}

    @classmethod
    def load(
        cls,
        path: Any,
        adapter_id: str,
        *,
        legacy_version: Optional[int] = None,
        legacy_decoder: Optional[LegacyStateDecoder] = None,
    ) -> "TimeEntrySyncState":
        state_path = Path(path)
        if not state_path.exists():
            return cls(state_path, adapter_id)

        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read sync state {state_path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError(f"Sync state {state_path} must be a JSON object")

        version = raw.get("version")
        if version == legacy_version and legacy_decoder is not None:
            cursor_date = cls._validate_cursor(raw.get("cursor_date"), state_path)
            entries = legacy_decoder(raw, state_path)
            return cls(state_path, adapter_id, cursor_date, entries)
        if version != STATE_VERSION:
            raise ValueError(
                f"Unsupported sync state version in {state_path}: {version!r}"
            )

        stored_adapter = raw.get("adapter")
        if stored_adapter != adapter_id:
            raise ValueError(
                f"Sync state {state_path} belongs to adapter "
                f"{stored_adapter!r}, not {adapter_id!r}"
            )

        cursor_date = cls._validate_cursor(raw.get("cursor_date"), state_path)
        raw_entries = raw.get("entries", {})
        if not isinstance(raw_entries, dict):
            raise ValueError(f"Invalid entries in sync state {state_path}")

        entries: Dict[str, SyncMapping] = {}
        for entry_id, raw_mapping in raw_entries.items():
            entries[str(entry_id)] = cls._parse_mapping(
                entry_id,
                raw_mapping,
                state_path,
            )
        return cls(state_path, adapter_id, cursor_date, entries)

    @staticmethod
    def _validate_cursor(value: Any, path: Path) -> Optional[str]:
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Invalid cursor_date in sync state {path}")
        return value

    @staticmethod
    def _parse_mapping(
        entry_id: Any,
        raw_mapping: Any,
        path: Path,
    ) -> SyncMapping:
        if not isinstance(raw_mapping, dict):
            raise ValueError(f"Invalid mapping for TimeCamp entry {entry_id} in {path}")
        target_key = str(raw_mapping.get("target_key") or "").strip()
        remote_id = str(raw_mapping.get("remote_id") or "").strip()
        if not target_key or not remote_id:
            raise ValueError(
                f"Incomplete mapping for TimeCamp entry {entry_id} in {path}"
            )
        fingerprint = raw_mapping.get("source_fingerprint")
        if fingerprint is not None:
            fingerprint = str(fingerprint).strip()
            if not fingerprint:
                raise ValueError(
                    f"Invalid source_fingerprint for TimeCamp entry "
                    f"{entry_id} in {path}"
                )
        return SyncMapping(target_key, remote_id, fingerprint)

    def get(self, entry_id: Any) -> Optional[SyncMapping]:
        return self.entries.get(str(entry_id))

    def set(self, entry_id: Any, mapping: SyncMapping) -> None:
        self.entries[str(entry_id)] = mapping

    def remove(self, entry_id: Any) -> None:
        self.entries.pop(str(entry_id), None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "adapter": self.adapter_id,
            "cursor_date": self.cursor_date,
            "entries": {
                entry_id: asdict(mapping)
                for entry_id, mapping in sorted(self.entries.items())
            },
        }

        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temp_path, self.path)
        except OSError:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
            raise
