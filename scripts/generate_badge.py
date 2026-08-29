#!/usr/bin/env python3
"""Render a Duolingo streak banner as SVG, on a schedule.

Duolingo has no public API. This reads the same unauthenticated endpoint the
web client uses, which means it can change shape or start refusing us at any
time. So:

  * A failed or implausible fetch never fails the run -- the banner falls back
    to the last known good values and shows a stale marker.
  * Each field gets its own validation rule, because "suspicious" means
    different things per field. Total XP is monotonic and must never fall.
    A streak legitimately resets to zero when you miss a day, so a drop there
    is real data, but a large jump is more likely a parsing error.
  * Only writes files when the rendered output actually changed, so the commit
    history stays meaningful.

Standard library only -- no dependency surface to maintain.
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

DUOLINGO_USERNAME = "Aham_Sai_Chary"

# Unofficial. The response is {"users": [ { ...profile... } ]}.
ENDPOINT = (
    "https://www.duolingo.com/2017-06-30/users"
    f"?username={DUOLINGO_USERNAME}"
)

# Duolingo rejects requests without a browser-ish User-Agent.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

TIMEOUT_SECONDS = 15
STALE_AFTER_HOURS = 48
MAX_STREAK_JUMP = 30   # a one-run gain bigger than this smells like a bad parse

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "last_known.json"
README = ROOT / "README.md"
BANNER = ROOT / "assets" / "banner.svg"

WIDTH, HEIGHT = 900, 240

# Flag stripes for the right edge, keyed by Duolingo's language code.
FLAGS = {
    "de": ["#000000", "#dd0000", "#ffce00"],
    "es": ["#aa151b", "#f1bf00", "#aa151b"],
    "fr": ["#002395", "#ffffff", "#ed2939"],
    "it": ["#008c45", "#f4f5f0", "#cd212a"],
    "pt": ["#006600", "#ff0000", "#ffcc00"],
    "nl": ["#ae1c28", "#ffffff", "#21468b"],
    "ja": ["#ffffff", "#bc002d", "#ffffff"],
    "ko": ["#ffffff", "#003478", "#c60c30"],
    "zh": ["#de2910", "#ffde00", "#de2910"],
    "ru": ["#ffffff", "#0039a6", "#d52b1e"],
    "sv": ["#006aa7", "#fecc00", "#006aa7"],
    "pl": ["#ffffff", "#dc143c", "#ffffff"],
    "tr": ["#e30a17", "#ffffff", "#e30a17"],
    "ar": ["#007a3d", "#ffffff", "#000000"],
    "hi": ["#ff9933", "#ffffff", "#138808"],
    "en": ["#012169", "#ffffff", "#c8102e"],
}
DEFAULT_FLAG = ["#58cc02", "#3ea800", "#2c7a00"]

LANGUAGE_NAMES = {
    "de": "German", "es": "Spanish", "fr": "French", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "ru": "Russian", "sv": "Swedish", "pl": "Polish",
    "tr": "Turkish", "ar": "Arabic", "hi": "Hindi", "en": "English",
}


# --- helpers ----------------------------------------------------------------

def log(level: str, message: str) -> None:
    """GitHub Actions annotation in CI, plain stderr locally."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{message}")
    else:
        print(f"[{level}] {message}", file=sys.stderr)


def coerce_int(value, field: str) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"bad {field}: {value!r}")
    return value


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
        # Also what you get for a private profile or a mistyped username.
        raise ValueError("no user in response (private profile or wrong name?)")

    profile = users[0]
    if not isinstance(profile, dict):
        raise ValueError("unexpected user shape")

    return {
        "streak": coerce_int(profile.get("streak"), "streak"),
        "total_xp": coerce_int(profile.get("totalXp"), "totalXp"),
        "language": str(profile.get("learningLanguage") or "").lower() or None,
    }


def validate(fresh: dict, previous: dict) -> None:
    """Raise if the new reading contradicts what we already know."""
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

