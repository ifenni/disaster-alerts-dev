# src/disaster_alerts/providers/common.py
"""
Shared HTTP helpers and small utilities for provider modules.

Design goals
------------
- Minimal, dependency-light HTTP GET with retries and sane timeouts.
- Consistent User-Agent so public APIs can identify the app.
- Safe JSON handling (return {} on failure), with concise logging.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("providers.common")

DEFAULT_TIMEOUT = 15  # seconds
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF = 1.5  # exponential backoff base


def user_agent() -> str:
    return "disaster-alerts/0.1 (+https://example.invalid)"


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> Dict[str, Any]:
    """
    GET a JSON endpoint with small retry/backoff. Returns {} on failure.
    """
    h = {"User-Agent": user_agent(), "Accept": "application/geo+json, application/json"}
    if headers:
        h.update(headers)

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(url, params=params, headers=h, timeout=timeout)
            if resp.status_code == 304:
                log.debug("GET %s -> 304 Not Modified", url)
                return {}
            if 200 <= resp.status_code < 300:
                ctype = resp.headers.get("Content-Type", "")
                if "json" not in ctype:
                    log.warning("Expected JSON from %s but got Content-Type=%s", url, ctype)
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    log.error("Failed to decode JSON from %s", url)
                    return {}
            else:
                log.warning("GET %s -> %s", url, resp.status_code)
        except requests.RequestException as e:
            log.warning("GET %s failed: %s", url, e)

        if attempt > retries:
            break
        sleep_s = backoff ** attempt
        time.sleep(sleep_s)

    return {}
