#!/usr/bin/env python3
"""Render a Duolingo streak/XP badge as SVG, on a schedule.

Duolingo has no public API. This reads the same unauthenticated endpoint the
web client uses, which means it can change shape or start refusing us at any
time. So:

  * A failed or implausible fetch never fails the run -- the badge falls back
    to the last known good values and shows a stale marker.
  * Each field gets its own validation rule, because "suspicious" means
    different things per field. Total XP is monotonic and must never fall.
    A streak legitimately resets to zero when you miss a day, so a drop there
    is real data, but a large jump is more likely a parsing error.
  * Only writes files when the rendered output actually changed.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

# --- configuration ----------------------------------------------------------

DUOLINGO_USERNAME = "Aham_Sai_Chary"  # <- your public Duolingo handle

# Unofficial. The response is {"users": [ { ...profile... } ]}.
ENDPOINT = (
    "https://www.duolingo.com/2017-06-30/users"
    f"?username={DUOLINGO_USERNAME}"
    "&fields=streak,totalXp,learningLanguage"
)

# Duolingo rejects requests without a browser-ish User-Agent.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

BADGE_LABEL = "duolingo"
TIMEOUT_SECONDS = 15
STALE_AFTER_HOURS = 48
MAX_STREAK_JUMP = 30   # a one-day gain bigger than this smells like a bad parse

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "last_known.json"
README = ROOT / "README.md"
LIGHT_SVG = ROOT / "assets" / "duolingo.svg"
DARK_SVG = ROOT / "assets" / "duolingo-dark.svg"

THEMES = {
    LIGHT_SVG: {"label_bg": "#3c3c3c", "value_bg": "#58cc02", "text": "#ffffff"},
    DARK_SVG: {"label_bg": "#2b2b2b", "value_bg": "#4caf00", "text": "#ffffff"},
}


# --- helpers ----------------------------------------------------------------

def log(level: str, message: str) -> None:
    """GitHub Actions annotation when running in CI, plain stderr locally."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{message}")
    else:
        print(f"[{level}] {message}", file=sys.stderr)


def fetch_profile() -> dict:
    request = urllib.request.Request(
        ENDPOINT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", None)
        if status is not None and status != 200:
            raise RuntimeError(f"unexpected status {status}")
        payload = json.loads(response.read().decode("utf-8"))

    users = payload.get("users")
    if not isinstance(users, list) or not users:
        # Also what you get for a private profile or a typo'd username.
        raise ValueError("no user in response (private profile or wrong name?)")

    profile = users[0]
    if not isinstance(profile, dict):
        raise ValueError("unexpected user shape")

    return {
        "streak": coerce_int(profile.get("streak"), "streak"),
        "total_xp": coerce_int(profile.get("totalXp"), "totalXp"),
        "language": str(profile.get("learningLanguage") or "").lower() or None,
    }


def coerce_int(value, field: str) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"bad {field}: {value!r}")
    return value


def validate(fresh: dict, previous: dict) -> None:
    """Raise if the new reading contradicts what we already know.

    Total XP only ever goes up. A streak can drop to zero for real, so only
    an implausible *rise* is treated as suspect.
    """
    old_xp = previous.get("total_xp")
    if isinstance(old_xp, int) and fresh["total_xp"] < old_xp:
        raise ValueError(f"total XP fell: {old_xp} -> {fresh['total_xp']}")

    old_streak = previous.get("streak")
    if isinstance(old_streak, int):
        jump = fresh["streak"] - old_streak
        if jump > MAX_STREAK_JUMP:
            raise ValueError(f"streak jumped {jump} days in one run")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(values: dict, fetched_at: str, ok: bool) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({**values, "fetched_at": fetched_at, "last_success": ok},
                   indent=2) + "\n",
        encoding="utf-8",
    )


def hours_since(iso_timestamp: str | None) -> float:
    if not iso_timestamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


# --- rendering --------------------------------------------------------------

