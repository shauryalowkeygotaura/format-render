"""Assemble the master video from a brief.

Per beat: narrate -> fetch a CC still -> Ken-Burns clip sized to the narration
-> mux that beat's audio. Then concat. Beat durations come from the narration
itself, never from the brief's `seconds` estimate, because a model's guess at
speech length is not the length edge-tts actually produces.

Checkpointed per beat: a killed render resumes instead of re-synthesising audio
and re-downloading imagery it already has.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import concurrent.futures as cf

import drawn_scenes as ds  # vendored at repo root, see drawn_scenes/VENDORED.txt

from . import broll, captions, cards, config, imagegen, shots, visuals, voice
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
    # License credit for the music bed, appended to the upload description.
    music_credit: str = ""


# Six keeps the network and ffmpeg busy without thrashing a machine that is
# also encoding. Each worker is one download plus one x264 encode.
BROLL_WORKERS = 6
NARRATION_WORKERS = 3


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


# Final-mix loudness. YouTube normalises to about -14 LUFS; the first
# engineering-failures render measured -26.4 LUFS integrated, i.e. ~12 dB
# quieter than everything around it, and Shaurya's first note was "the voice is
# too low". loudnorm on the final mix fixes it for every engine and every video.
TARGET_LUFS = -14.0
TRUE_PEAK = -1.5

# Background music: any .mp3/.wav/.m4a in assets/music (royalty-safe tracks
# only, e.g. YouTube Audio Library). None present -> voice only, still
# normalised. The bed sits at MUSIC_GAIN and ducks under the narration.
MUSIC_DIR = Path(__file__).resolve().parent.parent / "assets" / "music"
MUSIC_GAIN = 0.22


def music_credit(track: Path | None) -> str:
    """The attribution a CC-BY track requires, from assets/music/CREDITS.json.

    A track with no recorded credit returns a loud placeholder instead of "",
    so an unlicensed-looking upload is caught at review, not by Content ID.
    """
    if track is None:
        return ""
    try:
        credits = json.loads((MUSIC_DIR / "CREDITS.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        credits = {}
    return credits.get(track.name) or f"MUSIC CREDIT MISSING for {track.name}: check its license before upload"


def pick_music(seed: str) -> Path | None:
    """A stable track per video (crc32 of the seed, not hash(): per-process salt)."""
    if not MUSIC_DIR.is_dir():
        return None
    tracks = sorted(p for p in MUSIC_DIR.iterdir()
                    if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac"))
    if not tracks:
        return None
    return tracks[zlib.crc32(seed.encode("utf-8")) % len(tracks)]


def _premix(music: Path | None) -> str:
    """Audio graph up to (not including) loudness, ending in [m]."""
    # Gentle narrator compression first. TTS speech is peaky: without it the
    # true-peak ceiling stopped loudnorm at -15.4 LUFS instead of -14, and the
    # voice sounded thin next to real channels.
    comp = "acompressor=threshold=-20dB:ratio=3:attack=5:release=120:makeup=2"
    if music is None:
        return f"[0:a]{comp}[m]"
    # Duck the bed under the voice: the narration keys a compressor on the
    # music, so the music swells in the gaps and drops while words land.
    return (f"[0:a]{comp},asplit=2[voice][key];"
            f"[1:a]volume={MUSIC_GAIN}[bed];"
            f"[bed][key]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450[duck];"
            f"[voice][duck]amix=inputs=2:duration=first:normalize=0[m]")


def _loudnorm_pass2(inputs_cmd: list[str], music: Path | None) -> str:
    """Two-pass loudnorm: measure the real mix, then correct it linearly.

    Single-pass loudnorm undershot by ~2 LU on a test cut (-16.1 vs -14), and
    volume was the first complaint about the first render. Pass 1 measures the
    finished mix (audio only, fast); pass 2 applies the measured values with
    linear=true. Falls back to single-pass if the measurement cannot be read.
    """
    base = f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11"
    probe = inputs_cmd + ["-filter_complex", f"{_premix(music)};[m]{base}:print_format=json[a]",
                          "-map", "[a]", "-vn", "-f", "null", "-"]
    probe = [a for a in probe if a not in ("-loglevel", "error")] + ["-hide_banner"]
    proc = subprocess.run(probe, capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr or "", re.S)
    if proc.returncode != 0 or not m:
        return base
    try:
        j = json.loads(m.group(0))
        return (f"{base}:measured_I={j['input_i']}:measured_TP={j['input_tp']}"
                f":measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}"
                f":offset={j['target_offset']}:linear=true")
    except (ValueError, KeyError):
        return base


def _concat(parts: list[Path], out: Path, work: Path,
            caption_file: Path | None = None, music: Path | None = None) -> None:
    listfile = work / "concat.txt"
    # Absolute paths: the concat demuxer resolves relative entries against the
    # list file's own directory, which would double the prefix.
    listfile.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
        encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", str(listfile)]
    if music is not None:
        # Loop the bed for the whole video; -shortest below trims it.
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    # Burn captions in THIS pass rather than a second one. The concat already
    # re-encodes to libx264, so folding the subtitles filter in costs one
    # filtergraph and avoids a second generation of compression loss.
    # An empty caption file crashes the subtitles filter, hence the size check.
    vchain = "null"
    if caption_file is not None and caption_file.exists() and caption_file.stat().st_size > 0:
        vchain = f"subtitles='{captions.escape_filter_path(caption_file)}'"

    loud = _loudnorm_pass2(cmd, music)
    achain = f"{_premix(music)};[m]{loud}[a]"
    cmd += ["-filter_complex", f"[0:v]{vchain}[v];{achain}",
            "-map", "[v]", "-map", "[a]",
            # The only encode a viewer sees: intermediates are near-lossless, so
            # this pass sets final quality. CRF 20 (was default 23).
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", str(out)]
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
            use_gen: bool = False, tone: str = "",
            verbose: bool = True, use_broll: bool = False,
            drawn: str = "stock", drawn_style: str = "auto") -> RenderResult:
    """drawn: stock (photos/footage only) | mix (Groq picks, per beat, whether a code-drawn
    paper/sketch card says it better; also replaces gradient fallbacks) | drawn (a card every beat)."""
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
    # Exact real-photo queries per segment (see Beat.photos), aligned with cues.
    photo_lists = ([brief.hook_photos] if brief.hook else []) + \
                  [b.photos for b in brief.beats]
    card_lists = ([[]] if brief.hook else []) + [b.cards for b in brief.beats]

    parts: list[Path] = []
    renders: list[BeatRender] = []
    # Stock clip ids already used ANYWHERE in this video.
    # Claims, not a plain set: the prefetch below fetches shots concurrently
    # and a plain set lets two workers pick the same clip before either
    # records it.
    used_clip_ids = broll.Claims(state.get('_used_clip_ids', []))
    cursor = 0.0
    found_count = 0

    # Narrate every uncached segment CONCURRENTLY before the visual loop.
    # Narration (LLM markup, TTS per sentence, the STT timing pass) is all
    # network wait and was done one segment at a time, interleaved with the
    # visual work that does not depend on it. Three workers stays inside the
    # Groq and Deepgram per-minute limits; voice.narrate never raises (it
    # falls back to edge-tts), and a failed prefetch is simply redone inline.
    narr_cache: dict[str, object] = {}
    pending = [(slug, text) for slug, text in segments
               if not (resume and (work / f"{slug}.mp4").exists() and state.get(slug))]
    if pending:
        def _narr(item):
            slug_, text_ = item
            try:
                return slug_, voice.narrate(text_, work / "audio", slug_)
            except Exception:  # noqa: BLE001 - redone inline below
                return slug_, None
        with cf.ThreadPoolExecutor(max_workers=NARRATION_WORKERS) as pool:
            for slug_, n_ in pool.map(_narr, pending):
                if n_ is not None:
                    narr_cache[slug_] = n_

    # Code-drawn cards (drawn_scenes/, shared with vtuber-render & co): one Groq call over all beats.
    drawn = drawn if drawn in ("stock", "mix", "drawn") else "stock"
    drawn_specs: list[dict] = []
    drawn_beats: set[int] = set()
    if drawn != "stock":
        drawn_specs = ds.plan([t for _, t in segments], decide=(drawn == "mix"),
                              video="a 16:9 documentary-style explainer video",
                              stock="a real photograph or stock video of the subject")
        drawn_beats = set(range(len(segments))) if drawn == "drawn" else ds.decide(drawn_specs)
        if verbose:
            print(f"  drawn cards ({drawn}): beats {sorted(drawn_beats)}")
    drawn_look = ds.choose_style(drawn_style, brief.title)

    def _draw(i: int, k: int, seconds: float) -> Path | None:
        if not drawn_specs:
            return None
        try:
            return ds.render(drawn_look, drawn_specs[i], seconds, work / "drawn", size=size,
                             seed=7 + i * 31 + k)
        except (RuntimeError, ValueError) as e:
            print(f"  drawn card failed (beat {i}, shot {k}): {e}")
            return None

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

        narration = narr_cache.get(slug) or voice.narrate(text, work / "audio", slug)
        dur = max(narration.duration_sec, 1.0)

        # One beat used to be ONE still held for the whole beat, which on a real
        # render meant a single photograph on screen for 29 seconds against a
        # 3-to-5 second target. Cut it into shots instead, each with its own cue
        # taken from the narration it covers, so the picture tracks what is
        # being said and a bad match costs seconds rather than half a minute.
        n_shots = shots.plan(dur, words=len(text.split()))
        # A purpose-written `visual` cue beats keywords scraped from narration.
        # The 2026-10-22 Machchhu render searched stock with narration words
        # ("Two different causes" -> a sunny festival crowd, "more than five
        # hundred people" -> men pushing a tree), while the beat's own cue said
        # "suspension bridge cables crowd evening". When the brief carries a
        # visual, every shot uses it; variety comes from `variant=k` and the
        # used-clip dedupe, not from swapping in unrelated words.
        # Stock VIDEO only: stills keep per-shot cues, because fetch_still has no
        # variant and one repeated query would return one photo, dedupe away,
        # and hold a single image for the whole beat. Commons stills also do
        # better on narration names (Machchhu, Morbi) than on generic cues.
        authored = bool(cue and cue.strip() and cue.strip() != text.strip()[:120])
        cue_list = shots.shot_cues(text, cue, n_shots)
        broll_cues = [cue] * n_shots if authored else cue_list
        photos = photo_lists[i] if i < len(photo_lists) else []
        card_specs = card_lists[i] if i < len(card_lists) else []
        # Designed cards first, then named real photos, then stock. Never more
        # cards+photos than the beat has shots for.
        card_specs = card_specs[:n_shots]
        photos = photos[:max(0, n_shots - len(card_specs))]
        card_pngs = (cards.render_cards(card_specs, work / "cards", slug)
                     if card_specs else [])
        designed = len(card_specs) + len(photos)
        shot_files: list[Path] = []
        seen_digests: set[str] = set()
        first_still: Path | None = None
        first_ok = False

        seg = dur / n_shots
        (work / "shots").mkdir(parents=True, exist_ok=True)
        # Fetch every shot's footage CONCURRENTLY, then assemble in order.
        # Only the fetch is parallel: the assembly below carries order-dependent
        # state (seen_digests, first_still, append order) that a thread pool
        # would scramble. Measured 4.6s per clip after the download and encode
        # fixes, so 225 shots cost 17 minutes serially; the work is network and
        # ffmpeg, both of which idle a 18-core machine one clip at a time.
        prefetched: dict[int, Path | None] = {}
        if use_broll:
            def _grab(k: int, cue_text: str):
                return k, broll.fetch_clip(
                    cue_text, work / "shots" / f"{slug}_{k:02d}_b.mp4",
                    seg, size, topic=brief.topic, used_ids=used_clip_ids,
                    # Forced faces only for scraped cues: an authored cue already
                    # says whether people belong in frame, and a random stock
                    # model in a disaster story is worse than no face at all.
                    want_face=(not authored and k % broll.FACE_EVERY_NTH_SHOT == 1),
                    variant=k)
            with cf.ThreadPoolExecutor(max_workers=BROLL_WORKERS) as pool:
                todo = [(k, q) for k, q in enumerate(broll_cues) if k >= designed]
                for k, clip in pool.map(lambda a: _grab(*a), todo):
                    prefetched[k] = clip

        for k, shot_cue in enumerate(cue_list):
            shot_out = work / "shots" / f"{slug}_{k:02d}.mp4"

            clip = prefetched.get(k)
            # A beat Groq marked "drawn" gets its card in the first slot no designed card/photo owns.
            if i in drawn_beats and k == designed:
                clip = _draw(i, k, seg) or clip

            # A designed card for this shot wins over everything else.
            if clip is None and k < len(card_pngs) and card_pngs[k] is not None:
                clip = visuals.ken_burns(card_pngs[k], shot_out, seg, size,
                                         zoom_in=True)
                if first_still is None:
                    first_still, first_ok = card_pngs[k], True
            # Then a named real photo.
            pk = k - len(card_specs)
            if clip is None and 0 <= pk < len(photos):
                still, ok = visuals.fetch_exact(
                    photos[pk], work / "img" / f"{slug}_{k:02d}_p.jpg")
                if ok:
                    dg = shots.digest(still)
                    if dg not in seen_digests:
                        seen_digests.add(dg)
                        if first_still is None:
                            first_still, first_ok = still, True
                        clip = visuals.ken_burns(still, shot_out, seg, size,
                                                 zoom_in=((i + k) % 2 == 0))

            # Generated imagery, when asked for. Built from the BEAT'S `visual`
            # field, which is purpose-written to describe what should be seen,
            # rather than from keywords scraped out of the narration. That is
            # the whole difference: stock search can only approximate the
            # subject, generation makes it true by construction.
            if clip is None and use_gen:
                prompt = imagegen.build_prompt(cue, shot_cue, tone)
                img = imagegen.generate(prompt, work / "gen" / f"{slug}_{k:02d}.jpg",
                                        seed=abs(hash((slug, k))) % 100000)
                if img is not None:
                    clip = visuals.ken_burns(img, shot_out, seg, size,
                                             zoom_in=((i + k) % 2 == 0))

            if clip is None:
                still, shot_ok = visuals.fetch_still(
                    shot_cue, work / "img" / f"{slug}_{k:02d}.jpg", size,
                    subject=brief.topic, palette=palette)
                if not shot_ok and drawn != "stock":
                    # No real photo: a drawn card carries the line, a gradient carries nothing.
                    card = _draw(i, k, seg)
                    if card is not None:
                        shot_files.append(card)
                        continue
                # Repeating a picture INSIDE one beat looks worse than the beat
                # having fewer shots, and Commons returns the same file for
                # related queries often enough that this fires in practice.
                d = shots.digest(still)
                if d in seen_digests and len(shot_files) > 0:
                    continue
                seen_digests.add(d)
                if first_still is None:
                    first_still, first_ok = still, shot_ok
                clip = visuals.ken_burns(still, shot_out, seg, size,
                                         zoom_in=((i + k) % 2 == 0))
            shot_files.append(clip)

        if not shot_files:
            # Every shot deduped away: fall back to one still for the beat
            # rather than emitting a beat with no picture at all.
            fb_still, first_ok = visuals.fetch_still(
                cue, work / "img" / f"{slug}.jpg", size,
                subject=brief.topic, palette=palette)
            first_still = fb_still
            shot_files = [visuals.ken_burns(fb_still, work / "shots" / f"{slug}_00.mp4",
                                            dur, size, zoom_in=(i % 2 == 0))]

        # Resolve what this beat actually got BEFORE counting it. first_still
        # stays None when stock video supplied every shot: there is no still to
        # record, but the beat did get real footage, so counting it as a
        # gradient fallback would understate real_image_ratio and print
        # "gradient" for a beat that is nothing of the sort.
        if first_still is None:
            still_ref = ""          # no still exists; not an image path
            first_ok = True         # stock footage IS real imagery
        else:
            still_ref = str(first_still)
        found_count += 1 if first_ok else 0

        if len(shot_files) == 1:
            clip = shot_files[0]
        else:
            # Shots are cut to dur/n_shots each, so the track can drift a frame
            # or two from the audio. _mux uses -shortest, which trims rather
            # than desyncs.
            clip = shots.concat(shot_files, work / f"{slug}_track.mp4",
                                work / "shots" / f"{slug}_list.txt")
        _mux(clip, narration.audio_path, muxed)
        parts.append(muxed)

        # edge-tts timings are local to this segment; shift to the master
        # timeline or every caption clusters at zero.
        renders.append(BeatRender(
            index=i, start=cursor, end=cursor + dur, narration=text,
            image=still_ref, image_found=first_ok,
            word_timings=[(w, s + cursor, e + cursor)
                          for w, s, e in narration.word_timings]))
        state[slug] = {"start": cursor, "end": cursor + dur, "image": still_ref,
                       "image_found": first_ok, "word_timings": narration.word_timings}
        # Persist so a RESUMED render does not re-use clips that cached
        # segments already show.
        state["_used_clip_ids"] = sorted(used_clip_ids)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        cursor += dur
        if verbose:
            mark = "footage" if first_ok else "gradient"
            print(f"  [{i+1}/{len(segments)}] {slug}: {dur:5.1f}s  {len(shot_files)} shots ({dur/max(len(shot_files),1):.1f}s each)  {mark}")

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
        # Emphasis words come from paralang's markup (`*word*`), saved beside
        # each segment's audio by paced.py. They pop on screen as the word lands.
        pops: set[str] = set()
        for marked in (work / "audio").glob("*.marked.txt"):
            try:
                pops.update(re.findall(r"\*([^*\n]+)\*", marked.read_text(encoding="utf-8")))
            except OSError:
                pass
        pops = {w for p_ in pops for w in p_.split()}
        caption_file = captions.build(
            all_words, 0.0, cursor, work / "master.ass",
            config.RESOLUTION, margin_frac=0.08, pops=pops)
        if verbose:
            print(f"  captions: {caption_file.name}")
    music = pick_music(brief.title)
    _concat(parts, master, work, caption_file, music=music)

    hero = _pick_hero(renders)

    return RenderResult(
        video_path=master,
        duration=cursor,
        beats=renders,
        hero_still=hero,
        real_image_ratio=round(found_count / max(1, len(segments)), 2),
        music_credit=music_credit(music),
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
    # Compare CONTENT, not filenames. compose() gives the hook beat[0]'s visual
    # cue, so the hook and the first beat fetch the same picture into two
    # different files. A filename comparison called that "5 distinct stills"
    # and stayed silent while the video opened on the same image twice, which
    # is precisely the defect this check exists to catch.
    digests: dict[str, list[str]] = {}
    for b in result.beats:
        if not b.image:
            continue
        f = Path(b.image)
        try:
            key = hashlib.sha1(f.read_bytes()).hexdigest()
        except OSError:
            key = str(f)  # unreadable: fall back to the path so it still counts
        digests.setdefault(key, []).append(f.name)
    if digests:
        total = sum(len(v) for v in digests.values())
        dupes = {k: v for k, v in digests.items() if len(v) > 1}
        if dupes:
            worst = max(dupes.values(), key=len)
            blockers.append(
                f"imagery repeats: {total} beats use only {len(digests)} distinct "
                f"images, and {' and '.join(worst)} are byte-identical. "
                f"Reuse reads as a frozen video.")

    for b in blockers:
        # First items, and shouted, because an upload step scanning this list
        # should trip over them rather than find them under routine items.
        print(f"  WARNING: {b}")
    checklist = [f"BLOCKER: {b}" for b in blockers] + checklist

    payload = {
        "title": brief.title,
        "description": brief.description + (
            f"\n\n{result.music_credit}" if getattr(result, 'music_credit', '') else ""),
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
