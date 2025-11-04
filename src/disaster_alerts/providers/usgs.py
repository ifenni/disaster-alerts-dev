# src/disaster_alerts/providers/usgs.py
"""
USGS Earthquakes provider.

Uses the USGS FDSN event API to retrieve recent earthquakes as GeoJSON and maps
to the internal Event format.

Strategy
--------
- Query a short time window (last 60 minutes) to keep payloads small; cron will
  run frequently and `state.py` performs dedup.
- Honor min_magnitude from thresholds if provided; default 2.5.
- Return normalized fields:
    id            -> feature.id (USGS ids are stable)
    updated       -> properties.updated or properties.time (ms since epoch -> ISO)
    title         -> properties.title (e.g., "M 3.1 - 10km SE of ...")
    severity      -> derived from magnitude bucket (Minor/Light/Moderate/Strong/...)
    link          -> properties.url
    geometry      -> feature.geometry (Point [lon, lat, depth_km])
    properties    -> full properties (we also add depth_km if geometry has Z)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .common import get_json
from ..settings import Settings

log = logging.getLogger("providers.usgs")

Event = Dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts_ms: Optional[int]) -> Optional[str]:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _severity_from_mag(mag: Optional[float]) -> Optional[str]:
    if mag is None:
        return None
    if mag < 3.0:
        return "Minor"
    if mag < 4.0:
        return "Light"
    if mag < 5.0:
        return "Moderate"
    if mag < 6.0:
        return "Strong"
    if mag < 7.0:
        return "Major"
    return "Great"


def fetch_events(settings: Settings) -> List[Event]:
    # Determine min magnitude from thresholds (fallback 2.5)
    minmag = 2.5
    try:
        if settings.thresholds.earthquake and settings.thresholds.earthquake.min_magnitude is not None:
            minmag = float(settings.thresholds.earthquake.min_magnitude)
    except Exception:
        pass

    # Window: last 60 minutes
    end = _utc_now()
    start = end - timedelta(minutes=60)

    url = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    params = {
        "format": "geojson",
        "orderby": "time",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "minmagnitude": f"{minmag:.1f}",
        "limit": "200",
    }

    data = get_json(url, params=params)
    feats = data.get("features") or []
    if not isinstance(feats, list):
        return []

    out: List[Event] = []
    for f in feats:
        try:
            fid = f.get("id") or ""
            props = f.get("properties") or {}
            geom = f.get("geometry")

            mag = props.get("mag")
            if isinstance(mag, (int, float)):
                mag = float(mag)
            else:
                mag = None

            # updated / time are ms since epoch
            updated = _iso(props.get("updated")) or _iso(props.get("time"))
            title = props.get("title") or (f"M {mag}" if mag is not None else "Earthquake")
            link = props.get("url") if isinstance(props.get("url"), str) else None

            # depth_km from geometry.coordinates[2]
            depth_km = None
            if isinstance(geom, dict):
                coords = geom.get("coordinates")
                if isinstance(coords, list) and len(coords) >= 3 and isinstance(coords[2], (int, float)):
                    depth_km = float(coords[2])
            # expose as properties.depth_km for rules convenience
            if depth_km is not None:
                props.setdefault("depth_km", depth_km)

            ev: Event = {
                "id": str(fid) if fid else title,
                "provider": "usgs",
                "updated": updated,
                "title": title,
                "severity": _severity_from_mag(mag),
                "link": link,
                "geometry": geom,
                "properties": props,
                "routing_key": "default",
            }
            out.append(ev)
        except Exception as e:
            log.debug("Skipping malformed USGS feature: %s", e)
            continue

    return out
