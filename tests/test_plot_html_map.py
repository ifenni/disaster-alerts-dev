from pathlib import Path

import pytest

from disaster_alerts import plot_html_map as map_mod


def test_validate_remote_url_rejects_non_https(monkeypatch):
    monkeypatch.setattr(map_mod, "_host_resolves_public", lambda _host: True)
    with pytest.raises(ValueError, match="https"):
        map_mod._validate_remote_url("http://api.weather.gov/zones/forecast/CAZ041")


def test_validate_remote_url_rejects_untrusted_host(monkeypatch):
    monkeypatch.setattr(map_mod, "_host_resolves_public", lambda _host: True)
    with pytest.raises(ValueError, match="Untrusted host"):
        map_mod._validate_remote_url("https://example.com/zone.geojson")


def test_add_aoi_to_events_skips_non_storm_flood_without_crashing(tmp_path: Path):
    events = [
        {
            "id": "nws-1",
            "provider": "nws",
            "properties": {"event": "Heat Advisory"},
            "title": "Heat Advisory",
        }
    ]
    out = map_mod._add_aoi_to_events(events, str(tmp_path))
    assert len(out) == 1
    assert "aoi_polygon" not in out[0]


def test_add_aoi_to_events_storm_uses_first_affected_zone(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    def fake_bbox_to_geometry(link: str, _file_dir: str):
        calls.append(link)
        return "geom", (1, 2, 3, 4), "centroid"

    monkeypatch.setattr(map_mod, "_bbox_to_geometry", fake_bbox_to_geometry)

    events = [
        {
            "id": "nws-2",
            "provider": "nws",
            "properties": {
                "event": "Severe Thunderstorm Warning",
                "affectedZones": ["https://api.weather.gov/zones/forecast/CAZ041"],
            },
            "title": "Severe Thunderstorm Warning",
        }
    ]
    out = map_mod._add_aoi_to_events(events, str(tmp_path))
    assert calls == ["https://api.weather.gov/zones/forecast/CAZ041"]
    assert out[0]["aoi_polygon"] == "geom"
