"""Slice the 16:9 master into vertical 9:16 shorts with burned captions.

Adapted from football-shorts-autopilot/modules/shorts.py, with one change that
matters: segment selection is driven by the brief's `substance` array rather
than a `shortable` flag a scriptwriter had to set by hand.

That array is format-engine's list of original, checkable claims - the facts
that make the video not-a-reskin. A beat carrying one of those claims is, by
construction, the beat with a self-contained payoff. So the thing that keeps
the channel monetised also tells you which 30 seconds are worth clipping.

Everything here is ffmpeg. No key, no quota, no cost.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import captions, config

# Shorts sweet spot. Under ~20s reads as filler, over ~60s loses the loop.
TARGET_RANGE = (24.0, 58.0)
MAX_SHORTS = 4


def _keywords(text: str, limit: int = 8) -> set[str]:
    """Distinctive lowercase tokens, stopwords dropped."""
    stop = {"the", "and", "for", "with", "that", "this", "from", "its", "into",
            "are", "was", "were", "has", "have", "been", "over", "than", "which",
            "their", "there", "these", "those", "about", "would", "could"}
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", text or "")]
    # dict.fromkeys preserves first-seen order so `limit` keeps the opening
    # keywords, which are the topical ones, rather than an arbitrary subset.
    return set(list(dict.fromkeys(w for w in words if w not in stop))[:limit])


def _score_beat(beat, substance_keys: list[set[str]]) -> float:
    """How much standalone payoff does this beat carry?"""
    text = beat.narration
    if not text:
        return 0.0
    beat_keys = _keywords(text, 20)
    # Overlap with a substance claim is the dominant term: those are the
    # original facts, and a clip built on one has an actual reveal in it.
    best_overlap = max((len(beat_keys & k) / max(1, len(k)) for k in substance_keys),
                       default=0.0)
    score = best_overlap * 3.0
    # A concrete number is the single most clippable thing in a narration line.
    if re.search(r"\d", text):
        score += 0.6
    # Superlatives and stakes words carry hooks.
    if re.search(r"\b(most|largest|first|only|never|deadli\w+|worth|billion|million)\b",
                 text, re.I):
        score += 0.4
    return score


def select_segments(beats, substance: list[dict],
                    target: tuple[float, float] = TARGET_RANGE,
                    max_count: int = MAX_SHORTS) -> list[tuple[float, float, float]]:
    """Choose (start, end, score) windows on the master timeline.

    Windows grow forward to clear the minimum length and are trimmed at the
    maximum, then de-overlapped so two shorts are never the same footage.
    """
    lo, hi = target
    substance_keys = [_keywords(s.get("claim", "")) for s in substance
                      if isinstance(s, dict) and s.get("claim")]
    # Best payoff first, ties broken by earlier position (a tie means equal
    # payoff, and the earlier beat needs less context to make sense alone).
    ranked = sorted(((_score_beat(b, substance_keys), -i, i) for i, b in enumerate(beats)),
                    reverse=True)
    ranked = [(score, i) for score, _, i in ranked]

    chosen: list[tuple[float, float, float]] = []
    used: set[int] = set()
    for score, i in ranked:
        if len(chosen) >= max_count or score <= 0:
            break
        if i in used:
            continue
        b = beats[i]
        start, end = b.start, b.end
        span = {i}
        j = i + 1
        while (end - start) < lo and j < len(beats) and j not in used:
            end = beats[j].end
            span.add(j)
            j += 1
        if (end - start) < lo * 0.6:
            continue                      # too short even after growing
        if (end - start) > hi:
            end = start + hi
        if any(start < c_end and end > c_start for c_start, c_end, _ in chosen):
            continue                      # overlaps a short we already took
        chosen.append((start, end, score))
        used |= span
    return sorted(chosen, key=lambda c: c[0])


def build_captions(word_timings, seg_start: float, seg_end: float, out: Path) -> Path:
    """Captions on the SHORT's timeline. Timings arrive master-relative.

    Thin wrapper over captions.build, which the 16:9 master now shares. The
    0.14 margin lifts the text clear of the Shorts UI overlay.
    """
    return captions.build(word_timings, seg_start, seg_end, out,
                          config.SHORT_RESOLUTION, margin_frac=0.14)


_escape_filter_path = captions.escape_filter_path


def cut_vertical(master: Path, start: float, end: float, caption_file: Path,
                 out: Path) -> Path:
    """9:16 reframe: blurred fill behind the letterboxed content, captions burned.

    The caption file is ASS and owns its own geometry (see `build_captions`),
    so nothing here overrides its style. The parameter is `caption_file`, not
    `captions`, so it cannot shadow the `captions` module this now imports.
    """
    w, h = config.SHORT_RESOLUTION
    base = (f"[0:v]split=2[bg][fg];"
            f"[bg]scale={w}:{h},boxblur=30:5,setsar=1[bgb];"
            f"[fg]scale={w}:-1[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base]")
    # No force_style and no original_size: the .ass file carries PlayResX/Y and
    # its own [V4+ Styles] block. force_style would override exactly the values
    # that rewrite exists to make authoritative.
    # An empty caption file crashes the subtitles filter, so only burn when the
    # window actually produced Dialogue lines.
    if caption_file.exists() and caption_file.stat().st_size > 0:
        vf = base + f";[base]subtitles='{_escape_filter_path(caption_file)}'[v]"
    else:
        vf = base[:-len("[base]")] + "[v]"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(master),
         "-filter_complex", vf, "-map", "[v]", "-map", "0:a",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        raise RuntimeError(f"ffmpeg shorts cut failed: {' | '.join(tail)}")
    return out


def slice_shorts(master: Path, beats, substance: list[dict], work: Path,
                 *, max_count: int = MAX_SHORTS, verbose: bool = True) -> list[dict]:
    """Cut up to `max_count` vertical shorts from a rendered master."""
    all_words = [wt for b in beats for wt in b.word_timings]
    segments = select_segments(beats, substance, max_count=max_count)
    out: list[dict] = []
    for k, (start, end, score) in enumerate(segments):
        caps = build_captions(all_words, start, end, work / f"short{k}.ass")
        path = cut_vertical(master, start, end, caps, work.parent / f"short{k}.mp4")
        out.append({"path": str(path), "start": round(start, 2), "end": round(end, 2),
                    "duration": round(end - start, 2), "payoff_score": round(score, 2),
                    "captions": str(caps)})
        if verbose:
            print(f"  short{k}: {start:6.1f}-{end:6.1f}s "
                  f"({end - start:4.1f}s, payoff {score:.2f})")
    return out
