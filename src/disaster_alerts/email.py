# src/disaster_alerts/email.py
"""
Email notifications via yagmail.

This module is self-contained and production-ready:

- Builds one message per routing group with clear subject lines.
- Renders both plaintext and HTML (from templates) with a tiny, safe templater.
- Sends mail via yagmail with the creds provided in Settings (env-based).
- Adds minimal error handling and logging suitable for cron runs.

Public API
----------
build_message(settings: Settings, events: list[Event], group_key: str)
    -> tuple[str, str, str]  # subject, html, text

send(settings: Settings, recipients: list[str], subject: str, html_body: str,
     text_body: str) -> None
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yagmail

from .providers.common import get_json
from .settings import Settings

Event = dict[str, Any]

log = logging.getLogger(__name__)

# ------------------------ tiny template loader ------------------------


def _templates_dir() -> Path:
    """Return the templates directory located alongside the package."""
    return Path(__file__).resolve().parent / "templates"


def _read_template(name: str) -> str:
    """Read a template file, falling back to a minimal built-in template."""
    path = _templates_dir() / name
    if not path.exists():
        if name.endswith(".html"):
            return (
                "<html><body><h3>{{ subject }}</h3>"
                "<pre>{{ body }}</pre></body></html>"
            )
        return "{{ subject }}\n\n{{ body }}\n"
    return path.read_text(encoding="utf-8")


def _render(template: str, context: dict[str, str]) -> str:
    """Very small double-curly replacement; no code execution."""
    out = template
    for key, val in context.items():
        out = out.replace(f"{{{{ {key} }}}}", val)
        out = out.replace(f"{{{{{key}}}}}", val)  # tolerate missing space
    return out


def _to_wkt(geom: dict | None) -> str | None:
    """Convert a GeoJSON geometry (Point/Polygon/MultiPolygon) into WKT."""
    if not isinstance(geom, dict):
        return None

    gtype = geom.get("type")
    coords = geom.get("coordinates")
    try:
        if gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
            x, y = float(coords[0]), float(coords[1])
            return f"POINT ({x} {y})"

        if gtype == "Polygon" and isinstance(coords, list) and coords:
            # Outer ring only; holes could be added with `), (`
            ring = coords[0]
            pairs = ", ".join(f"{float(x)} {float(y)}" for x, y, *_ in ring)
            return f"POLYGON (({pairs}))"

        if gtype == "MultiPolygon" and isinstance(coords, list) and coords:
            # First polygon only to avoid huge payloads; expand if desired
            poly = coords[0]
            ring = poly[0]
            pairs = ", ".join(f"{float(x)} {float(y)}" for x, y, *_ in ring)
            return f"MULTIPOLYGON ((({pairs})))"
    except Exception:
        return None

    return None


def _wkt_for_event(event: dict) -> str | None:
    """
    Prefer resolving WKT by fetching the event URL JSON (NWS/USGS),
    and only fall back to the event's in-memory geometry if URL is
    missing/unusable.
    """
    # 1) Try fetching from URL first.
    link = event.get("link") or event.get("id")
    if isinstance(link, str) and link.startswith("http"):

        try:
            data = get_json(link)
        except Exception as exc:
            # Tests disable network; in production we also don't want a hard fail.
            # Log and fall back to any in-memory geometry.
            log.debug("Skipping WKT fetch for %s: %s", link, exc)
            data = {}

        # Direct geometry
        if isinstance(data.get("geometry"), dict):
            wkt = _to_wkt(data["geometry"])
            if wkt:
                return wkt

        # Feature / FeatureCollection fallbacks
        if data.get("type") == "Feature" and isinstance(data.get("geometry"), dict):
            wkt = _to_wkt(data["geometry"])
            if wkt:
                return wkt

        if data.get("type") == "FeatureCollection":
            feats = data.get("features") or []
            if feats and isinstance(feats[0], dict):
                wkt = _to_wkt(feats[0].get("geometry"))
                if wkt:
                    return wkt

    # 2) Fallback to event geometry if URL was missing or didn’t yield geometry.
    return _to_wkt(event.get("geometry"))


# ------------------------ formatting helpers ------------------------

_SEV_RANK = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1, "none": 0}


def _sev_rank(severity: str | None) -> int:
    """Map severity string to a rank for sorting."""
    return _SEV_RANK.get((severity or "").strip().lower(), 0)


def _tz(settings: Settings) -> ZoneInfo:
    """Return display timezone from settings, defaulting to UTC."""
    tzname = getattr(settings.app, "display_timezone", None) or "UTC"
    try:
        return ZoneInfo(tzname)
    except Exception:
        return ZoneInfo("UTC")


def _pick_time(props: dict, *keys: str) -> str | None:
    """Pick the first non-empty string value among given keys."""
    for key in keys:
        val = props.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _to_dt(timestr: str | None) -> datetime | None:
    """Parse ISO-8601 string (supports 'Z') to aware datetime."""
    if not timestr:
        return None
    try:
        return datetime.fromisoformat(timestr.replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_local(dt: datetime | None, tz: ZoneInfo) -> str:
    """Format datetime in local timezone."""
    if not dt:
        return "—"
    return dt.astimezone(tz).strftime("%b %-d, %H:%M")


def _time_left(expires: datetime | None, now: datetime) -> str:
    """Return a compact '(Xd Yh left)' countdown string."""
    if not expires:
        return ""
    delta = expires - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "(ended)"
    hours_total = secs // 3600
    days, hours = divmod(hours_total, 24)
    minutes = (secs % 3600) // 60
    if days:
        return f"({days}d {hours}h left)"
    if hours:
        return f"({hours}h {minutes}m left)"
    return f"({minutes}m left)"


def _format_text_lines(events: Iterable[Event], settings: Settings) -> list[str]:
    """Render plaintext rows for events list (deduped and sorted)."""

    def key_tuple(event: Event) -> tuple:
        props = event.get("properties") or {}
        onset = _to_dt(_pick_time(props, "onset", "effective", "sent"))
        expires = _to_dt(_pick_time(props, "expires", "ends"))
        office = props.get("senderName") or ""
        return (
            event.get("title") or props.get("event") or "",
            office,
            onset or "",
            expires or "",
        )

    # Deduplicate by key_tuple while preserving last occurrence.
    unique: dict[tuple, Event] = {}
    for ev in events:
        unique[key_tuple(ev)] = ev
    events_dedup = list(unique.values())

    # Sort by severity desc, then by expires asc (far future last).
    events_dedup.sort(
        key=lambda ev: (
            -_sev_rank(str(ev.get("severity"))),
            _to_dt(_pick_time((ev.get("properties") or {}), "expires", "ends"))
            or datetime.max.replace(tzinfo=timezone.utc),
        )
    )

    tz = _tz(settings)
    now_local = datetime.now(timezone.utc).astimezone(tz)

    out: list[str] = []
    for idx, ev in enumerate(events_dedup, 1):
        props = ev.get("properties") or {}
        title = str(props.get("event") or ev.get("title") or "(untitled)")
        office = str(props.get("senderName") or "").strip()
        area = str(props.get("areaDesc") or "").strip()
        onset = _to_dt(_pick_time(props, "onset", "effective", "sent"))
        expires = _to_dt(_pick_time(props, "expires", "ends"))
        sev = (str(ev.get("severity") or "").strip()) or "—"
        cert = (str(props.get("certainty") or "").strip()) or "—"
        urg = (str(props.get("urgency") or "").strip()) or "—"
        link = str(ev.get("link") or ev.get("id") or "").strip()

        head = f"{idx}) {title}"
        meta_parts = [office or None, area or None]
        meta = " — ".join(part for part in meta_parts if part)
        when = (
            f"When: {_fmt_local(onset, tz)} → {_fmt_local(expires, tz)}  "
            f"{_time_left(expires, now_local)}"
        )
        sevline = f"Severity: {sev} • Certainty: {cert} • Urgency: {urg}"
        url = f"URL: {link}"

        row = [head]
        if meta:
            row.append(f"   {meta}")
        row.append(f"   {when}")
        row.append(f"   {sevline}")
        row.append(f"   {url}")

        # Include WKT (from geometry or by fetching the event URL)
        wkt = _wkt_for_event(ev)
        if wkt:
            trimmed = (wkt[:600] + "…") if len(wkt) > 600 else wkt
            row.append(f"   WKT: {trimmed}")

        out.append("\n".join(row))
        out.append("")  # spacer

    return out


def _now_utc_iso() -> str:
    """Current time in UTC in compact ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _subject_for_group(group_key: str, events: list[Event]) -> str:
    """Compose a concise subject summarizing alert types and counts."""
    from collections import Counter

    titles: list[str] = []
    for ev in events:
        props = ev.get("properties") or {}
        title = str(props.get("event") or ev.get("title") or "")
        titles.append(title.split(" issued", 1)[0])

    counts = Counter(titles)
    types = ", ".join(f"{k} ×{v}" for k, v in counts.most_common())
    total = len(events)
    plural = "" if total == 1 else "s"
    # Generic across providers and contains "new event" (tests look for this substring)
    return f"[disaster-alerts] {total} new event{plural} — {types}  ({group_key})"


