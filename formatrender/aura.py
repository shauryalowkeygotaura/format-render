"""Deepgram Aura-2 narration, with word timings recovered via Deepgram STT.

WHY THIS EXISTS
---------------
edge-tts is the default in voice.py for one reason only: it returns word
timings, and burned captions come from those. kokoro sounds better and was
still not promoted, because it emits no timings and "the shorts are worth more
than the timbre".

Aura-2 sounds better than both and has the same problem: a TTS endpoint returns
audio, not alignment. Wiring it the way kokoro is wired would buy a nicer voice
and silently cost every caption, which is a bad trade right after the caption
path was fixed.

So this does not accept that trade. It synthesises with Aura-2, then sends the
rendered audio straight back to Deepgram's STT with word-level timestamps and
uses those as the alignment. Same vendor, same key, no new credential, and the
timings describe the audio that actually exists rather than a model's estimate
of it.

The STT pass is the fallback-bearing step: if it fails, the narration is still
returned with EMPTY timings rather than being thrown away, because a render with
no captions beats no render at all. That mirrors kokoro's contract.

Enable with FR_TTS_PROVIDER=aura. Voice via FR_AURA_VOICE.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .tts import Narration, probe_duration

SPEAK_URL = "https://api.deepgram.com/v1/speak"
LISTEN_URL = ("https://api.deepgram.com/v1/listen"
              "?model=nova-3&smart_format=false&punctuate=true&language=en")

# Shaurya picked these by ear on 2026-08-28 from a 6-way A/B:
# andromeda was "gooood", orion "fine". andromeda is therefore the default.
DEFAULT_VOICE = "aura-2-andromeda-en"

# Whitelist, not escaping, per the vault rule. `voice` reaches a URL query
# string, so an unvalidated value could smuggle in extra parameters with & or #
# and change the container or encoding out from under the caller. A Deepgram
# voice id is always lowercase alphanumerics and hyphens, so anything else is
# refused outright rather than cleaned up into something that might still work.
_VOICE_RE = re.compile(r"^[a-z0-9-]{3,40}$")

_TIMEOUT = 60
# Below this a 200 response is an error page or a truncated stream, not speech.
_MIN_AUDIO_BYTES = 2000


class AuraError(RuntimeError):
    pass


def _key() -> str:
    return (os.environ.get("DEEPGRAM_API_KEY") or "").strip()


def resolve_voice(voice: str | None = None) -> str:
    """Validated voice id. Raises rather than silently substituting."""
    v = (voice or os.environ.get("FR_AURA_VOICE") or DEFAULT_VOICE).strip()
    if not _VOICE_RE.match(v):
        raise AuraError(
            f"refusing voice id {v!r}: expected lowercase letters, digits and "
            f"hyphens only (e.g. {DEFAULT_VOICE}). It goes into a URL query "
            f"string, so it is whitelisted rather than escaped.")
    return v


def available() -> str:
    if not _key():
        return "no DEEPGRAM_API_KEY (falls back to edge)"
    try:
        v = resolve_voice()
    except AuraError as e:
        return f"misconfigured: {e}"
    return f"ready ({v}, timings via Deepgram STT)"


def _post(url: str, body: bytes, content_type: str) -> bytes:
    """POST and return the body. urlopen raises HTTPError on non-2xx already,
    so there is no status branch here pretending to catch that."""
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Token {_key()}",
        "Content-Type": content_type,
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read()


def _norm(w: str) -> str:
    """Comparison key. Punctuation-only tokens normalise to "" and simply never
    match, which is correct: they carry no sound to align to."""
    return "".join(ch for ch in w.lower() if ch.isalnum())


# Whole-sequence similarity below this means the transcript is not describing
# this audio, so every timing derived from it would be a guess.
_MIN_ALIGN_RATIO = 0.55


def _word_timings(audio: Path, spoken: str) -> list[tuple[str, float, float]]:
    """Timings for `spoken`, positioned by asking Deepgram where words landed.

    The returned WORDS always come from `spoken`, never from the transcript.
    Deepgram is used purely as a clock: if it mishears "Apophis" as "a office",
    using its tokens would burn a wrong word into the video, which is worse than
    no caption at all.

    Token counts do NOT line up in practice and cannot be required to. Deepgram
    expands numbers and dates, so "2029." comes back as "twenty twenty nine":
    12 submitted tokens against 14 heard on one ordinary sentence. Requiring
    equality rejected almost every real segment and left videos uncaptioned.

    So the sequences are ALIGNED. Matching runs map one to one; where they
    diverge, the submitted words are spread across the time span the
    corresponding heard words occupy, which is what puts "2029." on the moment
    "twenty twenty nine" is spoken.

    The confidence gate is the SequenceMatcher ratio over the whole pair, not a
    count of literal matches. Counting only literal matches would fail the exact
    number-heavy sentences this alignment exists to handle.
    """
    words = [w for w in spoken.split() if w.strip()]
    if not words:
        return []
    try:
        raw = _post(LISTEN_URL, audio.read_bytes(), "audio/mpeg")
        data = json.loads(raw.decode())
        heard = data["results"]["channels"][0]["alternatives"][0].get("words") or []
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            KeyError, IndexError, ValueError, TypeError):
        return []
    if not heard:
        return []

    try:
        h_words = [(_norm(h["word"]), float(h["start"]), float(h["end"])) for h in heard]
    except (KeyError, TypeError, ValueError):
        return []

    a = [_norm(w) for w in words]
    b = [h[0] for h in h_words]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    if sm.ratio() < _MIN_ALIGN_RATIO:
        return []

    out: list[tuple[str, float, float]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                _, st, en = h_words[j1 + k]
                out.append((words[i1 + k], st, en))
            continue
        if i1 == i2:
            continue  # heard extra tokens; no submitted word needs placing
        if j1 == j2:
            # Submitted words with no heard counterpart. Pin them to the
            # boundary rather than dropping them, so the caption text stays
            # complete and only the timing is approximate.
            at = h_words[j1][1] if j1 < len(h_words) else h_words[-1][2]
            for i in range(i1, i2):
                out.append((words[i], at, at))
            continue
        span_start = h_words[j1][1]
        span_end = h_words[j2 - 1][2]
        n = i2 - i1
        step = (span_end - span_start) / n if n else 0.0
        for k in range(n):
            out.append((words[i1 + k], span_start + step * k,
                        span_start + step * (k + 1)))

    out.sort(key=lambda t: t[1])
    return out


def narrate(text: str, out_dir: Path, slug: str, *,
            voice: str | None = None) -> Narration | None:
    """Synthesise one segment. Returns None so the caller can fall back.

    Keyword-only `voice`, matching tts.narrate and voice.narrate.
    """
    if not _key() or not text.strip():
        return None
    try:
        v = resolve_voice(voice)
    except AuraError:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    mp3 = out_dir / f"{slug}.mp3"

    try:
        audio = _post(f"{SPEAK_URL}?model={v}",
                      json.dumps({"text": text}).encode(),
                      "application/json")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None

    if len(audio) < _MIN_AUDIO_BYTES:
        return None
    mp3.write_bytes(audio)

    dur = probe_duration(mp3)
    if dur <= 0:
        # Deepgram returns mp3 regardless of extension; if ffprobe cannot read
        # it, nothing downstream can either. Remove it rather than leaving a
        # corrupt file for a later resume to pick up as cached narration.
        mp3.unlink(missing_ok=True)
        return None

    return Narration(audio_path=mp3, duration_sec=dur,
                     word_timings=_word_timings(mp3, text))
