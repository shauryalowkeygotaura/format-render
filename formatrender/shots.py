"""Break one narrated beat into several short shots.

WHY
---
The old renderer was one beat, one still, one Ken-Burns push across the whole
beat. On a real render that meant a single photograph on screen for 29 seconds.
Educational faceless video wants the picture to change every 3 to 5 seconds, so
that was between 6x and 10x too slow, and no amount of script or caption work
survives a slideshow.

It also fixes a second defect. The old code searched ONE cue for the whole beat,
so a beat about uncharted asteroids fetched a picture of Mars and held it for
half a minute. Here each shot derives its own cue from the SPAN OF NARRATION IT
COVERS, so the picture tracks what is actually being said at that moment. A
wrong match now costs 4 seconds instead of 30, and is far less likely because
the query is narrower.

Shots are deduplicated by image CONTENT, not filename: Commons happily returns
the same file for two related queries, and repeating a picture inside one beat
looks worse than the beat having fewer, longer shots.
"""
from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
from pathlib import Path

# Middle of the 3 to 5 second window the retention research points at. Shots are
# derived from narration spans, so this is a target rather than a hard cut.
SHOT_SECONDS = 2.6
# Under this a shot reads as a flicker rather than an image.
MIN_SHOT_SECONDS = 2.2
# A cue needs a few words behind it to be worth searching separately.
MIN_WORDS_PER_SHOT = 3

_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "that", "this", "these", "those", "it", "its",
    "is", "are", "was", "were", "be", "been", "being", "as", "than", "then",
    "there", "their", "they", "them", "we", "our", "you", "your", "has",
    "have", "had", "will", "would", "could", "can", "may", "might", "not",
    "one", "two", "about", "into", "over", "under", "more", "most", "very",
    "which", "what", "when", "where", "who", "how", "why", "all", "some",
}


def _keywords(text: str, limit: int = 4) -> list[str]:
    """Distinctive words from a narration span, in order of appearance.

    Proper nouns first: in this material they are the names (Apophis, Bennu,
    OSIRIS-REx) that make a search specific, and a generic query is exactly what
    returns yet another stock photograph of Earth.

    Partitioning is case-INSENSITIVE. Comparing against the capitalised list
    directly let a word that appears both sentence-initial and mid-sentence land
    in both buckets, burning two of the four keyword slots on one word.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text or "")
    proper, common, seen = [], [], set()
    for w in words:
        low = w.lower()
        if low in _STOP or low in seen:
            continue
        seen.add(low)
        (proper if w[0].isupper() else common).append(w)
    return (proper + common)[:limit]


def plan(duration: float, words: int | None = None) -> int:
    """How many shots to cut this beat into.

    Guards the boundaries because `duration` comes from ffprobe, which can hand
    back NaN or a negative on a malformed file.
    """
    try:
        d = float(duration)
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(d) or d <= MIN_SHOT_SECONDS:
        return 1
    n = max(1, int(round(d / SHOT_SECONDS)))
    if words is not None and words > 0:
        # Never ask for more shots than the narration can supply distinct cues
        # for, or the extra shots fall back to one shared cue, dedupe away, and
        # leave the beat visually shorter than its audio.
        n = min(n, max(1, words // MIN_WORDS_PER_SHOT))
    return n


def shot_cues(narration: str, fallback: str, n: int) -> list[str]:
    """One search cue per shot, each drawn from the words that shot covers."""
    words = (narration or "").split()
    if n <= 1 or not words:
        return [fallback or (narration or "")[:120]] * max(1, n)
    cues: list[str] = []
    total = len(words)
    for k in range(n):
        # Proportional slicing. Integer division with a fixed stride made the
        # LAST span absorb every remainder word, so the final shot got the
        # broadest, least specific cue, which is backwards.
        lo = (k * total) // n
        hi = ((k + 1) * total) // n
        span = words[lo:hi] or words[lo:lo + 1]
        kws = _keywords(" ".join(span))
        cues.append(" ".join(kws) if kws else (fallback or " ".join(span)))
    return cues


def digest(path: Path) -> str:
    """Content key for dedupe. Falls back to the path when unreadable, so an
    unreadable file still counts as itself rather than colliding with others."""
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return f"path:{path}"


def _listfile_quote(p: Path) -> str:
    """ffmpeg concat wraps each entry in single quotes, so a literal apostrophe
    in the path terminates the string early. Escape it the way ffmpeg expects."""
    return p.resolve().as_posix().replace("'", r"'\''")


def concat(parts: list[Path], out: Path, listfile: Path) -> Path:
    """Join a beat's shots into one silent track.

    Re-encodes rather than stream-copying: shots come from different sources
    (Ken-Burns renders and stock clips), and a concat-demuxer stream copy across
    mismatched encoder settings yields a file some players refuse.
    """
    parts = [Path(p) for p in parts if p]
    if not parts:
        raise RuntimeError("shot concat called with no shots")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")

    listfile.parent.mkdir(parents=True, exist_ok=True)
    listfile.write_text(
        "".join(f"file '{_listfile_quote(p)}'\n" for p in parts),
        encoding="utf-8")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
         "-pix_fmt", "yuv420p",
         "-an", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-2:])
        raise RuntimeError(
            f"shot concat failed (exit {proc.returncode})"
            + (f": {tail}" if tail else " with no stderr"))
    return out