def _format_html_rows(events: Iterable[Event]) -> str:
    """Build table rows for the HTML body, including a WKT details row."""
    rows: list[str] = []

    for ev in events:
        title = html.escape(str(ev.get("title") or "").strip() or "(untitled)")
        provider = html.escape(str(ev.get("provider") or "unknown").upper())
        severity = html.escape(str(ev.get("severity") or "").strip())
        updated = html.escape(str(ev.get("updated") or "").strip())
        link = str(ev.get("link") or "").strip()
        eid = html.escape(str(ev.get("id") or "").strip())

        link_html = f'<a href="{html.escape(link)}">link</a>' if link else ""
        sev_cell = severity or "&nbsp;"
        upd_cell = updated or "&nbsp;"

        rows.append(
            (
                "<tr>"
                f"<td>{provider}</td>"
                f"<td>{title}</td>"
                f"<td>{sev_cell}</td>"
                f"<td>{upd_cell}</td>"
                f"<td>{link_html}</td>"
                f"<td><code>{eid}</code></td>"
                "</tr>"
            )
        )

        wkt = _wkt_for_event(ev)
        if wkt:
            trimmed = (wkt[:600] + "…") if len(wkt) > 600 else wkt
            rows.append(
                (
                    "<tr>"
                    "<td colspan='6' "
                    "style='font-family:monospace;font-size:12px;white-space:nowrap;"
                    "overflow:auto;'>"
                    f"<strong>WKT:</strong> {html.escape(trimmed)}"
                    "</td>"
                    "</tr>"
                )
            )

    return "\n".join(rows)


