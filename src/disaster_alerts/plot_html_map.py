"""
Plot interactive HTML map for the activated events.

- add detailed description later .
"""

from __future__ import annotations

# import argparse
# import shapely
# import logging
# import os
# import re
# import xml.etree.ElementTree as ET
# from datetime import datetime, timezone
# from pathlib import Path
# from typing import List, Optional, Tuple, Union
# from urllib.parse import urljoin
# from shapely.geometry import shape, Polygon
# from shapely import LinearRing, Point
# from lxml import etree
# from bs4 import BeautifulSoup
# import geopandas as gpd

# from bs4 import BeautifulSoup
# from lxml import etree

# from timezonefinder import TimezoneFinder
# from zoneinfo import ZoneInfo


import html
import logging
import re
import json
import requests
import folium
import itertools
from folium.features import GeoJson
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo
from shapely import LinearRing, Point, Polygon, wkt
from shapely.geometry import shape, box

from .providers.common import get_json
from .settings import Settings
from urllib.parse import urlparse

Event = Dict[str, Any]
log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# generate and save an intercative HTML map
# -----------------------------------------------------------------------------


def _is_url(s: str) -> bool:
    parsed = urlparse(s)
    return parsed.scheme in ("http", "https")


def _generate_events_html_map(
    settings: Settings,
    events: Dict[str, List[Event]],
    file_dir: Path,
):
    """
    Create an interactive map displaying activated events,
    grouped by routing key.
    """

    output_file = file_dir / "activated_events_map.html"

    # Find a centroid to center the map
    center = None
    for group_events in events.values():
        for e in group_events:
            centroid = e.get("centroid")
            if centroid is not None:
                center = [centroid.y, centroid.x]
                break
        if center:
            break

    if center is None:
        raise RuntimeError("No event has a centroid; cannot center map")

    US_CENTER = [39.8283, -98.5795]

    # Create map
    map_object = folium.Map(
        location=US_CENTER,
        zoom_start=5,
        tiles=None,
    )

    folium.TileLayer("Esri.WorldImagery", name="Satellite").add_to(map_object)
    folium.TileLayer("OpenStreetMap", name="OSM").add_to(map_object)

    # Color cycle for groups
    colors = itertools.cycle(
        ["red", "blue", "green", "purple", "orange", "darkred",
         "cadetblue", "darkgreen", "black"]
    )

    # Add grouped events
    for event_type, group_events in events.items():
        color = next(colors)

        feature_group = folium.FeatureGroup(
            name=f"{event_type} ({len(group_events)})",
            show=True,
        )

        for e in group_events:
            geom = e.get("aoi_polygon")
            if geom is None:
                log.debug("Event %s has no AOI geometry", e.get("id"))
                continue
            provider = str(e.get("provider", "")).upper()

            popup_html = "<br>".join(
                f"<b>{label}:</b> {value}"
                for label, value in [
                    ("Provider", provider),
                    ("Severity", e.get("severity")),
                    ("Description", e.get("title")),
                ]
                if value
            )
            GeoJson(
                data=geom.__geo_interface__,
                style_function=lambda _, c=color: {
                    "color": c,
                    "weight": 2,
                    "fillColor": c,
                    "fillOpacity": 0.35,
                },
                highlight_function=lambda _: {
                    "weight": 3,
                    "fillOpacity": 0.6,
                },
                popup=folium.Popup(popup_html, max_width=300),
            ).add_to(feature_group)

        feature_group.add_to(map_object)

    # Controls & save
    folium.LayerControl(collapsed=False).add_to(map_object)

    map_object.save(output_file)

    log.info("Event map written to %s", output_file)


def _bbox_to_geometry(bbox, timestamp_dir):
    if isinstance(bbox, str):
        bbox_clean = bbox.strip()
        bbox_upper = bbox_clean.upper()
        if bbox_upper.startswith(("POINT", "POLYGON")):
            geometry = wkt.loads(bbox_clean)
        else:
            # if URL, download
            if _is_url(bbox_clean):
                filename = "AOI_from_url.geojson"
                file_path = Path(timestamp_dir) / filename
                bbox_path = _download_url_to_file(
                        bbox_clean,
                        file_path)
            # if path (geojson)
            else:
                bbox_path = Path(bbox_clean)

            geometry = _geometry_from_file(bbox_path)
    else:
        lat_min, lat_max, lon_min, lon_max = bbox
        if lat_min == lat_max and lon_min == lon_max:
            geometry = Point(lon_min, lat_min)
        else:
            geometry = box(lon_min, lat_min, lon_max, lat_max)

    return geometry, geometry.bounds, geometry.centroid


def _add_aoi_to_events(
    events: Iterable[Event],
    file_dir: str,
) -> List[Event]:
    """
    Enrich events with AOI geometry derived from their link.
    Adds:
      - event["aoi_polygon"]
      - event["aoi"]
      - event["centroid"]
    """
    out: List[Event] = []
    for i, e in enumerate(events):
        event_type = e["properties"].get("event", "")
        if "Flood" in event_type:
            link = str(e.get("link"))
        elif "Storm" in event_type:
            affected_zones = e["properties"].get("affectedZones", [])
            link = str(affected_zones[0]) if affected_zones else ""
        if not link:
            log.debug("Event %s has no link; skipping AOI", e.get("id"))
            out.append(e)
            continue

        try:
            aoi_polygon, aoi, centroid = _bbox_to_geometry(
                link, file_dir)

            e["aoi_polygon"] = aoi_polygon
            e["aoi"] = aoi
            e["centroid"] = centroid

        except Exception as exc:
            log.warning(
                "Failed to build AOI for event %s (link=%r): %s",
                e.get("id"),
                link,
                exc,
            )
        out.append(e)
    return out


def _download_url_to_file(
    url: str,
    output_path: str | Path,
    timeout: int = 30,
    ensure_geojson: bool = True,
) -> Path:
    """
    Download a URL and save its content to a file (GeoJSON-safe).
    """
    output_path = Path(output_path)

    if ensure_geojson and output_path.suffix.lower() != ".geojson":
        output_path = output_path.with_suffix(".geojson")

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    # Parse JSON to ensure validity
    try:
        data = response.json()
    except ValueError as e:
        raise ValueError(f"Response from {url} is not valid JSON") from e

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return output_path


def _geometry_from_file(path: str | Path):
    """
    Read a geometry from a spatial file (KML or GeoJSON).
    """
    path = Path(path)
    suffix = path.suffix.lower()

    # # ---- KML ----
    # if suffix == ".kml":
    #     return create_polygon_from_kml(str(path))

    # ---- GeoJSON ----
    if suffix in (".geojson", ".json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # FeatureCollection
        if data["type"] == "FeatureCollection":
            geometries = [shape(f["geometry"]) for f in data["features"]]
            return geometries[0] if len(geometries) == 1 else gpd.GeoSeries(geometries).unary_union

        # Single geometry or Feature
        return shape(data.get("geometry", data))

    raise ValueError(f"Unsupported spatial file format: {path}")
