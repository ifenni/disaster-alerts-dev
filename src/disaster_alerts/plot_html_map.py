"""
Plot interactive HTML map for the activated events.

- add detailed description later .
"""

from __future__ import annotations

import colorsys
import hashlib
import ipaddress
import json
import logging
import socket
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

import requests

from .settings import Settings

Event = Dict[str, Any]
log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# generate and save an interactive HTML map
# -----------------------------------------------------------------------------

FAMILY_HUES = {
    "flood": 210 / 360,  # blue
    "hurricane": 120 / 360,  # green
    "storm": 10 / 360,  # red/orange
    "thunderstorm": 270 / 360,  # purple
}

TRUSTED_URL_SUFFIXES = (
    "weather.gov",
    "noaa.gov",
    "usgs.gov",
)
MAX_GEOJSON_BYTES = 2 * 1024 * 1024  # 2 MiB


def _is_url(s: str) -> bool:
    parsed = urlparse(s)
    return parsed.scheme in ("http", "https")


def _host_is_trusted(hostname: str) -> bool:
    host = hostname.lower().strip(".")
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in TRUSTED_URL_SUFFIXES
    )


def _host_resolves_public(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for _, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def _validate_remote_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only https URLs are allowed for AOI downloads")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if not _host_is_trusted(parsed.hostname):
        raise ValueError(f"Untrusted host for AOI download: {parsed.hostname}")
    if not _host_resolves_public(parsed.hostname):
        raise ValueError(f"Host resolves to a non-public address: {parsed.hostname}")


def _detect_family(event_type: str) -> str:
    s = event_type.lower()
    if "flood" in s:
        return "flood"
    if "hurricane" in s:
        return "hurricane"
    if "thunderstorm" in s:
        return "thunderstorm"
    # keep this order otherwise thunderstorm events
    # will be categorized as storm
    if "storm" in s:
        return "storm"
    return "storm"


def _color_from_event_type(event_type: str) -> str:
    family = _detect_family(event_type)
    base_hue = FAMILY_HUES[family]

    hash_hex = hashlib.md5(event_type.encode()).hexdigest()

    # Use more hash bits for stronger variation
    h_variation = int(hash_hex[:2], 16) / 255.0  # 0–1
    l_variation = int(hash_hex[2:4], 16) / 255.0  # 0–1

    # --- HUE variation (±15 degrees) ---
    hue_offset = (h_variation - 0.5) * (30 / 360)  # ±15°
    hue = (base_hue + hue_offset) % 1.0

    # --- LIGHTNESS variation (wide range) ---
    lightness = 0.35 + 0.35 * l_variation  # 0.35–0.70

    saturation = 0.75

    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)

    return "#{:02x}{:02x}{:02x}".format(
        int(r * 255),
        int(g * 255),
        int(b * 255),
    )


def _generate_events_html_map(
    settings: Settings,
    events: Dict[str, List[Event]],
    file_dir: Path,
):
    """
    Create an interactive map displaying activated events,
    grouped by routing key.
    """

    try:
        import folium
        from folium.features import GeoJson
    except ImportError as exc:
        raise RuntimeError(
            "Map generation requires optional dependencies (folium/shapely). "
            "Install with: pip install folium shapely"
        ) from exc

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
        location=center or US_CENTER,
        zoom_start=5,
        tiles=None,
    )

    folium.TileLayer("Esri.WorldImagery", name="Satellite").add_to(map_object)
    folium.TileLayer("OpenStreetMap", name="OSM").add_to(map_object)

    # Add grouped events
    for event_type, group_events in events.items():
        color = _color_from_event_type(event_type)
        color_box = (
            "<span "
            "style="
            "'display:inline-block; "
            "width:12px; "
            "height:12px; "
            f"background:{color}; "
            "margin-right:6px; "
            "border:1px solid #333;'"
            "></span>"
        )
        legend_label = f"{color_box}{event_type} ({len(group_events)})"
        feature_group = folium.FeatureGroup(
            name=legend_label,
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
                popup=folium.Popup(popup_html, max_width=350),
            ).add_to(feature_group)

        feature_group.add_to(map_object)

    # Controls & save
    folium.LayerControl(collapsed=False).add_to(map_object)

    map_object.save(output_file)

    log.info("Event map written to %s", output_file)


def _bbox_to_geometry(bbox, timestamp_dir):
    try:
        from shapely import Point, wkt
        from shapely.geometry import box
    except ImportError as exc:
        raise RuntimeError(
            "AOI geometry parsing requires optional dependency 'shapely'. "
            "Install with: pip install shapely"
        ) from exc

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
                bbox_path = _download_url_to_file(bbox_clean, file_path)
            else:
                raise ValueError("Local file paths are not allowed for event AOI sources")
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
    for e in events:
        props = e.get("properties")
        if not isinstance(props, dict):
            props = {}
        event_type = str(props.get("event") or "")
        link = ""
        if "Flood" in event_type:
            raw_link = e.get("link")
            link = str(raw_link).strip() if isinstance(raw_link, str) else ""
        elif "Storm" in event_type:
            affected_zones = props.get("affectedZones", [])
            link = str(affected_zones[0]) if affected_zones else ""
        if not link:
            log.debug("Event %s has no link; skipping AOI", e.get("id"))
            out.append(e)
            continue

        try:
            aoi_polygon, aoi, centroid = _bbox_to_geometry(link, file_dir)

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

    _validate_remote_url(url)

    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    ctype = (response.headers.get("Content-Type") or "").lower()
    if "json" not in ctype:
        raise ValueError(f"Expected JSON payload from {url}, got Content-Type={ctype}")

    payload = bytearray()
    for chunk in response.iter_content(chunk_size=16384):
        if not chunk:
            continue
        payload.extend(chunk)
        if len(payload) > MAX_GEOJSON_BYTES:
            raise ValueError(
                f"Response from {url} exceeded max size ({MAX_GEOJSON_BYTES} bytes)"
            )

    # Parse JSON to ensure validity
    try:
        data = json.loads(payload.decode("utf-8"))
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
    try:
        from shapely.geometry import shape
        from shapely.ops import unary_union
    except ImportError as exc:
        raise RuntimeError(
            "Reading geometry files requires optional dependency 'shapely'. "
            "Install with: pip install shapely"
        ) from exc

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
            return (
                geometries[0]
                if len(geometries) == 1
                else unary_union(geometries)
            )

        # Single geometry or Feature
        return shape(data.get("geometry", data))

    raise ValueError(f"Unsupported spatial file format: {path}")
