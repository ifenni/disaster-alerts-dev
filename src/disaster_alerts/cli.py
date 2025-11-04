# src/disaster_alerts/cli.py
"""
Command-line entrypoint for disaster-alerts.

Goals
-----
- Zero-config happy path: `python -m disaster_alerts` just runs the pipeline.
- Useful flags:
    --config-dir PATH      Override ./config (also via DISASTER_ALERTS_CONFIG_DIR)
    --root PATH            Override repo root (also via DISASTER_ALERTS_ROOT)
    --dry-run              Run full pipeline but DO NOT send emails
    --print-settings       Dump effective settings (redact secrets) and exit
    --version              Print version and exit
- Clear, friendly errors suitable for cron logs.

Exit codes
----------
0  success (no exceptions; even if 0 events)
1  configuration error (missing/invalid config or email creds when needed)
2  runtime/provider error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

try:
    from . import __version__ as PKG_VERSION  # type: ignore
except Exception:
    PKG_VERSION = None  # falls back to "0.0.0" when printing --version

from . import pipeline
from .settings import Settings


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="disaster-alerts",
        description="Cron-friendly alerts from NWS/USGS with yagmail notifications.",
    )
    p.add_argument(
        "--config-dir",
        type=Path,
        help="Override config directory (default: ./config or $DISASTER_ALERTS_CONFIG_DIR)",
    )
    p.add_argument(
        "--root",
        type=Path,
        help="Override repo root (default: inferred from package or $DISASTER_ALERTS_ROOT)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run end-to-end but do NOT send emails (logs only).",
    )
    p.add_argument(
        "--print-settings",
        action="store_true",
        help="Print effective settings (with secrets redacted) and exit.",
    )
    p.add_argument(
        "--version",
        action="store_true",
        help="Print version and exit.",
    )
    return p.parse_args(argv)


def _redact(obj: Dict[str, Any]) -> Dict[str, Any]:
    # Deep copy without JSON (handles Path, etc.)
    out = deepcopy(obj)
    email = out.get("email")
    if isinstance(email, dict) and email.get("app_password"):
        email["app_password"] = "********"
    return out


def _as_dict(settings: "Settings") -> Dict[str, Any]:
    def conv(v: Any) -> Any:
        # Order matters: handle Path first, then containers, then models.
        if isinstance(v, Path):
            return str(v)
        if isinstance(v, list):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        # pydantic v2 models
        try:
            dumped = v.model_dump()  # type: ignore[attr-defined]
            return conv(dumped)  # <— recurse to convert Paths inside
        except Exception:
            return v

    return {
        "paths": conv(settings.paths),
        "app": conv(settings.app),
        "thresholds": conv(settings.thresholds),
        # Recipients is a pydantic-like object; convert its __dict__ then recurse
        "recipients": conv(settings.recipients.__dict__),
        "email": conv(settings.email),
        "enabled_providers": settings.enabled_providers,
    }


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv or sys.argv[1:])

    if ns.version:
        ver = PKG_VERSION or "0.0.0"
        print(f"disaster-alerts {ver}")
        return 0

    # Environment overrides (allow CLI to take precedence)
    if ns.root:
        os.environ["DISASTER_ALERTS_ROOT"] = str(ns.root.expanduser())
    if ns.config_dir:
        os.environ["DISASTER_ALERTS_CONFIG_DIR"] = str(ns.config_dir.expanduser())

    # Load settings
    try:
        settings = Settings.load(
            root=ns.root.expanduser() if ns.root else None,
        )
    except Exception as e:
        print(f"[config] {e}", file=sys.stderr)
        return 1

    if ns.print_settings:
        data = _redact(_as_dict(settings))
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if ns.dry_run:
        # Monkey-patch email sending to no-op while still exercising build_message
        from . import email as email_mod

        real_send = email_mod.send

        def _noop_send(settings, recipients, subject, html_body, text_body):
            print("[dry-run] would send to:", ", ".join(recipients))
            print("[dry-run] subject:", subject)
            print("[dry-run] text preview (first 200 chars):")
            print(
                text_body[:200].replace("\n", " ")
                + ("..." if len(text_body) > 200 else "")
            )
            return None

        email_mod.send = _noop_send  # type: ignore[assignment]
        try:
            count = pipeline.run(settings)
        except Exception as e:
            # restore and re-raise path
            email_mod.send = real_send  # type: ignore[assignment]
            print(f"[runtime] {e}", file=sys.stderr)
            return 2
        finally:
            email_mod.send = real_send  # type: ignore[assignment]
        return 0

    # Normal run
    try:
        pipeline.run(settings)
    except Exception as e:
        print(f"[runtime] {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
