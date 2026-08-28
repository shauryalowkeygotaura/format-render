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


def _search_pixabay(term: str, size: tuple[int, int]) -> str | None:
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
        videos = hit.get("videos") or {}
        for quality in ("large", "medium", "small"):
            v = videos.get(quality) or {}
            if v.get("url") and int(v.get("width", 0) or 0) >= size[0] // 2:
                return v["url"]
    return None


def _search_pexels(term: str, size: tuple[int, int]) -> str | None:
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
        files = sorted((f for f in vid.get("video_files", [])
                        if isinstance(f, dict) and f.get("link")),
                       key=lambda f: -int(f.get("width", 0) or 0))
        for f in files:
            if int(f.get("width", 0) or 0) >= size[0] // 2:
                return f["link"]
    return None


def _download(url: str, dest: Path) -> bool:
    try:
        with requests.get(url, timeout=(15, 60), stream=True) as r:
            if r.status_code != 200:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    fh.write(chunk)
    except (requests.RequestException, OSError):
        return False
    return dest.exists() and dest.stat().st_size > 50_000


def _conform(src: Path, out: Path, duration: float, size: tuple[int, int]) -> Path | None:
    """Loop/trim to the narration length, scale-crop to frame, strip audio."""
    w, h = size
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},fps={config.FPS},format=yuv420p")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(src),
         "-t", f"{duration:.3f}", "-vf", vf, "-an",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True)
    return out if proc.returncode == 0 and out.exists() else None


def fetch_clip(cue: str, dest: Path, duration: float, size: tuple[int, int],
               topic: str = "") -> Path | None:
    """A conformed stock clip, or None so the caller uses a Ken-Burns still."""
    flags = enabled()
    if not any(flags.values()):
        return None
    term = search_term(cue, topic)
    url = None
    if flags["pixabay"]:
        url = _search_pixabay(term, size)
    if not url and flags["pexels"]:
        url = _search_pexels(term, size)
    if not url:
        return None
    raw = dest.with_suffix(".src.mp4")
    if not _download(url, raw):
        return None
    clip = _conform(raw, dest, duration, size)
    raw.unlink(missing_ok=True)
    return clip
