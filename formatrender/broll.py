"""Optional stock video b-roll: Pixabay and Pexels, both free tiers.

Adapted from football-shorts-autopilot/modules/broll.py and from the material
sourcing MoneyPrinterTurbo popularised (Pexels + Pixabay video search over a
generated search term).

Motion beats a Ken-Burns push on a still, but ONLY when the clip is actually
about the topic. A generic "space" stock clip under narration about the
Chelyabinsk meteor is worse than a real photograph of the Chelyabinsk meteor,
so this is opt-in and stills remain the default.

Both providers are key-gated and this module is dry-safe: with no key,
`fetch_clip` returns None and the composer falls through to Ken Burns. Nothing
here can break a render.

Getting keys (both free, no card):
    Pixabay  https://pixabay.com/api/docs/     -> PIXABAY_API_KEY
    Pexels   https://www.pexels.com/api/       -> PEXELS_API_KEY

Pexels accepts a comma-separated list in PEXELS_API_KEY and rotates through
them, which is how football-shorts stretches the free tier across a daily run.
"""
from __future__ import annotations

import itertools
import os
import re
import subprocess
from pathlib import Path

import requests

from . import config

PIXABAY_API = "https://pixabay.com/api/videos/"
PEXELS_API = "https://api.pexels.com/videos/search"
MIN_CLIP_SEC = 4
_key_cycle: itertools.cycle | None = None

_STOP = {"image", "photo", "shot", "view", "of", "the", "a", "an", "and", "with"}


def enabled() -> dict[str, bool]:
    return {"pixabay": bool(os.environ.get("PIXABAY_API_KEY", "").strip()),
            "pexels": bool(os.environ.get("PEXELS_API_KEY", "").strip())}


def search_term(cue: str, topic: str = "") -> str:
    """Stock libraries index generic concepts, not named entities.

    'image of 16 Psyche' returns nothing on Pixabay; 'asteroid space' returns
    plenty. So the entity is dropped and the topic carried instead, which is
    the opposite of the Commons ladder in visuals.py, where the entity is the
    whole point.
    """
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", cue or "")
             if w.lower() not in _STOP]
    generic = [w for w in words if not w[0].isupper()][:2]
    parts = generic or words[:2]
    if topic:
        parts.append(topic.lower())
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out[:3]) or (topic or "abstract")


def _pexels_key() -> str | None:
    global _key_cycle
    raw = os.environ.get("PEXELS_API_KEY", "").strip()
    if not raw:
        return None
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return None
    if _key_cycle is None:
        _key_cycle = itertools.cycle(keys)
    return next(_key_cycle)


def _search_pixabay(term: str, size: tuple[int, int],
                    exclude: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """(url, clip_id) or None. `exclude` holds clip_ids already used."""
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.get(PIXABAY_API, timeout=(15, 30), params={
            "key": key, "q": term, "video_type": "film",
            "per_page": 10, "safesearch": "true"})
        if r.status_code != 200:
            return None
        hits = r.json().get("hits", [])
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(hits, list):
        return None
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        if int(hit.get("duration", 0) or 0) < MIN_CLIP_SEC:
            continue
        clip_id = f"pixabay:{hit.get('id')}"
        # Skip the WHOLE hit, not just one rendition: the same clip served at a
        # different quality is still the same footage on screen.
        if clip_id in exclude:
            continue
        videos = hit.get("videos") or {}
        for quality in ("large", "medium", "small"):
            v = videos.get(quality) or {}
            if v.get("url") and int(v.get("width", 0) or 0) >= size[0] // 2:
                return v["url"], clip_id
    return None


def _search_pexels(term: str, size: tuple[int, int],
                   exclude: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """(url, clip_id) or None. `exclude` holds clip_ids already used."""
    key = _pexels_key()
    if not key:
        return None
    try:
        r = requests.get(PEXELS_API, timeout=(15, 30),
                         headers={"Authorization": key},
                         params={"query": term, "per_page": 10,
                                 "orientation": "landscape", "size": "medium"})
        if r.status_code != 200:
            return None
        videos = r.json().get("videos", [])
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(videos, list):
        return None
    for vid in videos:
        if not isinstance(vid, dict):
            continue
        if int(vid.get("duration", 0) or 0) < MIN_CLIP_SEC:
            continue
        clip_id = f"pexels:{vid.get('id')}"
        # Dedupe on the VIDEO id, not the per-resolution link. Keyed on the link
        # the same footage walks back in through a different rendition URL.
        # `continue`, not `break`: try the next video, do not abandon the search.
        if clip_id in exclude:
            continue
        files = sorted((f for f in vid.get("video_files", [])
                        if isinstance(f, dict) and f.get("link")),
                       key=lambda f: -int(f.get("width", 0) or 0))
        for f in files:
            if int(f.get("width", 0) or 0) >= size[0] // 2:
                return f["link"], clip_id
    return None


def fetch_clip(cue: str, dest: Path, duration: float, size: tuple[int, int],
               topic: str = "", used_ids: set[str] | None = None) -> Path | None:
    """A conformed stock clip, or None so the caller uses a Ken-Burns still.

    `used_ids` holds the clip ids already placed in THIS video. It is read to
    skip repeats and mutated on SUCCESS. Without it, related cues ("aurora",
    "polar horizon", "high-latitude") all rank the same clip first and one clip
    was reused 15 times in a single render, so the video changed shot every 4
    seconds while showing identical footage.
    """
    flags = enabled()
    if not any(flags.values()):
        return None
    term = search_term(cue, topic)
    seen = frozenset(used_ids or ())
    found = None
    if flags["pixabay"]:
        found = _search_pixabay(term, size, seen)
    if not found and flags["pexels"]:
        found = _search_pexels(term, size, seen)
    if not found:
        return None
    url, clip_id = found
    raw = dest.with_suffix(".src.mp4")
    if not _download(url, raw):
        return None
    clip = _conform(raw, dest, duration, size)
    raw.unlink(missing_ok=True)
    # Record only AFTER the clip actually exists. Marking it used earlier would
    # blacklist footage that never reached the video because a download failed.
    if clip is not None and used_ids is not None:
        used_ids.add(clip_id)
    return clip
