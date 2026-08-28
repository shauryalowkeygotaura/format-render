"""Assemble the master video from a brief.

Per beat: narrate -> fetch a CC still -> Ken-Burns clip sized to the narration
-> mux that beat's audio. Then concat. Beat durations come from the narration
itself, never from the brief's `seconds` estimate, because a model's guess at
speech length is not the length edge-tts actually produces.

Checkpointed per beat: a killed render resumes instead of re-synthesising audio
and re-downloading imagery it already has.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import broll, captions, config, visuals, voice
from .brief import Brief


@dataclass
class BeatRender:
    index: int
    start: float
    end: float
    narration: str
    image: str
    image_found: bool
    word_timings: list = field(default_factory=list)


@dataclass
class RenderResult:
    video_path: Path
    duration: float
    beats: list[BeatRender]
    hero_still: Path | None
    real_image_ratio: float


class ComposeError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise ComposeError(f"{cmd[0]} failed: {' | '.join(tail) or proc.returncode}")


def _mux(clip: Path, audio: Path, out: Path) -> None:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip), "-i", str(audio),
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])


def _concat(parts: list[Path], out: Path, work: Path,
            caption_file: Path | None = None) -> None:
    listfile = work / "concat.txt"
    # Absolute paths: the concat demuxer resolves relative entries against the
    # list file's own directory, which would double the prefix.
    listfile.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
        encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", str(listfile)]
    # Burn captions in THIS pass rather than a second one. The concat already
    # re-encodes to libx264, so folding the subtitles filter in costs one
    # filtergraph and avoids a second generation of compression loss.
    # An empty caption file crashes the subtitles filter, hence the size check.
    if caption_file is not None and caption_file.exists() and caption_file.stat().st_size > 0:
        cmd += ["-vf", f"subtitles='{captions.escape_filter_path(caption_file)}'"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", str(out)]
    _run(cmd)


def _palette_pair(spec: dict) -> tuple[str, str]:
    """Gradient fallback colours drawn from the format's own palette."""
    pal = (spec.get("thumbnail_checklist", {}) or {}).get("signature_palette", [])
    hexes = [c["hex"].lstrip("#") for c in pal if c.get("hex")][:2]
    while len(hexes) < 2:
        hexes.append("0b0f1a" if not hexes else hexes[-1])
    return f"0x{hexes[0]}", f"0x{hexes[1]}"


def _pick_hero(renders: list[BeatRender]) -> Path | None:
    """Choose the still the thumbnail should be built from.

    Not simply the first real image: thumbnails live on colour and separation,
    and the thumbnail builder has to hit the format's measured saturation and
    contrast. Handing it the most chromatic still gives that correction loop
    something to work with instead of a flat grey photograph it cannot rescue.
    """
    best, best_score = None, -1.0
    for r in renders:
        if not r.image_found:
            continue
        p = Path(r.image)
        if not p.exists():
            continue
        try:
            from PIL import Image, ImageStat
            im = Image.open(p).convert("RGB").resize((120, 68))
            sat = ImageStat.Stat(im.convert("HSV")).mean[1] / 255.0
            con = ImageStat.Stat(im.convert("L")).stddev[0] / 255.0
        except (OSError, ImportError, IndexError):
            continue
        score = sat * 0.65 + con * 0.35
        if score > best_score:
            best, best_score = p, score
    if best is None:
        for r in renders:
            if r.image_found:
                return Path(r.image)
    return best


