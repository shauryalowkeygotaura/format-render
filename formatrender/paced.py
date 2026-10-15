"""Paced narration through paralang (Code/paralang): pauses and emphasis that
make the intense lines land, instead of one flat read.

    FR_TTS_PROVIDER=paralang                  the default provider (voice.py)
    FR_PARALANG_ENGINE=orpheus|aura           orpheus (default) = Groq, ACTS
                                              [serious]/[dramatic], needs only GROQ_API_KEY;
                                              aura = the andromeda voice with sentence
                                              gaps + "..." beats (DEEPGRAM_API_KEY)
    FR_PARALANG_VOICE=troy                    engine voice (defaults per engine)
    FR_PARALANG_MARKUP=0                      skip the LLM markup pass (text is
                                              used as written, marks and all)

Why both engines exist: the 2026-10-08 A/B (Code/paralang/samples/ab) showed
Aura cannot be paced by cutting and slowing fragments (it became unintelligible)
but stays clean with sentence-level gaps; Orpheus performs the emotion itself
from one request per paragraph. Which sounds better is an ear decision.

Captions keep working on either engine. orpheus: Groq Whisper word timestamps
(paralang.orpheus.align, windowed for long scripts), Deepgram alignment as the
backup if Groq fails and a DEEPGRAM_API_KEY exists. aura: Deepgram alignment
on the CLEAN text, the same path Aura narration uses.
Returns None on any failure so voice.narrate falls back like every provider.
"""
from __future__ import annotations

import os
import re
import subprocess
import wave
from pathlib import Path

from . import aura
from .tts import Narration, probe_duration

ENGINE = os.environ.get("FR_PARALANG_ENGINE", "orpheus").strip().lower()
VOICE = os.environ.get("FR_PARALANG_VOICE", "").strip() or None
MARKUP = os.environ.get("FR_PARALANG_MARKUP", "1").strip() != "0"


def available() -> str:
    try:
        import paralang  # noqa: F401
    except ImportError:
        return "not installed (pip install -e Code/paralang)"
    if ENGINE == "orpheus":
        need = []
        if not any(os.environ.get(k, "").strip() for k in ("GROQ_API_KEY", "GROQ_API_KEYS")):
            need = ["GROQ_API_KEY"]
    else:
        need = ["DEEPGRAM_API_KEY"] + (["GROQ_API_KEY"] if MARKUP else [])
    missing = [k for k in need if not os.environ.get(k, "").strip()]
    return f"engine={ENGINE}, " + (f"missing {', '.join(missing)}" if missing else "ready")


def _spoken_text(marked: str) -> str:
    """What is actually said: markup and [emotion] tags removed."""
    from paralang.emotion import _TAG_RE, clean
    return clean(_TAG_RE.sub(" ", marked))


def narrate(text: str, out_dir: Path, slug: str) -> Narration | None:
    try:
        from paralang.emotion import express
        from paralang.say import SR, aura_pcm, orpheus_pcm
    except ImportError:
        return None
    if not text.strip():
        return None

    marked = text
    if MARKUP:
        try:
            from paralang.markup import annotate
            marked = annotate(text)
        except Exception as e:  # noqa: BLE001 - markup is an enhancement, never a blocker
            print(f"  ! paralang markup skipped ({type(e).__name__}); using the text as written")

    try:
        script = express(marked)
        if ENGINE == "orpheus":
            pcm = orpheus_pcm(script, VOICE or "troy")
        else:
            pcm = aura_pcm(script, VOICE or aura.resolve_voice(None))
    except (Exception, SystemExit) as e:  # noqa: BLE001 - fall back, never lose a render
        print(f"  ! paralang {ENGINE} failed ({type(e).__name__}: {str(e)[:120]})")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    wav, mp3 = out_dir / f"{slug}.paced.wav", out_dir / f"{slug}.mp3"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                           "-codec:a", "libmp3lame", "-q:a", "3", str(mp3)], capture_output=True)
    wav.unlink(missing_ok=True)
    if proc.returncode != 0:
        mp3.unlink(missing_ok=True)
        return None
    dur = probe_duration(mp3)
    if dur <= 0:
        mp3.unlink(missing_ok=True)
        return None
    # The markup is kept beside the audio, so a reviewer can see why a line paused.
    (out_dir / f"{slug}.marked.txt").write_text(marked, encoding="utf-8")
    spoken = re.sub(r"\s+", " ", _spoken_text(marked)).strip()
    return Narration(audio_path=mp3, duration_sec=dur, word_timings=_timings(pcm, mp3, spoken))


def _timings(pcm: bytes, mp3: Path, spoken: str) -> list[tuple[str, float, float]]:
    """Caption word timings. Orpheus: Groq Whisper first (Groq-only setups get
    captions), Deepgram as backup. Aura: Deepgram, as before."""
    if ENGINE == "orpheus":
        try:
            from paralang.orpheus import align
            words = align(pcm, spoken)
            if words:
                return words
        except Exception as e:  # noqa: BLE001 - try Deepgram, then no captions, never no render
            print(f"  ! groq alignment failed ({type(e).__name__}: {str(e)[:100]})")
        if not os.environ.get("DEEPGRAM_API_KEY", "").strip():
            return []
    return aura._word_timings(mp3, spoken)
