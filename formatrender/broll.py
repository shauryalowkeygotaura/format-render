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
import threading
import zlib
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

# Human presence. Faces hold attention in a way landscapes do not, and a
# faceless-channel video built entirely from scenery reads as a screensaver.
# Not every shot: a solar-storm explainer with a person in all forty shots is
# just as wrong. Every other shot targets a human, so roughly half the video
# has someone in it.
FACE_EVERY_NTH_SHOT = 2
# Appended to the query, because a term like "collapse solar storms" will never
# return a person no matter how many results are fetched. The word is chosen
# deterministically from the cue so neighbouring shots do not issue the same
# query and collide on the same footage.
_PEOPLE_WORDS = ("person", "man", "woman", "people", "worker", "engineer")
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


def search_term(cue: str, topic: str = "", want_face: bool = False) -> str:
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
    # One cue word instead of two when a person is wanted, to leave room for the
    # people word and the topic anchor inside the four-word cap.
    parts = (generic or proper)[:1 if want_face else 2]

    if want_face:
        # crc32, NOT hash(): Python salts string hashing per process, so hash()
        # would pick a different word every run and the same brief would
        # render different footage each time.
        word = _PEOPLE_WORDS[zlib.crc32((cue or "").encode()) % len(_PEOPLE_WORDS)]
        if word not in parts:
            parts.append(word)

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
                    exclude: frozenset[str] = frozenset(),
                    offset: int = 0) -> tuple[str, str] | None:
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
    acceptable = []
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
                acceptable.append((v["url"], clip_id))
                break
    if not acceptable:
        return None
    return acceptable[offset % len(acceptable)]


# ── relevance judge ──────────────────────────────────────────────────────────
# Keyword search returns clips that only share a word with the cue: a 2026-11-24 CI render put
# nightclub footage under "the invoice totalled Can$6 million". One Groq call per (term, topic)
# reads every candidate's title (the Pexels page slug) and keeps only clips a viewer would
# connect to the line. Rejected -> the shot falls through to a Commons still, then a drawn card.
# Groq down or no key -> no filtering (the old behaviour), never a failed render.
_JUDGED: dict[tuple[str, str], frozenset[str] | None] = {}
_JUDGE_LOCK = threading.Lock()


def _slug_title(page_url: str) -> str:
    slug = (page_url or "").rstrip("/").rsplit("/", 1)[-1]
    words = slug.split("-")
    return " ".join(words[:-1] if words and words[-1].isdigit() else words)[:120]


def _relevant(term: str, topic: str, cands: list[tuple[str, str]]) -> frozenset[str] | None:
    """clip_ids worth showing for `term` in a video about `topic`; None = could not judge."""
    if not cands or os.environ.get("FR_JUDGE", "1") == "0":
        return None
    key = (term, topic)
    with _JUDGE_LOCK:
        if key in _JUDGED:
            return _JUDGED[key]
    verdict: frozenset[str] | None = None
    try:
        import json  # noqa: PLC0415

        import drawn_scenes  # noqa: PLC0415  (vendored; its groq_chat rotates the key pool)
        menu = "\n".join(f"{i}: {t or '(untitled)'}" for i, (_, t) in enumerate(cands[:60]))
        raw = drawn_scenes.groq_chat([
            {"role": "system", "content":
                "You pick stock footage for a documentary. Keep a clip only if a viewer would connect it to "
                "the search and the video's topic: it shows the subject, its setting, or a scene that plainly "
                "illustrates it. Reject clips that merely share a word, generic lifestyle scenes (parties, "
                "nightclubs, people posing, dancing) unless the topic is about them, and anything off-tone. "
                'Reply JSON {"keep": [indexes]}'},
            {"role": "user", "content": f"Topic: {topic}\nSearch: {term}\nClips:\n{menu}"}], 900)
        keep = json.loads(raw).get("keep", [])
        if isinstance(keep, list):
            verdict = frozenset(cands[i][0] for i in keep
                                if isinstance(i, int) and 0 <= i < min(60, len(cands)))
            print(f"  broll judge: {term!r} kept {len(verdict)}/{min(60, len(cands))}")
    except Exception as e:  # noqa: BLE001 - judging is a bonus; never fail a render over it
        print(f"  broll judge unavailable for {term!r}: {str(e)[:120]}")
        verdict = None
    with _JUDGE_LOCK:
        _JUDGED[key] = verdict
    return verdict