def _build_bodies(
    subject: str,
    events: list[Event],
    settings: Settings,
) -> tuple[str, str]:
    """Build HTML and plaintext bodies for the email."""
    # plaintext
    lines = _format_text_lines(events, settings)
    text_body = "\n".join(lines)

    # html table
    rows_html = _format_html_rows(events)
    table = (
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;'>"
        "<thead><tr>"
        "<th>Provider</th><th>Title</th><th>Severity</th>"
        "<th>Updated</th><th>Link</th><th>ID</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
    )

    html_body = table
    return html_body, text_body


# ------------------------ public API ------------------------


def build_message(
    settings: Settings,
    events: list[Event],
    group_key: str,
) -> tuple[str, str, str]:
    """
    Compose subject + HTML + plaintext for a group of events.

    Templates `alert.html` and `alert.txt` are used with simple {{ subject }}
    and {{ body }} tokens.
    """
    subject = _subject_for_group(group_key, events)
    html_table, text_lines = _build_bodies(subject, events, settings)

    html_tpl = _read_template("alert.html")
    txt_tpl = _read_template("alert.txt")

    context_html = {
        "subject": subject,
        # For the HTML template we insert the table into {{ body }}.
        "body": html_table,
        "generated_at": _now_utc_iso(),
        "group": group_key,
    }
    html_body = _render(html_tpl, context_html)

    context_txt = {
        "subject": subject,
        "body": text_lines,
        "generated_at": _now_utc_iso(),
        "group": group_key,
    }
    text_body = _render(txt_tpl, context_txt)

    return subject, html_body, text_body


def send(
    settings: Settings,
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    """Send the message with yagmail. Raises on failure."""
    settings.require_email()
    user = settings.email.user
    app_password = settings.email.app_password

    log.debug("Connecting to SMTP as %s to send to %s", user, ", ".join(recipients))
    with yagmail.SMTP(user, app_password) as yag:
        yag.send(
            to=recipients,
            subject=subject,
            contents=[text_body, html_body],
        )

    log.info(
        "Email sent to %d recipient(s): %s",
        len(recipients),
        ", ".join(recipients),
    )
