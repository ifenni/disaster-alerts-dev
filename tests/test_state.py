import json
from pathlib import Path

from disaster_alerts.state import State


def test_state_load_empty(tmp_path: Path):
    state_path = tmp_path / "state.json"
    s = State.load(state_path)
    assert s.providers == {}
    assert s.path == state_path


def test_is_new_and_update_with(tmp_path: Path):
    state_path = tmp_path / "state.json"
    s = State.load(state_path)

    ev1 = {"id": "A1", "provider": "usgs", "updated": "2025-01-01T00:00:00Z"}
    ev2 = {"id": "A2", "provider": "usgs", "updated": "2025-01-01T00:05:00Z"}
    ev3 = {"id": "B1", "provider": "nws", "updated": "2025-01-01T01:00:00Z"}

    assert s.is_new(ev1)
    assert s.is_new(ev2)
    assert s.is_new(ev3)

    s.update_with([ev1, ev2, ev3])
    assert not s.is_new(ev1)
    assert not s.is_new(ev2)
    assert not s.is_new(ev3)

    # Check watermark advanced per provider
    assert s.providers["usgs"].last_updated == "2025-01-01T00:05:00Z"
    assert s.providers["nws"].last_updated == "2025-01-01T01:00:00Z"


def test_save_and_reload(tmp_path: Path):
    state_path = tmp_path / "state.json"
    s = State.load(state_path)

    ev = {"id": "X1", "provider": "nws", "updated": "2025-02-02T02:02:02Z"}
    s.update_with([ev])
    s.save()

    # File exists and is valid JSON
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert "providers" in data

    # Reload preserves content
    s2 = State.load(state_path)
    assert not s2.is_new(ev)
    assert s2.providers["nws"].last_updated == "2025-02-02T02:02:02Z"


def test_lru_limit(tmp_path: Path, monkeypatch):
    # Force a small LRU for test
    monkeypatch.setenv("DISASTER_ALERTS_STATE_LRU", "3")
    state_path = tmp_path / "state.json"
    s = State.load(state_path)

    # Add 4 ids for same provider; oldest should drop
    events = [{"id": f"E{i}", "provider": "usgs"} for i in range(4)]
    s.update_with(events)
    ids = s.providers["usgs"].ids
    assert ids == ["E3", "E2", "E1"]  # E0 evicted
