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
# How many results to ask for per search. Both APIs return this in ONE call, so
# a deep pool costs nothing extra in requests.
#
# This was 10, which is roughly how many usable clips a cue returns. Once shot
# dedupe landed, a 44-shot video needed 44 distinct clips while each cue could
# only ever offer ~9, so later beats exhausted the pool and fell through to a
# flat gradient: 9 consecutive gradient shots in one beat. Measured against the
# live API, per_page=80 returns 76-80 usable clips for the same cues.
SEARCH_POOL = 80

# A clip is rejected as unwatchable only when it is dim AND has no highlights.
# Calibrated against real footage from a solar-storms render, because the naive
# "too dark" rule throws away the best shots in the video:
#   aurora over a horizon   YAVG 121  YMAX 245  <- the single best frame
#   sun disc on black space YAVG  40  YMAX 255  <- dim average, perfectly good
#   dead night frame        YAVG  16  YMAX 107  <- reject
#   dead grey frame         YAVG  23  YMAX  89  <- reject
# Mean luminance alone cannot separate the second row from the last two; the
# peak is what says whether anything is actually visible in the frame.
MIN_YAVG = 25.0
MIN_YMAX = 130.0
# Each retry costs another download and conform, so this is capped. Measured
# rate of dead clips was 2 in 39 (5%), so 4 attempts is ample headroom.
MAX_CLIP_ATTEMPTS = 4
_key_cycle: itertools.cycle | None = None

# Words that name no picture. A stock query built from verbs, connectives or
# abstractions ("arrives", "expand", "insurance") matches nothing in particular,
# and both APIs answer ANY query with a full page of loosely-related results, so
# a meaningless query fails silently instead of returning nothing.
_STOP = {
    "image", "photo", "shot", "view", "of", "the", "a", "an", "and", "with",
    # connectives and determiners
    "from", "into", "onto", "over", "under", "that", "this", "these", "those",
    "than", "then", "when", "what", "which", "while", "where", "their", "them",
    "they", "there", "its", "for", "but", "not", "you", "your", "our", "was",
    "were", "are", "has", "have", "had", "been", "being", "can", "will",
    "would", "could", "should", "may", "might", "must", "each", "every", "all",
    "any", "some", "most", "more", "less", "very", "just", "only", "also",
    # generic verbs and abstractions that describe no visible subject
    "arrives", "arrive", "expand", "expands", "suffer", "suffers", "posing",
    "cause", "causes", "caused", "begin", "begins", "start", "starts", "make",
    "makes", "made", "take", "takes", "become", "becomes", "happen", "happens",
    "means", "meant", "keep", "keeps", "need", "needs", "used", "using",
    "total", "large", "small", "slight", "chaos", "insurance", "levels",
}


def enabled() -> dict[str, bool]:
    return {"pixabay": bool(os.environ.get("PIXABAY_API_KEY", "").strip()),
            "pexels": bool(os.environ.get("PEXELS_API_KEY", "").strip())}


