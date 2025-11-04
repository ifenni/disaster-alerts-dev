# src/disaster_alerts/pipeline.py
"""
Pipeline: fetch → filter → dedup → route → email → persist

This module wires the whole run. It is designed so the rest of the codebase can
plug into well-defined interfaces without having to modify this file later.

Interfaces locked here (implement the referenced functions/classes in their files):

providers.nws
    - fetch_events(settings: Settings) -> list[Event]

providers.usgs
    - fetch_events(settings: Settings) -> list[Event]

rules
    - filter_events(
          events: list[Event],
          thresholds: Thresholds,
          aoi: dict | None
      ) -> list[Event]

state
    - class State:
          @classmethod
          def load(path: Path) -> "State": ...
          def is_new(self, event: Event) -> bool: ...
          def update_with(self, events: list[Event]) -> None: ...
          def save(self) -> None: ...

email
    - def build_message(
          settings: Settings,
          events: list[Event],
          group_key: str
      ) -> tuple[str, str, str]:  # subject, html, text
    - def send(
          settings: Settings,
          recipients: list[str],
          subject: str,
          html_body: str,
          text_body: str
      ) -> None

Event contract (dict-like):
    Required keys:
      - "id": str                         # stable per provider
      - "provider": str                   # e.g., "nws", "usgs"
      - "updated": str | None             # ISO8601; used for watermarking (optional)
      - "title": str                      # short title/summary
      - "severity": str | None            # optional, free text
    Optional but recommended:
      - "routing_key": str                # maps to recipients.yaml key; default "default"
      - "link": str | None
      - "geometry": dict | None           # GeoJSON geometry
      - "properties": dict                # provider-specific fields

Logging:
    Respects settings.app.log_level and emits concise progress metrics.
"""

from __future__ import annotations

import importlib
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .settings import Settings, Thresholds

Event = Dict[str, Any]


# ------------------------ logging setup ------------------------


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ------------------------ providers dispatch ------------------------


_PROVIDER_MODULES = {
    "nws": "disaster_alerts.providers.nws",
    "usgs": "disaster_alerts.providers.usgs",
}


def _fetch_from_provider(provider: str, settings: Settings) -> List[Event]:
    mod_name = _PROVIDER_MODULES.get(provider)
    if not mod_name:
        logging.getLogger("pipeline").warning("Unknown provider '%s'—skipping.", provider)
        return []
    mod = importlib.import_module(mod_name)
    if not hasattr(mod, "fetch_events"):
        raise RuntimeError(f"Provider {provider} missing fetch_events(settings) implementation")
    events: List[Event] = mod.fetch_events(settings)  # type: ignore[attr-defined]
    # Normalize required fields; fail fast if missing
    for i, e in enumerate(events):
        if "id" not in e or "provider" not in e:
            raise RuntimeError(f"{provider} event #{i} missing required keys 'id'/'provider'")
        e.setdefault("routing_key", "default")
        e.setdefault("updated", None)
        e.setdefault("title", "")
        e.setdefault("severity", None)
        e.setdefault("link", None)
        e.setdefault("properties", {})
    return events


def _collect_events(settings: Settings) -> List[Event]:
    log = logging.getLogger("pipeline")
    all_events: List[Event] = []
    for prov in settings.enabled_providers:
        evs = _fetch_from_provider(prov, settings)
        log.debug("Fetched %d event(s) from %s", len(evs), prov)
        all_events.extend(evs)
    log.info("Fetched total %d event(s) from %d provider(s)", len(all_events), len(settings.enabled_providers))
    return all_events


# ------------------------ filtering & dedup ------------------------


def _apply_rules(
    events: List[Event],
    thresholds: Thresholds,
    aoi: Dict[str, Any] | None,
) -> List[Event]:
    from . import rules  # local import to avoid cycles at import time
    filtered = rules.filter_events(events, thresholds, aoi)
    return filtered


def _apply_dedup_and_update_state(
    events: List[Event],
    state_path: Path,
) -> List[Event]:
    from .state import State  # local import to avoid cycles
    state = State.load(state_path)
    new_events = [e for e in events if state.is_new(e)]
    state.update_with(new_events)
    state.save()
    return new_events


# ------------------------ routing & email ------------------------


def _group_by_routing_key(events: Iterable[Event], settings: Settings) -> Dict[str, List[Event]]:
    cfg = settings.app.routing
    groups: Dict[str, List[Event]] = defaultdict(list)
    for e in events:
        key = cfg.force_group or str(e.get("routing_key", "default")).strip() or "default"
        if key in set(cfg.drop_groups):
            continue
        if not cfg.force_group and key in cfg.merge:
            key = cfg.merge[key] or key
        groups[key].append(e)
    return groups


def _recipients_for_key(settings: Settings, key: str) -> List[str]:
    # Settings.Recipients implements get(key, default)
    recipients = settings.recipients.get(key, [])  # type: ignore[attr-defined]
    if not recipients and key != "default":
        # Fallback to 'default' if a specific key has no list
        recipients = settings.recipients.get("default", [])  # type: ignore[attr-defined]
    return recipients


def _dispatch_emails(settings: Settings, grouped: Dict[str, List[Event]]) -> Tuple[int, int]:
    """
    Send one email per group (routing key). Returns (groups_sent, events_notified).
    """
    from . import email as email_mod

    log = logging.getLogger("pipeline")
    settings.require_email()

    groups_sent = 0
    events_notified = 0

    for key, evs in grouped.items():
        if not evs:
            continue
        recipients = _recipients_for_key(settings, key)
        if not recipients:
            log.warning("No recipients configured for routing key '%s' (and no default fallback). Skipping.", key)
            continue

        subject, html_body, text_body = email_mod.build_message(settings, evs, key)
        email_mod.send(settings, recipients, subject, html_body, text_body)

        groups_sent += 1
        events_notified += len(evs)
        log.info("Sent %d event(s) to %d recipient(s) for group '%s'", len(evs), len(recipients), key)

    return groups_sent, events_notified


# ------------------------ public entrypoint ------------------------


def run(settings: Settings) -> int:
    """
    Execute one full pipeline run. Returns number of events notified.
    Raises on configuration or provider errors; logs operational metrics.

    Typical usage (in cli.py):
        settings = Settings.load()
        count = pipeline.run(settings)
        sys.exit(0 if count == 0 else 0)
    """
    _setup_logging(settings.app.log_level)
    log = logging.getLogger("pipeline")

    # 1) fetch
    events = _collect_events(settings)
    if not events:
        log.info("No events fetched.")
        return 0

    # 2) filter
    events = _apply_rules(events, settings.thresholds, settings.app.aoi)
    if not events:
        log.info("All events filtered out by rules.")
        return 0

    # 3) dedup
    events = _apply_dedup_and_update_state(events, settings.paths.state_file)
    if not events:
        log.info("No new events after deduplication.")
        return 0

    # 4) route
    grouped = _group_by_routing_key(events, settings)

    # 5) email
    try:
        groups_sent, events_notified = _dispatch_emails(settings, grouped)
    except RuntimeError as e:
        # Most likely missing email configuration; surface clearly and re-raise
        log.error("Notification failed: %s", e)
        raise

    log.info("Pipeline completed: %d group(s) emailed, %d event(s) notified.", groups_sent, events_notified)
    return events_notified
