"""Copyright-safe imagery from Wikimedia Commons, plus Ken-Burns motion.

Why Commons rather than an AI image generator: CC and public-domain stills are
free, keyless, and safe to monetise over. Generated imagery is fine too, but it
triggers YouTube's altered-content disclosure and, at volume, is precisely the
"mass-produced" signature the inauthentic-content policy looks for. Real
sourced photography over original narration is the safer default.

Generalised from football-shorts-autopilot/modules/visuals.py, which hardcoded
football into its query ladder. Here the topic arrives from the brief.
"""
from __future__ import annotations

import re
import subprocess
import time
import urllib.parse
from pathlib import Path

import requests

from . import config

_HEADERS = {"User-Agent": f"format-render/1.0 ({config.WIKIMEDIA_CONTACT})"}

# Capitalised words that open a phrase without naming a subject.
_STOP = {
    "the", "a", "an", "in", "his", "her", "their", "its", "with", "and", "of",
    "on", "for", "to", "at", "as", "is", "from", "after", "before", "during",
    "young", "later", "early", "this", "that", "these", "those", "image",
    "photo", "shot", "view", "close", "wide",
}


def _proper_nouns(text: str) -> list[str]:
    """Distinct proper-noun tokens from a descriptive cue.

    Commons file titles are entity-named, so 'image of Ceres against a starfield'
    hits on 'Ceres' and never on the full sentence.
    """
    out, seen = [], set()
    for t in re.findall(r"[A-Z][A-Za-z'’.\-]+", text):
        if t.lower() in _STOP or len(t) <= 1:
            continue
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _content_words(text: str, limit: int = 4) -> list[str]:
    """Fallback when a cue carries no proper nouns at all ('image of the belt')."""
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", text) if w.lower() not in _STOP]
    return words[:limit]


def _commons_query(query: str) -> str | None:
    """One Commons file search. Returns a direct image URL or None."""
    try:
        r = requests.get(config.WIKIMEDIA_API, headers=_HEADERS, timeout=20, params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": "5",
            "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": "1600",
        })
        if r.status_code != 200:
            return None
        pages = r.json().get("query", {}).get("pages", {})
        # Shape-check inside the guard. Commons returns a dict-of-dicts here,
        # but an error payload or a formatversion change makes it a list, and
        # an AttributeError escaping a function contracted to return "URL or
        # None" aborts the whole render instead of falling back to a gradient.
        if not isinstance(pages, dict):
            return None
        ranked = sorted((p for p in pages.values() if isinstance(p, dict)),
                        key=lambda p: p.get("index", 999))
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return None
    # `ranked` is already index-ordered and shape-filtered above; re-sorting
    # pages.values() here would put the unguarded .get() back outside the try.
    for page in ranked:
        info = (page.get("imageinfo") or [{}])[0]
        if not isinstance(info, dict):
            continue
        mime = info.get("mime", "")
        url = info.get("thumburl") or info.get("url")
        # SVG excluded: ffmpeg's still decoder chokes on it.
        if not (url and mime.startswith("image/") and mime != "image/svg+xml"):
            continue
        # Skip within the result set rather than abandoning the whole query:
        # hit #1 being a classical nude does not mean hit #2 is unusable.
        if _is_unsafe(url):
            continue
        return url
    return None


# Filename tokens that must never end up on a monetised thumbnail. Commons
# hosts a great deal of classical art and medical photography, and a bare
# entity query lands on it constantly.
#
# Matched on WORD BOUNDARIES, not as substrings. Substring matching looked
# tempting and was wrong: "venus" blocks Venus_transit.jpg and "bath" blocks
# Bathymetry_map.jpg, quietly pushing legitimate beats to the gradient
# placeholder. Genuinely ambiguous words (venus, bath, leda) are therefore
# absent here; disambiguating the query is what handles those, not a blocklist.
_UNSAFE_FILE_TERMS = (
    "nude", "nudes", "naked", "akt", "desnudo", "desnuda", "erotic", "erotica",
    "odalisque", "nsfw", "topless", "lucretia", "corpse", "cadaver", "autopsy",
    "dissection", "lynching", "execution", "massacre", "atrocity", "mutilated",
)
_UNSAFE_RE = re.compile(
    r"(?<![a-z])(" + "|".join(_UNSAFE_FILE_TERMS) + r")(?![a-z])")


def _is_unsafe(url: str) -> bool:
    """Word-boundary match against the decoded filename."""
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1]).lower()
    return bool(_UNSAFE_RE.search(name))