def _search_pexels(term: str, size: tuple[int, int],
                   exclude: frozenset[str] = frozenset(),
                   offset: int = 0, topic: str = "") -> tuple[str, str] | None:
    """(url, clip_id) or None. `exclude` holds clip_ids already used.
    With `topic`, Groq drops candidates that are off-topic (see _relevant)."""
    key = _pexels_key()
    if not key:
        return None
    try:
        # Page through, do not just read page 1. Pexels reports total_results in
        # the thousands (8000 for "dark city night") while per_page caps at 80,
        # so reading only the first page made the pool look 100x shallower than
        # it is. The page is derived from the offset so different shots reach
        # different pages instead of all contending for the same 80 results.
        page = 1 + (offset // max(1, SEARCH_POOL))
        r = requests.get(PEXELS_API, timeout=(15, 30),
                         headers={"Authorization": key},
                         params={"query": term, "per_page": SEARCH_POOL,
                                 "page": page,
                                 "orientation": "landscape", "size": "medium"})
        if r.status_code != 200:
            return None
        videos = r.json().get("videos", [])
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(videos, list):
        return None
    acceptable = []
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
        # SMALLEST rendition that still covers the frame, not the largest.
        # Sorting by -width downloaded a 62 MB 3840x2160 60fps file to render
        # 1920x1080, so every clip paid 5.6s of transfer and then 8.5s of
        # conform mostly spent decoding 4K60 that was thrown away. Picking the
        # smallest file at or above the target width is free quality-wise: the
        # conform downscales to exactly `size` either way.
        usable = [f for f in vid.get("video_files", [])
                  if isinstance(f, dict) and f.get("link")
                  and int(f.get("width", 0) or 0) >= size[0]]
        if not usable:  # nothing at full width, fall back to the biggest there is
            usable = sorted((f for f in vid.get("video_files", [])
                             if isinstance(f, dict) and f.get("link")
                             and int(f.get("width", 0) or 0) >= size[0] // 2),
                            key=lambda f: -int(f.get("width", 0) or 0))[:1]
        # Then prefer a sane frame rate: 60fps doubles decode cost for frames
        # the render throws away at config.FPS anyway.
        usable.sort(key=lambda f: (int(f.get("width", 0) or 0),
                                   abs(int(f.get("fps", 30) or 30) - config.FPS)))
        if usable:
            acceptable.append((usable[0]["link"], clip_id, _slug_title(vid.get("url", ""))))
    if topic and acceptable:
        ok = _relevant(term, topic, [(cid, title) for _, cid, title in acceptable])
        if ok is not None:
            acceptable = [a for a in acceptable if a[1] in ok]
    acceptable = [(link, cid) for link, cid, *_ in acceptable]
    if not acceptable:
        return None
    # Rotate by offset so concurrent shots do not all grab the same first hit.
    # Without this, every worker snapshots the same exclusions, picks result #0,
    # and all but one loses the claim race - each collision burning a retry
    # until workers exhaust MAX_CLIP_ATTEMPTS and return nothing at all.
    # Rotation is deterministic, so renders stay reproducible.
    return acceptable[offset % len(acceptable)]


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
         # Shot clips are transient: the concat pass re-encodes them to burn
         # captions, so intermediate encode effort is thrown away. veryfast at
         # crf 20 is visually indistinguishable after that second pass and
         # roughly 3x quicker than the default medium preset.
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
         "-pix_fmt", "yuv420p", str(out)],
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


def _has_face(path: Path) -> bool | None:
    """Whether a frame of this clip contains a face. None when undecidable.

    None matters: a missing OpenCV, an undecodable clip or a missing cascade
    must not be read as "no face", or every candidate gets rejected and the
    shot falls through to a gradient. Undecidable is treated as acceptable
    everywhere it is used, the same rule as _unwatchable.
    """
    try:
        import cv2
    except ImportError:
        return None
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return None
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        # Sample the middle rather than frame zero, which is often a fade.
        if total > 2:
            cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            return None
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(grey, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(60, 60))
        return len(faces) > 0
    except Exception:
        # Detection is a preference, never a hard requirement. Any failure here
        # must leave the clip usable.
        return None


# libx264 is ALREADY multi-threaded and saturates every core on one clip, so
# running conforms concurrently just time-slices the same cores. Measured on 6
# clips: 11.1s sequential vs 36.5s across 6 workers - parallelism made encoding
# 3.3x SLOWER, which is what turned a render that should have sped up into one
# that took 23 minutes instead of 15.
#
# Search parallelises 8.95x and download about 1.1x, so those stay concurrent.
# Only the encode is serialised.
_ENCODE_LOCK = threading.Lock()


class Claims:
    """Thread-safe set of clip ids already used in this video.

    Parallel fetching breaks a plain set: two workers snapshot the same
    exclusions, search, and can pick the SAME clip before either records it,
    which is exactly the duplicate-footage bug the dedupe exists to prevent.
    `claim` makes check-and-add atomic, so the loser is told to try again.
    """

    def __init__(self, initial=()):
        self._ids = set(initial)
        self._lock = threading.Lock()

    def snapshot(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._ids)

    def claim(self, clip_id: str) -> bool:
        with self._lock:
            if clip_id in self._ids:
                return False
            self._ids.add(clip_id)
            return True

    def __iter__(self):
        return iter(self.snapshot())

    def __len__(self):
        return len(self.snapshot())


def _snapshot(used) -> frozenset[str]:
    if used is None:
        return frozenset()
    snap = getattr(used, "snapshot", None)
    return snap() if snap else frozenset(used)


def _claim(used, clip_id: str) -> bool:
    """True if this caller now owns the id. Plain sets keep working."""
    if used is None:
        return True
    claim = getattr(used, "claim", None)
    if claim:
        return claim(clip_id)
    if clip_id in used:
        return False
    used.add(clip_id)
    return True


def _probe(path: Path) -> tuple[tuple[float, float] | None, bool | None]:
    """((mean, peak) luminance, has_face) from ONE decode of one frame.

    Was two passes over the same frame: _frame_stats spawned an ffmpeg process
    (0.24s) and _has_face opened the file again in OpenCV (1.76s). Both wanted a
    single mid-clip frame, so they now share one decode. Costs about a second
    per clip, and at 225 clips a render that is nearly four minutes.

    Both halves degrade to None independently, keeping the existing contract:
    an unmeasurable clip is kept rather than discarded.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, None
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return None, None
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 2:
            cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None, None
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        stats = (float(np.mean(grey)), float(np.max(grey)))
        face = None
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if not cascade.empty():
            face = len(cascade.detectMultiScale(grey, 1.1, 5, minSize=(60, 60))) > 0
        return stats, face
    except Exception:
        return None, None


def fetch_clip(cue: str, dest: Path, duration: float, size: tuple[int, int],
               topic: str = "", used_ids: set[str] | None = None,
               want_face: bool = False, variant: int = 0) -> Path | None:
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
    term = search_term(cue, topic, want_face)
    raw = dest.with_suffix(".src.mp4")
    rejected: set[str] = set()
    # Best clip that was usable but not ideal. rank 1 beats rank 2, so a bright
    # shot with no face is preferred over a dim one when nothing ideal turns up.
    reserve: tuple[int, Path, str] | None = None

    def _hold(rank: int, clip: Path, clip_id: str) -> None:
        nonlocal reserve
        if reserve is not None and reserve[0] <= rank:
            return
        keep = dest.with_suffix(f".hold{rank}.mp4")
        try:
            clip.replace(keep)
        except OSError:
            return
        reserve = (rank, keep, clip_id)

    for _ in range(MAX_CLIP_ATTEMPTS):
        seen = _snapshot(used_ids) | rejected
        found = None
        if flags["pixabay"]:
            found = _search_pixabay(term, size, seen, variant)
        if not found and flags["pexels"]:
            found = _search_pexels(term, size, seen, variant, topic=topic)
        if not found:
            break
        url, clip_id = found
        if not _download(url, raw):
            rejected.add(clip_id)
            continue
        with _ENCODE_LOCK:
            clip = _conform(raw, dest, duration, size)
        raw.unlink(missing_ok=True)
        if clip is None:
            rejected.add(clip_id)
            continue
        stats, face = _probe(clip)
        if _unwatchable(stats):
            _hold(2, clip, clip_id)
            rejected.add(clip_id)
            continue
        # None means undecidable, which is accepted rather than rejected.
        if want_face and face is False:
            _hold(1, clip, clip_id)
            rejected.add(clip_id)
            continue
        # Claimed only once the clip exists AND is worth showing. Marking it
        # earlier would blacklist footage that never reached the video. A lost
        # race means another worker took this clip first, so try the next one.
        if not _claim(used_ids, clip_id):
            rejected.add(clip_id)
            continue
        return clip

    if reserve is not None:
        _, keep, clip_id = reserve
        try:
            keep.replace(dest)
        except OSError:
            return None
        _claim(used_ids, clip_id)
        return dest
    return None
