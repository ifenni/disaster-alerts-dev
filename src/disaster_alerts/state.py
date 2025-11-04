# src/disaster_alerts/state.py
"""
State management for deduplication and watermarks.

Goals
-----
- Remember which events we've already notified about (by stable event `id`)
- Optionally track the latest `updated` ISO8601 timestamp per provider as a
  watermark to short-circuit polling.
- Be robust for cron usage (atomic writes; tolerate missing/corrupt files).
- Keep memory bounded via per-provider LRU caps.

Data model (JSON on disk)
-------------------------
{
  "version": 1,
  "providers": {
    "nws": {
      "ids": ["abc", "def", ...],           # most recent first (LRU)
      "last_updated": "2025-10-01T12:34:56Z"
    },
    "usgs": {
      "ids": [...],
      "last_updated": null
    }
  }
}

Public API
----------
State.load(path: Path) -> State
state.is_new(event: dict) -> bool
state.update_with(events: list[dict]) -> None
state.save() -> None

Notes
-----
- This module is self-contained and does not import Settings to avoid cycles.
- ISO8601 parsing is conservative but supports common formats with/without 'Z'.
- Writes are atomic: write to temp file in same directory then `replace()`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable

# --------------------------- helpers ---------------------------


def _parse_iso8601(ts: Optional[str]) -> Optional[Tuple[int, int, int, int, int, int]]:
    """
    Parse a subset of ISO8601 into a tuple comparable via Python's tuple ordering.
    Returns None if input is None/empty/invalid.

    Supported examples:
      "2025-10-29T21:36:47Z"
      "2025-10-29T21:36:47.776Z"
      "2025-10-29T21:36:47"
      "2025-10-29T21:36:47+00:00"  (offsets are ignored; treated as UTC)
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Strip timezone if present; we treat everything as UTC for ordering
        t = ts
        if t.endswith("Z"):
            t = t[:-1]
        if "+" in t:
            t = t.split("+", 1)[0]
        if "-" in t and "T" in t:
            date, time = t.split("T", 1)
            y, m, d = (int(x) for x in date.split("-"))
            # Drop subseconds if present
            if "." in time:
                time = time.split(".", 1)[0]
            hh, mm, ss = (int(x) for x in time.split(":"))
            return (y, m, d, hh, mm, ss)
        return None
    except Exception:
        return None


def _is_newer(a: Optional[str], b: Optional[str]) -> bool:
    """
    True if timestamp `a` is strictly newer than `b`. None is treated as lowest.
    """
    pa, pb = _parse_iso8601(a), _parse_iso8601(b)
    if pa is None:
        return False
    if pb is None:
        return True
    return pa > pb


# --------------------------- core types ---------------------------

DEFAULT_LRU_LIMIT = 5000


def _env_lru_limit() -> int:
    """Read current env at runtime for testability (monkeypatch-friendly)."""
    return int(os.environ.get("DISASTER_ALERTS_STATE_LRU", str(DEFAULT_LRU_LIMIT)))


@dataclass
class _ProviderState:
    ids: List[str] = field(default_factory=list)  # most recent first
    last_updated: Optional[str] = None

    def remember(self, event_id: str, maxlen: int) -> None:
        """Insert ID at front (most-recent-first) and cap length."""
        if event_id in self.ids:
            self.ids.remove(event_id)
        self.ids.insert(0, event_id)
        if len(self.ids) > maxlen:
            # drop oldest beyond maxlen
            del self.ids[maxlen:]

    def add_id(self, eid: str, lru_limit: int) -> None:
        # De-dup inside list while keeping "most recent first"
        if self.ids and self.ids[0] == eid:
            return
        try:
            self.ids.remove(eid)
        except ValueError:
            pass
        self.ids.insert(0, eid)
        if len(self.ids) > lru_limit:
            del self.ids[lru_limit:]

    def consider_updated(self, updated: Optional[str]) -> None:
        if updated and _is_newer(updated, self.last_updated):
            self.last_updated = updated


@dataclass
class State:
    path: Path
    version: int = 1
    providers: Dict[str, _ProviderState] = field(default_factory=dict)
    lru_limit: int = field(default_factory=_env_lru_limit)

    # ---------------------- construction ----------------------

    @classmethod
    def load(cls, path: Path) -> "State":
        """
        Load state from JSON file. If missing or corrupt, return an empty state.
        Ensures parent directory exists.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            version = int(data.get("version", 1))
            providers_raw = data.get("providers", {})
            providers: Dict[str, _ProviderState] = {}
            for name, obj in providers_raw.items():
                ids = obj.get("ids") or []
                if not isinstance(ids, list):
                    ids = []
                ids = [s for s in ids if isinstance(s, str)]
                last_updated = obj.get("last_updated")
                if not isinstance(last_updated, str):
                    last_updated = None
                providers[name] = _ProviderState(ids=ids, last_updated=last_updated)
            if "lru_limit" in data:
                lru_limit = int(data["lru_limit"])
            else:
                # No value persisted on disk: use current env at runtime
                lru_limit = _env_lru_limit()
            
            return cls(path=path, version=version, providers=providers, lru_limit=lru_limit)
        except Exception:
            # Corrupt file; back it up and start fresh
            try:
                path.rename(path.with_suffix(".json.bak"))
            except Exception:
                pass
            return cls(path=path)

    # ---------------------- query / update ----------------------

    def _prov(self, name: str) -> _ProviderState:
        ps = self.providers.get(name)
        if ps is None:
            ps = _ProviderState()
            self.providers[name] = ps
        return ps

    def is_new(self, event: Dict[str, Any]) -> bool:
        """
        Return True if event has not been seen before.
        Criteria:
          1) Event id not in provider LRU list
          2) (Optional) If provider watermark exists and event.updated <= watermark,
             we *still* check id; watermark is advisory only (in case of late arrivals).
        """
        provider = str(event.get("provider", "")).strip() or "unknown"
        eid = str(event.get("id", "")).strip()
        if not eid:
            # If an event has no id, treat as notifiable (can't dedup safely)
            return True
        ps = self._prov(provider)
        return eid not in ps.ids

    def update_with(self, events: List[Dict[str, Any]]) -> None:
        """
        Update internal state with a batch of events that were notified.
        - Adds each event id to provider LRU.
        - Advances provider last_updated watermark to max(updated) in this batch.
        """
        # Group by provider to compute per-provider max(updated)
        per_provider_max_updated: Dict[str, Optional[str]] = {}
        for e in events:
            provider = str(e.get("provider", "")).strip() or "unknown"
            eid = str(e.get("id", "")).strip()
            updated = e.get("updated")
            ps = self._prov(provider)
            if eid:
                ps.add_id(eid, self.lru_limit)
            # track max(updated) per provider
            prev = per_provider_max_updated.get(provider)
            per_provider_max_updated[provider] = updated if _is_newer(updated, prev) else prev

        # Apply watermarks
        for provider, new_max in per_provider_max_updated.items():
            if new_max:
                self._prov(provider).consider_updated(new_max)

    # ---------------------- persistence ----------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "lru_limit": self.lru_limit,
            "providers": {
                name: {
                    "ids": ps.ids,
                    "last_updated": ps.last_updated,
                }
                for name, ps in self.providers.items()
            },
        }

    def save(self) -> None:
        """
        Atomically write state JSON. Writes to a temp file in the same directory
        then replaces the target to avoid partial writes on crash.
        """
        tmp = self.path.with_suffix(".json.tmp")
        data = self.to_dict()
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
