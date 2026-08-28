"""Central config for format-render.

Deliberately niche-agnostic. Everything topic-specific arrives in the brief, so
this file never needs editing to point the renderer at a different channel.
That is the difference from football-shorts-autopilot, whose config.py hardcodes
one channel's identity.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
WORK = OUTPUT / "work"
OUTPUT.mkdir(parents=True, exist_ok=True)

# Where format-engine lives. The two projects stay separate; this is the only
# seam between them, and it degrades gracefully when the path is wrong.
FORMAT_ENGINE_HOME = Path(os.environ.get(
    "FORMAT_ENGINE_HOME", str(ROOT.parent / "format-engine")))
FORMAT_SPECS = FORMAT_ENGINE_HOME / "data" / "formats"
BRIEF_DIR = FORMAT_ENGINE_HOME / "data" / "briefs"

# ── video ────────────────────────────────────────────────────────────────────
RESOLUTION = (1920, 1080)
SHORT_RESOLUTION = (1080, 1920)   # 9:16 vertical, sliced from the master
FPS = 30
THUMB_SIZE = (1280, 720)

# ── voice (edge-tts, free, no key) ───────────────────────────────────────────
VOICE = os.environ.get("FR_VOICE", "en-US-AndrewMultilingualNeural")
VOICE_RATE = os.environ.get("FR_VOICE_RATE", "+6%")

# ── imagery ──────────────────────────────────────────────────────────────────
# Wikimedia Commons: CC / public-domain stills, no key, no cost, and safe to
# monetise over. Their UA policy 429-blocks generic agents, so a real contact
# string is required or every download silently fails to a gradient card.
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WIKIMEDIA_CONTACT = os.environ.get(
    "WIKIMEDIA_CONTACT", "https://github.com/shauryalowkeygotaura")

KEN_BURNS_ZOOM = 1.18
KEN_BURNS_STEP = 0.0008

# ── thumbnail targeting ──────────────────────────────────────────────────────
# The renderer builds to the format's MEASURED numbers, then re-measures its own
# output and corrects. These bound that loop.
THUMB_MATCH_TOLERANCE = 0.12   # same tolerance format-engine's verify-thumb uses
# Each pass is clamped to a 1.5x enhancement so the loop converges instead of
# oscillating, and mean HSV saturation on a dark image moves far less than the
# enhancement factor suggests: measured gains run ~10-15% per pass. Three passes
# left a genuinely-converging image well short of target. These are local Pillow
# operations with no API cost, so the budget is cheap to raise.
THUMB_CORRECTION_PASSES = 10
