"""Generate a shot's imagery from the script instead of searching for it.

Keyword stock search cannot guarantee that a clip is about what the narration
says. Measured on real renders it returned a beach for "the water pressure in
your pipes" and a jogger in bright sunlight inside a blackout story. Those are
not tuning failures; they are the error floor of retrieval. Working faceless
channels generate their visuals from the script instead, which makes the match
true by construction.

Provider is Pollinations: free, no key, no quota. Measured behaviour:

  concurrency   REFUSED - 4 of 6 parallel requests returned HTTP 429 instantly
  sequential    5/5 succeeded, ~39s per image
  size          returns 1024x576 regardless of what is requested

So this is strictly serial and slow. A 52-shot video costs about half an hour of
generation, a 225-shot one about two and a half. That is the price of imagery
that is actually about the right thing.

It renders positive subjects well ("substation transformer sparking at night")
and fails ABSENCE ("empty shelves, dim emergency lighting" came back as a fully
stocked, brightly lit supermarket). Prompts must therefore describe what IS in
frame, never what is missing; `_positive` enforces the common cases.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://image.pollinations.ai/prompt/"
# Images take about 39s. 90 bounds how long one hung request can hold the
# queue, since the lock is deliberately held across the request: releasing it
# early would let a second request overlap, which the service answers with 429.
TIMEOUT = 90
MAX_ATTEMPTS = 3
# The service rejects concurrent requests outright, so every caller queues here.
# Without this a parallel render loses most of its images to 429 in under two
# seconds and silently falls back to stock.
_LOCK = threading.Lock()
_MIN_GAP_SECONDS = 2.0
_last_request = 0.0

# Absence words the model renders as presence. "empty shelves" returns full
# shelves; "pitch dark hallway" returns a lit hallway. Rewrite to the positive
# thing that is actually visible.
_ABSENCE = {
    "empty": "bare stripped",
    "no light": "single dim light source",
    "pitch dark": "near black with one faint light",
    "dark": "unlit, lit only by a single source",
    "without power": "unlit",
    "deserted": "abandoned, litter on the ground",
}


def _positive(prompt: str) -> str:
    """Rewrite absence phrasing into something that can be drawn.

    Word-boundary matching, case-insensitive. A plain str.replace corrupted
    ordinary words that merely CONTAIN a key: "darkened room" became "unlit, lit
    only by a single sourceened room", and "emptying" became "bare strippeding".
    Title-casing the key also missed sentence case on multi-word keys, because
    "pitch dark".title() is "Pitch Dark", not "Pitch dark".

    Longest key first, so "pitch dark" is consumed before the shorter "dark"
    can match inside it.
    """
    out = prompt
    for bad in sorted(_ABSENCE, key=len, reverse=True):
        # Pattern built with chr(92) rather than a "\b" literal: written as an
        # escape inside a generated file it kept becoming the BACKSPACE byte
        # (0x08), producing a pattern that silently matched nothing.
        pattern = '\\b' + re.escape(bad) + '\\b'
        out = re.sub(pattern, _ABSENCE[bad], out, flags=re.I)
    return out


def build_prompt(visual: str, narration: str = "", tone: str = "") -> str:
    """The text handed to the generator.

    Prefers the brief's per-beat `visual` field, which is purpose-written to
    describe imagery, over keywords scraped from narration. That distinction is
    the whole point: narration keywords describe what is SAID, `visual`
    describes what should be SEEN.

    `tone` carries a per-video constraint. A blackout video must not contain
    bright sunny footage even when the subject keyword matches, and tone is the
    only thing that prevents it.
    """
    base = (visual or narration or "").strip()
    base = " ".join(base.split()[:28])
    parts = [_positive(base)]
    if tone:
        parts.append(tone.strip())
    parts.append("cinematic, photographic, 16:9")
    return ", ".join(p for p in parts if p)


def generate(prompt: str, dest: Path, *, seed: int | None = None) -> Path | None:
    """One image, or None. Serialised and retried; never raises."""
    if not prompt.strip():
        return None
    global _last_request
    q = urllib.parse.quote(prompt[:900], safe="")
    url = f"{ENDPOINT}{q}?width=1280&height=720&nologo=true"
    if seed is not None:
        url += f"&seed={int(seed)}"

    for attempt in range(MAX_ATTEMPTS):
        with _LOCK:
            gap = time.time() - _last_request
            if gap < _MIN_GAP_SECONDS:
                time.sleep(_MIN_GAP_SECONDS - gap)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=TIMEOUT).read()
            except Exception:
                data = b""
            finally:
                _last_request = time.time()
        # A truncated or error body is not an image. 5KB is far below any real
        # photograph and comfortably above an error page.
        if len(data) > 5000:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if _valid(dest):
                return dest
            dest.unlink(missing_ok=True)
        # Back off before retrying: the failure is nearly always rate limiting.
        # Not after the final attempt - that sleep delays the caller and then
        # returns None anyway.
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(3 * (attempt + 1))
    return None


def _valid(path: Path) -> bool:
    """Decodes as an image. Guards against saving an error page as a .jpg."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False