def search_term(cue: str, topic: str = "") -> str:
    """A query a stock library can actually match.

    Stock libraries index concrete, generic concepts: "aurora", "power grid",
    "city at night". They do not index named entities, so 'image of 16 Psyche'
    returns nothing useful while 'asteroid space' returns plenty.

    Two bugs made this return near-random queries, which is why the footage
    looked unrelated to the narration:

    1. The proper-noun filter was dead. `words` was lowercased BEFORE testing
       `w[0].isupper()`, so the test was never true and nothing was ever
       filtered. It just took the first two words of the cue.
    2. Those first two words are the worst possible choice. shots._keywords
       deliberately sorts proper nouns FIRST, because for Commons the named
       entity is the whole point. Chained together, this function reliably
       picked exactly the words it was written to reject, producing queries
       like "chaos power ..." and "total carrington ...".

    The topic is also condensed. Appending the whole topic sentence made every
    cue's query ~80% identical boilerplate, so unrelated shots searched for
    nearly the same thing and the query's specific half was drowned out.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", cue or "")
    generic, proper, seen = [], [], set()
    for w in tokens:
        low = w.lower()
        if low in _STOP or low in seen:
            continue
        seen.add(low)
        # Case must be read from the ORIGINAL token, not a lowercased copy.
        (proper if w[0].isupper() else generic).append(low)
    # Generic concepts first; proper nouns only when there is nothing else, so a
    # cue made entirely of names still produces a query rather than an empty one.
    parts = (generic or proper)[:2]

    for w in _topic_anchor(topic):
        if w not in parts:
            parts.append(w)
    return " ".join(parts[:4]) or (topic.lower() if topic else "abstract")


def _topic_anchor(topic: str, limit: int = 2) -> list[str]:
    """The first couple of content words of the topic.

    Topics here are written subject-first ("solar storms hitting Earth, from
    aurora to grid collapse"), so the opening words carry the subject and the
    tail is framing that only dilutes the query.
    """
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", topic or ""):
        low = w.lower()
        if low in _STOP or low in out:
            continue
        out.append(low)
        if len(out) >= limit:
            break
    return out


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
            "per_page": SEARCH_POOL, "safesearch": "true"})
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
                         params={"query": term, "per_page": SEARCH_POOL,
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


def _frame_stats(path: Path) -> tuple[float, float] | None:
    """(mean, peak) luminance of one frame, or None if it cannot be measured.

    Samples a frame a little way in rather than the first, which is often a
    fade from black and would condemn a perfectly good clip.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-vf", r"select='eq(n\,15)',signalstats,metadata=print",
         "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True)
    blob = (proc.stderr or "") + (proc.stdout or "")
    avg = re.search(r"YAVG=([0-9.]+)", blob)
    peak = re.search(r"YMAX=([0-9.]+)", blob)
    if not avg or not peak:
        return None
    try:
        return float(avg.group(1)), float(peak.group(1))
    except ValueError:
        return None


def _unwatchable(stats: tuple[float, float] | None) -> bool:
    """A frame with nothing visible in it.

    An unmeasurable clip is KEPT. Discarding footage because ffprobe output
    could not be parsed would turn a tooling hiccup into a missing shot.
    """
    if stats is None:
        return False
    yavg, ymax = stats
    return yavg < MIN_YAVG and ymax < MIN_YMAX


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
    raw = dest.with_suffix(".src.mp4")
    rejected: set[str] = set()
    fallback: tuple[Path, str] | None = None

    for _ in range(MAX_CLIP_ATTEMPTS):
        seen = frozenset(used_ids or ()) | rejected
        found = None
        if flags["pixabay"]:
            found = _search_pixabay(term, size, seen)
        if not found and flags["pexels"]:
            found = _search_pexels(term, size, seen)
        if not found:
            break
        url, clip_id = found
        if not _download(url, raw):
            rejected.add(clip_id)
            continue
        clip = _conform(raw, dest, duration, size)
        raw.unlink(missing_ok=True)
        if clip is None:
            rejected.add(clip_id)
            continue
        if _unwatchable(_frame_stats(clip)):
            # Keep the first dim clip in reserve: a dark shot still beats the
            # flat gradient card the caller falls back to.
            if fallback is None:
                keep = clip.with_suffix(".dim.mp4")
                try:
                    clip.replace(keep)
                    fallback = (keep, clip_id)
                except OSError:
                    pass
            rejected.add(clip_id)
            continue
        # Record only AFTER the clip exists and is worth showing. Marking it
        # earlier would blacklist footage that never reached the video.
        if used_ids is not None:
            used_ids.add(clip_id)
        return clip

    if fallback is not None:
        keep, clip_id = fallback
        try:
            keep.replace(dest)
        except OSError:
            return None
        if used_ids is not None:
            used_ids.add(clip_id)
        return dest
    return None
