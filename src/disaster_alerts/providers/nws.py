# src/disaster_alerts/providers/nws.py
"""
NWS Alerts provider.

Fetches active NWS alerts via api.weather.gov and maps them to the internal
Event shape consumed by the pipeline.

Notes
-----
- We fetch *active* alerts and let `rules.py` handle AOI filtering.
- Some alerts lack numeric thresholds; we still include them and rely on
  `rules.py` to be permissive for weather unless numeric values are present.
- Fields used:
    id            -> stable event id (feature.id or properties.id)
    updated       -> properties.effective or properties.onset or properties.sent
    title         -> properties.headline or properties.event
    severity      -> properties.severity
    link          -> properties.url or first entry in properties.references
    geometry      -> feature.geometry
    properties    -> whole properties dict (for optional metrics)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .common import get_json
from ..settings import Settings

log = logging.getLogger("providers.nws")

Event = Dict[str, Any]


def _pick(d: Dict[str, Any], *keys: str) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def fetch_events(settings: Settings) -> List[Event]:
    url = "https://api.weather.gov/alerts/active"
    # We could add params like status=actual;area=... but we rely on AOI later
    data = get_json(url)

    feats = data.get("features") or []
    if not isinstance(feats, list):
        return []

    out: List[Event] = []
    for f in feats:
        try:
            props = f.get("properties") or {}
            fid = f.get("id") or props.get("id") or props.get("@id")
            if not isinstance(fid, str):
                # Construct a fallback id from a couple of fields if necessary
                fid = str(props.get("id") or props.get("event") or props.get("headline") or "nws-unknown")

            updated = _pick(props, "effective", "onset", "sent", "updated", "ends")
            title = _pick(props, "headline", "event") or "(NWS Alert)"
            severity = props.get("severity") if isinstance(props.get("severity"), str) else None
            link = _pick(props, "url")

            ev: Event = {
                "id": fid,
                "provider": "nws",
                "updated": updated,
                "title": title,
                "severity": severity,
                "link": link,
                "geometry": f.get("geometry"),
                "properties": props,
                # Optional: route severe alerts differently, else default
                "routing_key": "severe" if (severity or "").lower() in {"severe", "extreme"} else "default",
            }
            out.append(ev)
        except Exception as e:
            log.debug("Skipping malformed NWS feature: %s", e)
            continue

    return out