LANGUAGE_NAMES = {
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "nl": "Dutch", "sv": "Swedish", "ar": "Arabic", "hi": "Hindi",
    "ru": "Russian", "tr": "Turkish", "pl": "Polish", "en": "English",
}


def value_text(values: dict) -> str:
    parts = [f"{values['streak']} day streak",
             f"{values['total_xp']:,} XP"]
    language = LANGUAGE_NAMES.get(values.get("language") or "")
    if language:
        parts.insert(0, language)
    return "  ".join(parts)


def text_width(text: str, size: int = 11) -> int:
    """Rough advance width for DejaVu Sans. Good enough for a badge."""
    return int(len(text) * size * 0.62)


def render_svg(label: str, value: str, stale: bool, theme: dict) -> str:
    label, value = escape(label), escape(value)
    pad, height = 10, 20
    label_w = text_width(label) + pad * 2
    value_w = text_width(value) + pad * 2
    total_w = label_w + value_w
    alt = f"{label}: {value}"

    stale_dot = ""
    if stale:
        stale_dot = (
            f'<circle cx="{total_w - 6}" cy="5" r="3" fill="#f59e0b">'
            "<title>Stale: the last fetch from Duolingo failed.</title>"
            "</circle>"
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{height}" \
role="img" aria-label="{escape(alt)}">
  <title>{escape(alt)}</title>
  <linearGradient id="gloss" x2="0" y2="100%">
    <stop offset="0" stop-color="#fff" stop-opacity=".08"/>
    <stop offset="1" stop-opacity=".08"/>
  </linearGradient>
  <clipPath id="round"><rect width="{total_w}" height="{height}" rx="4"/></clipPath>
  <g clip-path="url(#round)">
    <rect width="{label_w}" height="{height}" fill="{theme['label_bg']}"/>
    <rect x="{label_w}" width="{value_w}" height="{height}" fill="{theme['value_bg']}"/>
    <rect width="{total_w}" height="{height}" fill="url(#gloss)"/>
  </g>
  <g fill="{theme['text']}" font-family="DejaVu Sans,Verdana,Geneva,sans-serif" \
font-size="11" text-anchor="middle">
    <text x="{label_w / 2:.0f}" y="14">{label}</text>
    <text x="{label_w + value_w / 2:.0f}" y="14" font-weight="bold">{value}</text>
  </g>
  {stale_dot}
</svg>
"""


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def bump_readme_cache_key() -> bool:
    """GitHub proxies README images through camo and caches them hard.

    Committing a new SVG at the same URL is not reliably enough, so bump a
    version query string whenever the badge actually changes.
    """
    if not README.exists():
        return False
    original = README.read_text(encoding="utf-8")
    updated = re.sub(
        r"(assets/duolingo(?:-dark)?\.svg\?v=)\d+",
        lambda m: f"{m.group(1)}{int(time.time())}",
        original,
    )
    if updated == original:
        return False
    README.write_text(updated, encoding="utf-8")
    return True


# --- entry point ------------------------------------------------------------

def main() -> int:
    previous = load_state()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        values = fetch_profile()
        validate(values, previous)
        stale, fetched_at = False, now
        log("notice", f"fetched {values}")
    except (urllib.error.URLError, OSError, ValueError, KeyError,
            RuntimeError, json.JSONDecodeError) as error:
        if "total_xp" not in previous:
            log("error", f"fetch failed and nothing is cached: {error}")
            return 1
        values = {k: previous.get(k) for k in ("streak", "total_xp", "language")}
        fetched_at = previous.get("fetched_at", now)
        stale = True
        log("warning", f"fetch failed ({error}); serving cached values")

    if not stale:
        stale = hours_since(fetched_at) > STALE_AFTER_HOURS

    rendered = value_text(values)
    changed = False
    for path, theme in THEMES.items():
        changed |= write_if_changed(
            path, render_svg(BADGE_LABEL, rendered, stale, theme)
        )

    save_state(values, fetched_at, ok=not stale)
    if changed:
        bump_readme_cache_key()
    else:
        log("notice", "no visible change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