def compose(brief: Brief, spec: dict, work: Path, *, resume: bool = True,
            verbose: bool = True, use_broll: bool = False) -> RenderResult:
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / "beats.json"
    state = {}
    if resume and state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}

    size = config.RESOLUTION
    palette = _palette_pair(spec)
    # The hook leads the video; it is narration, not metadata.
    segments = ([("hook", brief.hook)] if brief.hook else []) + \
               [(f"beat{b.index}", b.narration) for b in brief.beats]
    cues = ([brief.beats[0].visual] if brief.hook else []) + \
           [b.visual for b in brief.beats]

    parts: list[Path] = []
    renders: list[BeatRender] = []
    cursor = 0.0
    found_count = 0

    for i, ((slug, text), cue) in enumerate(zip(segments, cues)):
        muxed = work / f"{slug}.mp4"
        cached = state.get(slug)
        if resume and muxed.exists() and cached:
            dur = float(cached["end"]) - float(cached["start"])
            parts.append(muxed)
            found_count += 1 if cached.get("image_found") else 0
            renders.append(BeatRender(
                index=i, start=cursor, end=cursor + dur, narration=text,
                image=cached.get("image", ""), image_found=cached.get("image_found", False),
                word_timings=[(w, s + cursor, e + cursor)
                              for w, s, e in cached.get("word_timings", [])]))
            cursor += dur
            if verbose:
                print(f"  [{i+1}/{len(segments)}] {slug}: cached ({dur:.1f}s)")
            continue

        narration = voice.narrate(text, work / "audio", slug)
        dur = max(narration.duration_sec, 1.0)

        still, ok = visuals.fetch_still(
            cue, work / "img" / f"{slug}.jpg", size,
            subject=brief.topic, palette=palette)
        found_count += 1 if ok else 0

        # Motion when a stock provider is keyed AND has something on-topic;
        # otherwise a Ken-Burns push on the real photograph, which is the
        # better default anyway (see broll.py).
        clip = None
        if use_broll:
            clip = broll.fetch_clip(cue, work / f"{slug}_broll.mp4", dur, size,
                                    topic=brief.topic)
        if clip is None:
            clip = visuals.ken_burns(still, work / f"{slug}_clip.mp4", dur, size,
                                     zoom_in=(i % 2 == 0))
        _mux(clip, narration.audio_path, muxed)
        parts.append(muxed)

        # edge-tts timings are local to this segment; shift to the master
        # timeline or every caption clusters at zero.
        renders.append(BeatRender(
            index=i, start=cursor, end=cursor + dur, narration=text,
            image=str(still), image_found=ok,
            word_timings=[(w, s + cursor, e + cursor)
                          for w, s, e in narration.word_timings]))
        state[slug] = {"start": cursor, "end": cursor + dur, "image": str(still),
                       "image_found": ok, "word_timings": narration.word_timings}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        cursor += dur
        if verbose:
            mark = "commons" if ok else "gradient"
            print(f"  [{i+1}/{len(segments)}] {slug}: {dur:5.1f}s  {mark}")

    if not parts:
        raise ComposeError("nothing rendered")

    # Name the master after the render, not after the work directory, or every
    # render on disk is called "work.mp4".
    master = work.parent / f"{work.parent.name}.mp4"

    # Captions on the long-form master. Until now only the vertical shorts got
    # them, because the caption builder lived inside shorts.py, so every
    # long-form upload shipped with no subtitles at all.
    # 0.08 rather than the shorts' 0.14: a 16:9 master has no Shorts UI overlay
    # to clear, so the text can sit lower without being crowded.
    all_words = [wt for b in renders for wt in b.word_timings]
    caption_file = None
    if all_words:
        caption_file = captions.build(
            all_words, 0.0, cursor, work / "master.ass",
            config.RESOLUTION, margin_frac=0.08)
        if verbose:
            print(f"  captions: {caption_file.name}")
    _concat(parts, master, work, caption_file)

    hero = _pick_hero(renders)

    return RenderResult(
        video_path=master,
        duration=cursor,
        beats=renders,
        hero_still=hero,
        real_image_ratio=round(found_count / max(1, len(segments)), 2),
    )


# A duration in a title is only a RUNTIME claim when nothing after it turns it
# into subject matter. "Explained in 12 Minutes" promises a length, while
# "6 Hours Before Impact" is about the asteroid. Flagging that second one is how
# a gate starts crying wolf, and a gate that cries wolf is a gate nobody reads.
# "of" is excluded on purpose: "12 Minutes of Pure Chaos" is a real claim.
_TITLE_DURATION = re.compile(
    r"(?:in\s+)?(\d+)\s*(?:\+\s*)?(minute|minutes|min|mins|hour|hours|hr|hrs)\b"
    r"(?!\s+(?:before|after|ago|earlier|later|into|from|without|since|until|till)\b)",
    re.IGNORECASE)


