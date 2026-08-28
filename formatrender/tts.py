"""edge-tts narration. Free, no key, no quota.

Adapted from football-shorts-autopilot/modules/tts.py, which adapted it from
philosopher-pipeline. The `boundary="WordBoundary"` argument is load-bearing:
without it edge-tts collapses timings to sentence granularity and burned
captions drift off the voice.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import edge_tts

from . import config


@dataclass
class Narration:
    audio_path: Path
    duration_sec: float
    word_timings: list[tuple[str, float, float]] = field(default_factory=list)


class TTSError(RuntimeError):
    pass


async def _render(text: str, voice: str, rate: str, out: Path):
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    audio = bytearray()
    words: list[tuple[str, float, float]] = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"] / 1e7
            end = (chunk["offset"] + chunk["duration"]) / 1e7
            words.append((chunk["text"], start, end))
    if not audio:
        raise TTSError(f"edge-tts produced no audio for voice {voice!r}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(audio))
    return words


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True)
        return float(r.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 0.0


def narrate(text: str, out_dir: Path, slug: str, *,
            voice: str | None = None, rate: str | None = None) -> Narration:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3 = out_dir / f"{slug}.mp3"
    timings = asyncio.run(_render(text, voice or config.VOICE,
                                  rate or config.VOICE_RATE, mp3))
    dur = probe_duration(mp3)
    if dur <= 0:
        raise TTSError(f"could not read a duration from {mp3}; is ffprobe installed?")
    return Narration(audio_path=mp3, duration_sec=dur, word_timings=timings)
