from disaster_alerts.rules import filter_events
from disaster_alerts.settings import EarthquakeThresholds, Thresholds, WeatherThresholds


def test_earthquake_threshold_min_mag():
    thresholds = Thresholds(
        earthquake=EarthquakeThresholds(min_magnitude=4.5, max_depth_km=700)
    )
    aoi = None

    events = [
        {
            "id": "eq1",
            "provider": "usgs",
            "geometry": {"type": "Point", "coordinates": [-120.0, 35.0, 5.0]},
            "properties": {"mag": 4.6, "depth_km": 5.0},
        },
        {
            "id": "eq2",
            "provider": "usgs",
            "geometry": {"type": "Point", "coordinates": [-120.0, 35.0, 5.0]},
            "properties": {"mag": 4.4, "depth_km": 5.0},
        },
    ]

    out = filter_events(events, thresholds, aoi)
    ids = {e["id"] for e in out}
    assert "eq1" in ids and "eq2" not in ids


def test_earthquake_max_depth():
    thresholds = Thresholds(
        earthquake=EarthquakeThresholds(min_magnitude=0.0, max_depth_km=50)
    )
    aoi = None

    events = [
        {
            "id": "shallow",
            "provider": "usgs",
            "geometry": {"type": "Point", "coordinates": [-120.0, 35.0, 10.0]},
            "properties": {"mag": 3.0, "depth_km": 10.0},
        },
        {
            "id": "deep",
            "provider": "usgs",
            "geometry": {"type": "Point", "coordinates": [-120.0, 35.0, 400.0]},
            "properties": {"mag": 6.0, "depth_km": 400.0},
        },
    ]
    out = filter_events(events, thresholds, aoi)
    ids = {e["id"] for e in out}
    assert "shallow" in ids and "deep" not in ids


def test_weather_thresholds_permissive_when_values_missing():
    # If weather values are absent, rules remain permissive (do not exclude)
    thresholds = Thresholds(
        weather=WeatherThresholds(wind_gust_mps=20, rainfall_mm_hr=10)
    )
    aoi = None

    events = [
        {
            "id": "nws-no-numerics",
            "provider": "nws",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            },
            "properties": {"event": "Severe Thunderstorm Warning"},
        }
    ]
    out = filter_events(events, thresholds, aoi)
    assert len(out) == 1 and out[0]["id"] == "nws-no-numerics"


def test_aoi_polygon_inclusion_and_exclusion():
    thresholds = Thresholds()
    aoi = {
        "type": "Polygon",
        "coordinates": [
            [[-122, 34], [-118, 34], [-118, 36], [-122, 36], [-122, 34]]  # simple box
        ],
    }

    inside = {
        "id": "in",
        "provider": "usgs",
        "geometry": {"type": "Point", "coordinates": [-120.0, 35.0, 5.0]},
        "properties": {"mag": 3.0},
    }
    outside = {
        "id": "out",
        "provider": "usgs",
        "geometry": {"type": "Point", "coordinates": [-110.0, 35.0, 5.0]},
        "properties": {"mag": 3.0},
    }

    out = filter_events([inside, outside], thresholds, aoi)
    ids = {e["id"] for e in out}
    assert "in" in ids and "out" not in ids


def test_aoi_missing_geometry_kept():
    thresholds = Thresholds()
    aoi = {
        "type": "Polygon",
        "coordinates": [[[-122, 34], [-118, 34], [-118, 36], [-122, 36], [-122, 34]]],
    }

    no_geom = {
        "id": "nogeom",
        "provider": "nws",
        "geometry": None,
        "properties": {"event": "Test Alert"},
    }

    out = filter_events([no_geom], thresholds, aoi)
    assert len(out) == 1 and out[0]["id"] == "nogeom"