def title_duration_conflict(title: str, actual_sec: float) -> str | None:
    """Does the title promise a length the render does not deliver?

    The asteroid render went out as "Every Scariest Asteroid Explained in 12
    Minutes" over 2 minutes 54 seconds of video. Nobody caught it because
    nothing compared the two numbers. A title that overstates length by 4x is
    a bait complaint waiting to happen, and it is metadata, so it is cheap to
    catch here and free to fix without re-rendering.

    Returns a description of the conflict, or None when the title is honest.
    Tolerance is deliberately loose: "in 10 minutes" over 8 real minutes is
    normal rounding, 12 claimed over 3 actual is not.
    """
    m = _TITLE_DURATION.search(title or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    claimed = n * 3600 if unit.startswith(("hour", "hr")) else n * 60
    if claimed <= 0:
        return None
    ratio = actual_sec / claimed
    if 0.6 <= ratio <= 1.8:
        return None
    return (f"title claims {n} {unit} ({claimed/60:.0f}m) but the render is "
            f"{actual_sec/60:.1f}m ({ratio:.0%} of claimed)")


def write_manifest(brief: Brief, result: RenderResult, thumb: dict,
                   out: Path, shorts: list[dict] | None = None) -> Path:
    """Everything an upload step needs, so this project never touches OAuth."""
    checklist = [
        "Made for kids: NO (channel setting AND video setting)",
        "End screen added, pointing at the follow-up title above",
        "Auto-dubbing ON",
        "Altered/synthetic content disclosure: not required when all stills "
        "are Commons photography; required if you swap in generated imagery",
        "Attribution for CC-BY stills belongs in the description",
    ]
    blockers: list[str] = []

    conflict = title_duration_conflict(brief.title, result.duration)
    if conflict:
        blockers.append(f"retitle before upload. {conflict}")

    # Layout is the one thumbnail trait the correction loop cannot fix.
    # Saturation, contrast and brightness are continuous, so a pass can nudge
    # them; layout is categorical. build_best already tries every still, so a
    # layout miss on the winner means NO candidate had the right composition,
    # which is an imagery problem upstream, not a thumbnail problem. That
    # deserves to be visible instead of sitting in a field nobody reads.
    advice = (thumb or {}).get("layout_advice")
    if advice:
        blockers.append(f"thumbnail composition. {advice}")

    # Repeated stills are why the asteroid render showed one Ceres image for
    # its first 30 seconds. Cheap to detect here, and invisible otherwise.
    images = [b.image for b in result.beats if b.image]
    if images:
        unique = len(set(images))
        if unique < len(images):
            worst = max(set(images), key=images.count)
            blockers.append(
                f"imagery repeats: {len(images)} beats share {unique} distinct "
                f"stills, and '{Path(worst).name}' is used {images.count(worst)} "
                f"times. Consecutive reuse reads as a frozen video.")

    for b in blockers:
        # First items, and shouted, because an upload step scanning this list
        # should trip over them rather than find them under routine items.
        print(f"  WARNING: {b}")
    checklist = [f"BLOCKER: {b}" for b in blockers] + checklist

    payload = {
        "title": brief.title,
        "description": brief.description,
        "hashtags": brief.hashtags,
        "tags": brief.tags,
        "end_screen_followup": brief.end_screen_followup,
        "format_slug": brief.format_slug,
        "topic": brief.topic,
        "source_brief": str(brief.source) if brief.source else "",
        "video": str(result.video_path),
        "duration_sec": round(result.duration, 2),
        "real_image_ratio": result.real_image_ratio,
        "thumbnail": thumb,
        "shorts": shorts or [],
        "beats": [asdict(b) for b in result.beats],
        "upload_checklist": checklist,
        "title_duration_conflict": conflict,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
