"""Pluggable TTS backends, mirroring the two-tier design in Code/voicezero.

Default provider: paralang (Groq Orpheus, acts emotion, captions timed by Groq
Whisper; see paced.py). `tts.py`'s edge-tts path is the ALWAYS-THERE safety
net: no key, no install, no quota, and it returns the word timings the shorts
slicer needs for captions. FR_TTS_PROVIDER=edge makes it the primary again.

The contract every provider honours: if its package, model or key is missing,
or the call fails, the caller silently falls back to edge-tts. A render is
never lost to an un-installed optional backend.

    kokoro   Fully local, Apache-2.0, CPU-friendly. The best FREE quality bump
             for English narration and it stays $0 forever once the ~310 MB
             model is cached.  pip install kokoro-onnx
             NOTE: kokoro emits no word timings. Captions on shorts come from
             those timings, so a kokoro render either loses burned captions or
             pays for a forced-alignment pass. edge-tts remains the default for
             exactly this reason - the shorts are worth more than the timbre.

Deliberately NOT here: ElevenLabs. The whole point of this pipeline is $0, and
a metered voice in an unattended render loop is how a cheap channel becomes an
expensive one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import config
from . import aura
from . import paced
from .tts import Narration, TTSError, narrate as edge_narrate, probe_duration

PROVIDER = os.environ.get("FR_TTS_PROVIDER", "paralang").strip().lower()  # Orpheus via paralang; edge-tts on failure

# Kokoro voice ids: af_* American female, am_* American male, bf_/bm_ British.
KOKORO_VOICE = os.environ.get("FR_KOKORO_VOICE", "am_michael")
KOKORO_CACHE = Path(os.environ.get(
    "VOICEZERO_CACHE", str(Path.home() / ".cache" / "voicezero")))


def available() -> dict[str, str]:
    """What this machine can actually do, for the preflight command."""
    out = {"edge": "ready (fallback, no key, gives word timings)",
           "aura": aura.available(),
           "paralang": paced.available()}
    try:
        import kokoro_onnx  # noqa: F401
        model = KOKORO_CACHE / "kokoro-v1.0.onnx"
        out["kokoro"] = ("ready" if model.exists()
                         else f"installed, model not cached at {model}")
    except ImportError:
        out["kokoro"] = "not installed (pip install kokoro-onnx, ~310MB model)"
    return out


def _kokoro(text: str, out_dir: Path, slug: str) -> Narration | None:
    """Local Kokoro synthesis. Returns None so the caller can fall back."""
    try:
        import numpy as np
        import soundfile as sf
        from kokoro_onnx import Kokoro
    except ImportError:
        return None
    model = KOKORO_CACHE / "kokoro-v1.0.onnx"
    voices = KOKORO_CACHE / "voices-v1.0.bin"
    if not (model.exists() and voices.exists()):
        return None
    try:
        kok = Kokoro(str(model), str(voices))
        samples, rate = kok.create(text, voice=KOKORO_VOICE, speed=1.0, lang="en-us")
        out_dir.mkdir(parents=True, exist_ok=True)
        wav = out_dir / f"{slug}.wav"
        sf.write(str(wav), np.asarray(samples), rate)
    except Exception:
        # Any backend failure is a fallback, never a lost render.
        return None
    mp3 = out_dir / f"{slug}.mp3"
    if shutil.which("ffmpeg"):
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
             "-c:a", "libmp3lame", "-b:a", "192k", str(mp3)],
            capture_output=True)
        if proc.returncode == 0:
            wav.unlink(missing_ok=True)
        else:
            mp3 = wav
    else:
        mp3 = wav
    dur = probe_duration(mp3)
    if dur <= 0:
        return None
    # No word timings: kokoro-onnx does not expose them. Shorts captions
    # degrade to none rather than to wrong.
    return Narration(audio_path=mp3, duration_sec=dur, word_timings=[])


def narrate(text: str, out_dir: Path, slug: str, *, provider: str | None = None,
            warn: bool = True) -> Narration:
    """Synthesise one segment through the selected provider, edge-tts on failure."""
    choice = (provider or PROVIDER).lower()
    if choice in ("paralang", "paced"):
        # Paced narration (beats + emphasis, see paced.py). Falls through to
        # plain Aura, then edge-tts, so pacing can never cost a render.
        result = paced.narrate(text, Path(out_dir), slug)
        if result is not None:
            return result
        if warn:
            print("  ! paralang unavailable, falling back to plain aura")
        choice = "aura"
    if choice in ("aura", "deepgram"):
        # Aura-2 keeps captions working, unlike kokoro: it recovers word
        # timings by sending its own output back through Deepgram STT and
        # pairing those timestamps with the SUBMITTED text.
        result = aura.narrate(text, Path(out_dir), slug)
        if result is not None:
            return result
        if warn:
            print("  ! aura unavailable, falling back to edge-tts "
                  "(check DEEPGRAM_API_KEY)")
    if choice in ("kokoro", "local"):
        result = _kokoro(text, Path(out_dir), slug)
        if result is not None:
            return result
        if warn:
            print("  ! kokoro unavailable, falling back to edge-tts "
                  "(pip install kokoro-onnx)")
    return edge_narrate(text, out_dir, slug)