def render_banner(values: dict, stale: bool) -> str:
    streak = values["streak"]
    total_xp = values["total_xp"]
    code = (values.get("language") or "").lower()
    language = LANGUAGE_NAMES.get(code, code.upper() or "Duolingo")
    stripes = FLAGS.get(code, DEFAULT_FLAG)

    streak_text = f"{streak:,}"

    subtitle = escape(f"{language}  ·  {total_xp:,} XP")
    alt = f"{streak} day {language} streak, {total_xp:,} XP total"

    stale_marker = ""
    if stale:
        stale_marker = (
            '<g transform="translate(806 34)">'
            '<circle r="6" fill="#f59e0b"/>'
            '<title>Stale: the last fetch from Duolingo failed, '
            'showing the last known good values.</title>'
            "</g>"
        )

    stripe_height = HEIGHT / 3
    flag = "".join(
        f'<rect x="0" y="{i * stripe_height:.1f}" width="62" '
        f'height="{stripe_height:.1f}" fill="{colour}" opacity="0.7"/>'
        for i, colour in enumerate(stripes)
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" \
viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(alt)}">
  <title>{escape(alt)}</title>

  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0f2b12"/>
      <stop offset="0.55" stop-color="#14401a"/>
      <stop offset="1" stop-color="#0a1f0d"/>
    </linearGradient>
    <linearGradient id="flame" x1="0.5" y1="1" x2="0.5" y2="0">
      <stop offset="0" stop-color="#ff9500"/>
      <stop offset="0.5" stop-color="#ffb800"/>
      <stop offset="1" stop-color="#ffe066"/>
    </linearGradient>
    <linearGradient id="flameInner" x1="0.5" y1="1" x2="0.5" y2="0">
      <stop offset="0" stop-color="#ffd24d"/>
      <stop offset="1" stop-color="#fff6d1"/>
    </linearGradient>
    <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#58cc02"/>
      <stop offset="1" stop-color="#58cc02" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="#ffb800" stop-opacity="0.35"/>
      <stop offset="1" stop-color="#ffb800" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="card"><rect width="{WIDTH}" height="{HEIGHT}" rx="18"/></clipPath>
  </defs>

  <g clip-path="url(#card)">
    <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#bg)"/>

    <g stroke="#58cc02" stroke-opacity="0.07" fill="none" stroke-width="1.5">
      <path d="M-20 200 Q 180 120 420 190 T 920 150"/>
      <path d="M-20 225 Q 200 150 440 215 T 920 175"/>
      <path d="M-20 175 Q 160 95 400 165 T 920 125"/>
    </g>

    <circle cx="140" cy="120" r="105" fill="url(#glow)"/>

    <g transform="translate(140 120)">
      <path d="M0 -74 C 26 -44 40 -26 40 -2 C 40 26 21 46 0 46
               C -21 46 -40 26 -40 -2 C -40 -22 -28 -34 -16 -50
               C -12 -34 -4 -28 2 -26 C 6 -42 2 -58 0 -74 Z" fill="url(#flame)"/>
      <path d="M0 -30 C 13 -12 20 -4 20 8 C 20 24 11 34 0 34
               C -11 34 -20 24 -20 8 C -20 -4 -8 -14 0 -30 Z" fill="url(#flameInner)"/>
    </g>

    <g font-family="DejaVu Sans, Verdana, Geneva, sans-serif">
      <text x="270" y="86" fill="#9fe870" font-size="19" letter-spacing="4.5"
            font-weight="bold">CURRENT STREAK</text>
      <text x="268" y="162" fill="#ffffff" font-size="76" font-weight="bold">{streak_text}<tspan
            fill="#d8f5c4" font-size="30" font-weight="bold" dx="18">days</tspan></text>
      <rect x="270" y="182" width="300" height="3" fill="url(#rule)" rx="1.5"/>
      <text x="270" y="214" fill="#8fd96a" font-size="19">{subtitle}</text>
    </g>

    <g transform="translate({WIDTH - 62} 0)">{flag}</g>
    {stale_marker}

    <rect x="0.75" y="0.75" width="{WIDTH - 1.5}" height="{HEIGHT - 1.5}" rx="18"
          fill="none" stroke="#58cc02" stroke-opacity="0.35" stroke-width="1.5"/>
  </g>
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
    version query string whenever the banner actually changes.
    """
    if not README.exists():
        return False
    original = README.read_text(encoding="utf-8")
    updated = re.sub(
        r"(assets/banner\.svg\?v=)\d+",
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

    changed = write_if_changed(BANNER, render_banner(values, stale))
    save_state(values, fetched_at, ok=not stale)

    if changed:
        bump_readme_cache_key()
    else:
        log("notice", "no visible change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
