from pathlib import Path
from typing import Any, Dict, List

from disaster_alerts import pipeline
from disaster_alerts.settings import (
    AppConfig,
    EmailConfig,
    Paths,
    ProvidersConfig,
    Recipients,
    Settings,
)

Event = Dict[str, Any]


class DummySettings(Settings):
    """Settings builder that avoids reading .env / YAML files."""

    @classmethod
    def build(
        cls,
        tmp_root: Path,
        recipients_map: Dict[str, List[str]] | None = None,
        enable_nws: bool = True,
        enable_usgs: bool = True,
    ) -> "Settings":
        paths = Paths(
            root=tmp_root,
            config_dir=tmp_root / "config",
            data_dir=tmp_root / "data",
            logs_dir=tmp_root / "logs",
            state_file=tmp_root / "data" / "state.json",
        )
        app = AppConfig(
            log_level="ERROR",  # quieter test output
            aoi=None,
            providers=ProvidersConfig(nws=enable_nws, usgs=enable_usgs),
        )
        recipients = Recipients.from_raw(
            recipients_map or {"default": ["alerts@example.com"]}
        )
        email = EmailConfig(user="sender@example.com", app_password="x-token")
        return cls(paths=paths, app=app, recipients=recipients, email=email)


def _fake_events_batch_1() -> List[Event]:
    return [
        {
            "id": "usgs-001",
            "provider": "usgs",
            "updated": "2025-11-03T10:00:00Z",
            "title": "M4.6 near Testville",
            "severity": "Moderate",
            "link": "https://earthquake.example/usgs-001",
            "geometry": {"type": "Point", "coordinates": [-120.0, 35.0, 8.0]},
            "properties": {"mag": 4.6, "depth_km": 8.0},
            "routing_key": "default",
        },
        {
            "id": "nws-xyz",
            "provider": "nws",
            "updated": "2025-11-03T10:05:00Z",
            "title": "Severe Thunderstorm Warning",
            "severity": "Severe",
            "link": "https://alerts.example/nws-xyz",
            "geometry": None,
            "properties": {"event": "Thunderstorm"},
            "routing_key": "ops",
        },
    ]


def _fake_events_batch_2() -> List[Event]:
    return [
        {
            "id": "usgs-001",
            "provider": "usgs",
            "updated": "2025-11-03T10:00:00Z",
            "title": "M4.6 near Testville",
            "severity": "Moderate",
            "link": "https://earthquake.example/usgs-001",
            "geometry": {"type": "Point", "coordinates": [-120.0, 35.0, 8.0]},
            "properties": {"mag": 4.6, "depth_km": 8.0},
            "routing_key": "default",
        },
        {
            "id": "usgs-002",
            "provider": "usgs",
            "updated": "2025-11-03T10:20:00Z",
            "title": "M3.8 near Sampletown",
            "severity": "Light",
            "link": "https://earthquake.example/usgs-002",
            "geometry": {"type": "Point", "coordinates": [-121.0, 36.0, 5.0]},
            "properties": {"mag": 3.8, "depth_km": 5.0},
            "routing_key": "default",
        },
    ]


def test_pipeline_happy_path_and_dedup(tmp_path: Path, monkeypatch):
    recipients_map = {"default": ["alerts@example.com"], "ops": ["ops@example.com"]}
    settings = DummySettings.build(tmp_path, recipients_map)

    settings.paths.data_dir.mkdir(parents=True, exist_ok=True)
    settings.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    # Optional explicit state init:
    # settings.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    # settings.paths.state_file.write_text("{}", encoding="utf-8")

    from disaster_alerts.providers import nws as nws_mod
    from disaster_alerts.providers import usgs as usgs_mod

    batch1 = _fake_events_batch_1()
    batch2 = _fake_events_batch_2()
    call_counter = {"count": 0}

    def fake_usgs_fetch(_settings):
        return [
            e
            for e in (
                batch1
                if call_counter["count"] == 0
                else batch2 if call_counter["count"] == 1 else []
            )
            if e["provider"] == "usgs"
        ]

    def fake_nws_fetch(_settings):
        return [
            e
            for e in (batch1 if call_counter["count"] == 0 else [])
            if e["provider"] == "nws"
        ]

    monkeypatch.setattr(usgs_mod, "fetch_events", fake_usgs_fetch)
    monkeypatch.setattr(nws_mod, "fetch_events", fake_nws_fetch)

    sent = []

    def fake_send(_settings, recipients, subject, html_body, text_body):
        sent.append(
            {
                "recipients": tuple(recipients),
                "subject": subject,
                "html": html_body,
                "text": text_body,
            }
        )

    from disaster_alerts import email as email_mod

    monkeypatch.setattr(email_mod, "send", fake_send)

    call_counter["count"] = 0
    assert pipeline.run(settings) == 2
    assert len(sent) == 2
    groups = {tuple(s["recipients"]) for s in sent}
    assert ("alerts@example.com",) in groups
    assert ("ops@example.com",) in groups

    sent.clear()
    call_counter["count"] = 1
    assert pipeline.run(settings) == 1
    assert len(sent) == 1
    assert "usgs-002" in sent[0]["text"]

    sent.clear()
    call_counter["count"] = 2
    assert pipeline.run(settings) == 0
    assert len(sent) == 0
