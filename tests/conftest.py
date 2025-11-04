# tests/conftest.py
"""
Shared pytest fixtures for the disaster-alerts test suite.

What you get:
- tmp_repo: a temporary repo-like directory structure (config/, data/, logs/)
- settings_factory: factory to build a minimal Settings object rooted at tmp_repo
- block_network (autouse): prevents accidental live HTTP during tests
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

from disaster_alerts.settings import (
    Settings,
    Thresholds,
    AppConfig,
    ProvidersConfig,
    Paths,
    EmailConfig,
    Recipients,
)


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a throwaway repo layout under a temp directory."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    # ensure state file exists
    (tmp_path / "data" / "state.json").write_text("{}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings_factory(tmp_repo: Path) -> Callable[..., Settings]:
    """
    Build a minimal Settings object rooted at tmp_repo without reading real YAML/.env.

    Usage:
        settings = settings_factory(recipients={"default": ["alerts@example.com"]},
                                    enable_nws=True, enable_usgs=True)
    """
    def _build(
        recipients: Optional[Dict[str, List[str]]] = None,
        enable_nws: bool = True,
        enable_usgs: bool = True,
        aoi: Optional[dict] = None,
    ) -> Settings:
        paths = Paths(
            root=tmp_repo,
            config_dir=tmp_repo / "config",
            data_dir=tmp_repo / "data",
            logs_dir=tmp_repo / "logs",
            state_file=tmp_repo / "data" / "state.json",
        )
        app = AppConfig(
            log_level="INFO",
            aoi=aoi,
            providers=ProvidersConfig(nws=enable_nws, usgs=enable_usgs),
        )
        # permissive defaults unless overridden in tests
        thresholds = Thresholds()
        rcpts = Recipients.from_raw(recipients or {"default": ["alerts@example.com"]})
        email = EmailConfig(user="sender@example.com", app_password="test-token")
        return Settings(paths=paths, app=app, thresholds=thresholds, recipients=rcpts, email=email)

    return _build


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """
    Fail fast if any test tries to perform a real HTTP request.

    Provider tests should monkeypatch `providers.common.get_json` or the provider's
    `fetch_events` function to return sample data instead of hitting the network.
    """
    import requests

    def _nope(*args, **kwargs):
        raise RuntimeError("Network access disabled in tests. Monkeypatch the provider HTTP call.")

    monkeypatch.setattr(requests, "get", _nope)