def _search(query: str, subject: str | None, accept=None) -> str | None:
    """Progressively broader queries until Commons yields something usable.

    Every query carries the video's topic as context. A bare entity name is
    ambiguous in exactly the way that ruins a faceless video: "Ceres" returns
    the Roman goddess long before it returns the dwarf planet, and the render
    silently ships a 19th-century oil painting of a reclining nude on an
    astronomy channel. Disambiguated queries run first, the bare noun is a last
    resort, and results whose filenames look like classical nudes or gore are
    rejected outright.
    """
    nouns = _proper_nouns(query)
    candidates: list[str] = []
    # Disambiguated first: entity plus the topic it is supposed to belong to.
    if nouns and subject:
        candidates.append(f"{nouns[0]} {subject}")
        candidates.append(" ".join(nouns[:2]) + f" {subject}")
    if nouns:
        candidates.append(" ".join(nouns[:3]))
    words = _content_words(query)
    if words and subject:
        candidates.append(" ".join(words[:3]) + f" {subject}")
    if words:
        candidates.append(" ".join(words))
    if subject:
        candidates.append(subject)
    candidates.append(query)
    # Bare entity name last: highest ambiguity, so only when nothing else hit.
    if nouns:
        candidates.append(nouns[0])

    seen: set[str] = set()
    for cand in candidates:
        cand = cand.strip()
        if not cand or cand.lower() in seen:
            continue
        seen.add(cand.lower())
        url = _commons_query(cand)
        # accept(url, query): optional relevance judge (compose passes broll.still_is_relevant in
        # drawn/mix mode). A rejected pick tries the next, broader query instead of shipping a
        # book cover or a logo; all rejected -> the caller draws a card.
        if url and not _is_unsafe(url) and (accept is None or accept(url, query)):
            return url
    return None


def _download(url: str, dest: Path, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=30)
            if r.status_code in (429, 503) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                return False
            if not r.headers.get("Content-Type", "").startswith("image/"):
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return dest.stat().st_size > 1000
        except requests.RequestException:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False
    return False


def gradient_card(dest: Path, size: tuple[int, int],
                  colors: tuple[str, str] = ("0x0b0f1a", "0x16233a")) -> Path:
    """Deterministic fallback still so a missing image never fails the render."""
    w, h = size
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"gradients=s={w}x{h}:c0={colors[0]}:c1={colors[1]}:duration=1",
         "-frames:v", "1", str(dest)],
        check=True, capture_output=True)
    return dest


def fetch_exact(query: str, dest: Path) -> tuple[Path, bool]:
    """One exact Commons query (an authored `photos` entry), no broadening.

    Broadening is what fetch_still is for; an authored photo query names a
    specific real image, and a looser match would quietly put the wrong
    subject on screen. (dest, False) means "not found, fall back".
    """
    url = _commons_query(query)
    if url and not _is_unsafe(url) and _download(url, dest):
        return dest, True
    return dest, False


def fetch_still(cue: str, dest: Path, size: tuple[int, int],
                subject: str | None = None,
                palette: tuple[str, str] | None = None, accept=None) -> tuple[Path, bool]:
    """Resolve a beat's visual cue to a local image. Always returns a path.

    `found` is False when the still is the gradient placeholder, which lets the
    caller report honestly how much of the video is real imagery.
    """
    url = _search(cue, subject, accept)
    if url and _download(url, dest):
        return dest, True
    gradient_card(dest, size, palette or ("0x0b0f1a", "0x16233a"))
    return dest, False


def ken_burns(image: Path, out: Path, duration: float,
              size: tuple[int, int], zoom_in: bool = True) -> Path:
    """Slow push/pull over a still -> silent mp4.

    zoompan needs a frame budget (d = duration * fps), and the source must be
    upscaled first or the crop runs out of pixels and the pan judders.
    """
    w, h = size
    fps = config.FPS
    frames = max(1, int(duration * fps))
    z = config.KEN_BURNS_ZOOM
    step = config.KEN_BURNS_STEP
    z_expr = (f"min(zoom+{step},{z})" if zoom_in
              else f"if(lte(zoom,1.0),{z},max(zoom-{step},1.0))")
    vf = (
        f"scale={w*2}:-1,"
        f"zoompan=z='{z_expr}':d={frames}:s={w}x{h}:fps={fps}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
        f"format=yuv420p"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(image),
         "-t", f"{duration:.3f}", "-vf", vf, "-r", str(fps),
         # Intermediate: re-encoded again downstream, so spend bits not time.
         # Measured on a 2.6s 1080p push: 1.06s vs 2.72s, SSIM 0.970 vs 0.959.
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
         "-pix_fmt", "yuv420p", str(out)],
        check=True, capture_output=True)
    return out
