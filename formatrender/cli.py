"""format-render CLI.

    python -m formatrender render <brief>   turn a brief into a finished video
    python -m formatrender briefs           list renderable briefs
    python -m formatrender thumb <brief>    thumbnail only (fast iteration)
    python -m formatrender check            environment preflight

No API keys. edge-tts, Wikimedia Commons and ffmpeg are all free and keyless,
so unlike format-engine this project needs no Doppler wrapper.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import broll, compose, config, publish, shorts, thumbnail, voice
from .brief import BriefError, load_brief, load_format_spec
from .compose import ComposeError
from .publish import PublishError
from .tts import TTSError


def _slug(text: str, maxlen: int = 60) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "render")[:maxlen]


def cmd_check(args) -> int:
    ok = True
    print("Binaries")
    for exe in ("ffmpeg", "ffprobe"):
        path = shutil.which(exe)
        print(f"  {exe:<10} {path or 'MISSING'}")
        ok &= bool(path)

    print("\nPython packages")
    for mod in ("edge_tts", "PIL", "requests"):
        try:
            __import__(mod)
            print(f"  {mod:<10} ok")
        except ImportError:
            print(f"  {mod:<10} MISSING (pip install -r requirements.txt)")
            ok = False

    print(f"\nformat-engine  {config.FORMAT_ENGINE_HOME}")
    if config.FORMAT_ENGINE_HOME.exists():
        n_specs = len(list(config.FORMAT_SPECS.glob("*.json"))) if config.FORMAT_SPECS.exists() else 0
        n_briefs = len(list(config.BRIEF_DIR.glob("*.json"))) if config.BRIEF_DIR.exists() else 0
        print(f"  found: {n_specs} format specs, {n_briefs} briefs")
        print(f"  thumbnail verification: "
              f"{'available' if thumbnail._analyzer() else 'UNAVAILABLE (import failed)'}")
    else:
        print("  NOT FOUND. Set FORMAT_ENGINE_HOME. Rendering still works;")
        print("  thumbnails will use defaults and cannot be verified.")

    print("\nTTS providers")
    for name, state in voice.available().items():
        mark = "  <- active" if name == voice.PROVIDER else ""
        print(f"  {name:<10} {state}{mark}")

    print("\nStock b-roll (optional; Commons stills are the default)")
    for name, on in broll.enabled().items():
        print(f"  {name:<10} {'keyed' if on else 'no key (both free, see broll.py)'}")

    print("\nUpload")
    up = publish.FOOTBALL_HOME / "modules" / "uploader.py"
    print(f"  YT OAuth   {'present' if publish.creds_present() else 'ABSENT -> publish is always a dry run'}")
    print(f"  uploader   {up}{'' if up.exists() else '   (NOT FOUND)'}")

    print(f"\nvoice          {config.VOICE} @ {config.VOICE_RATE}")
    print(f"master         {config.RESOLUTION[0]}x{config.RESOLUTION[1]} @ {config.FPS}fps")
    print(f"shorts         {config.SHORT_RESOLUTION[0]}x{config.SHORT_RESOLUTION[1]}")
    print(f"\n{'READY' if ok else 'NOT READY'}")
    return 0 if ok else 1


def cmd_briefs(args) -> int:
    if not config.BRIEF_DIR.exists():
        print(f"No brief directory at {config.BRIEF_DIR}")
        return 2
    rows = []
    for p in sorted(config.BRIEF_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        gate = (d.get("gate") or {}).get("passed", True)
        rows.append((p.stem, "PASS" if gate else "BLOCKED",
                     len(d.get("beats") or []), d.get("format_slug", "")))
    if not rows:
        print("No briefs found. Generate one with format-engine.")
        return 0
    print(f"{'GATE':<8} {'BEATS':>5}  {'FORMAT':<22} BRIEF")
    print("-" * 96)
    for stem, gate, beats, fmt in rows:
        print(f"{gate:<8} {beats:>5}  {fmt:<22} {stem}")
    print(f"\nRender with: python -m formatrender render <brief>")
    return 0


def _load(args):
    b = load_brief(args.brief, force=getattr(args, "force", False))
    spec = load_format_spec(b.format_slug)
    if not spec and b.format_slug:
        print(f"  ! format spec {b.format_slug!r} not found; using defaults")
    return b, spec


def _render_stills(out_dir: Path) -> list[Path]:
    """Stills a previous render left behind, largest first."""
    img = out_dir / "work" / "img"
    if not img.exists():
        return []
    return sorted(img.glob("*.jpg"), key=lambda p: -p.stat().st_size)


def cmd_thumb(args) -> int:
    b, spec = _load(args)
    out_dir = config.OUTPUT / _slug(b.title)
    out = out_dir / "thumbnail.jpg"

    if args.still:
        res = thumbnail.build(b.title, Path(args.still), spec, out, b.thumbnail_text)
    else:
        stills = _render_stills(out_dir)
        if args.best_still and len(stills) > 1:
            print(f"  trying {len(stills)} stills against the format spec")
            res = thumbnail.build_best(b.title, stills, spec, out, b.thumbnail_text)
        else:
            res = thumbnail.build(b.title, stills[0] if stills else None, spec, out, b.thumbnail_text)
    _print_thumb(res)
    return 0


def _print_thumb(res: dict) -> None:
    print(f"\nThumbnail -> {res['path']}")
    if res.get("note"):
        print(f"  {res['note']}")
    if not res.get("verified"):
        return
    correction = res.get("correction") or {}
    print(f"  correction passes: {correction.get('passes', 0)}")
    print(f"  {'TRAIT':<12} {'BUILT':<24} {'FORMAT':<24} OK")
    for name, got, want, ok in res.get("checks", []):
        print(f"  {name:<12} {str(got):<24} {str(want):<24} {'yes' if ok else 'NO'}")
    print(f"  matches {res.get('matches',0)}/{res.get('total',0)}")
    if res.get("chosen_still"):
        print(f"  chosen still: {res['chosen_still']} "
              f"(of {len(res.get('candidates', []))} tried)")
    for s in correction.get("stalled", []):
        print(f"  ! {s['trait']} cannot be corrected further: {s['fix']}")
    if res.get("layout_advice"):
        print(f"  ! {res['layout_advice']}")


def _frames_from_video(video: Path, beats, out_dir: Path) -> list[Path]:
    """Thumbnail candidates lifted from the finished video.

    Candidates used to come only from Commons stills (`r.image`). With --broll
    the beats that found footage have no still at all, so the list was whatever
    grey leftovers remained, and the build warned that its source was
    near-monochrome and was a subject on a plain background where this format
    wants a wide environmental frame.

    Frames from the stock footage are exactly that frame: colourful, wide, and
    already the imagery the viewer is about to watch, so the thumbnail promises
    what the video delivers instead of advertising a different picture.
    """
    out: list[Path] = []
    if not video or not Path(video).exists():
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(beats):
        try:
            mid = (float(r.start) + float(r.end)) / 2.0
        except (TypeError, ValueError):
            continue
        if not (mid > 0):
            continue
        dest = out_dir / f"frame_{i:02d}.jpg"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{mid:.2f}",
             "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)],
            capture_output=True, text=True)
        # A frame that did not decode is skipped rather than handed on as a
        # zero-byte candidate the scorer would rank on garbage.
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1024:
            out.append(dest)
    return out


def cmd_render(args) -> int:
    b, spec = _load(args)
    out_dir = config.OUTPUT / _slug(b.title)
    work = out_dir / "work"

    print(f"\n{b.title}")
    print(f"  format   {b.format_slug or '(none)'}")
    print(f"  topic    {b.topic}")
    print(f"  gate     {'PASS' if b.gate_passed else 'BLOCKED (forced)'}")
    print(f"  segments {len(b.beats) + (1 if b.hook else 0)}\n")

    result = compose.compose(b, spec, work, resume=not args.fresh,
                             use_broll=args.broll)

    print(f"\nVideo -> {result.video_path}")
    print(f"  duration          {result.duration:.1f}s ({result.duration/60:.1f} min)")
    print(f"  real imagery      {result.real_image_ratio:.0%} "
          f"(rest are gradient cards)")
    if result.real_image_ratio < 0.5:
        print("  ! Over half the visuals fell back to gradients. The visual cues")
        print("    in this brief are too abstract for Commons. Rewrite them as")
        print("    concrete named subjects and re-render.")

    thumb_out = out_dir / "thumbnail.jpg"
    stills = [Path(r.image) for r in result.beats if r.image_found]
    if getattr(args, "broll", False):
        # Footage frames first: with --broll they are what the video actually
        # shows, and the Commons stills are leftovers from beats that found none.
        stills = _frames_from_video(result.video_path, result.beats,
                                    out_dir / "thumbframes") + stills
    # hero_still is a Commons still. With --broll most beats have no still
    # at all, so falling back to it ignored every footage frame gathered
    # above and rebuilt from the same grey leftover: two renders with wholly
    # different footage scored an identical brightness 0.059, which is the
    # tell that nothing new was being read. Using the frames instead moved
    # brightness 0.059 -> 0.391 and contrast 0.194 -> 0.301, both onto
    # target, with zero correction passes instead of three.
    if len(stills) > 1 and (args.best_still or getattr(args, "broll", False)):
        print(f"\n  trying {len(stills)} stills against the format spec")
        thumb = thumbnail.build_best(b.title, stills, spec, thumb_out, b.thumbnail_text)
    elif stills:
        thumb = thumbnail.build(b.title, stills[0], spec, thumb_out, b.thumbnail_text)
    else:
        thumb = thumbnail.build(b.title, result.hero_still, spec, thumb_out, b.thumbnail_text)
    _print_thumb(thumb)

    clips: list[dict] = []
    if not args.no_shorts:
        print(f"\nShorts (payoff-ranked from the brief's substance claims)")
        try:
            clips = shorts.slice_shorts(result.video_path, result.beats,
                                        b.substance, work, max_count=args.shorts)
        except RuntimeError as e:
            print(f"  ! shorts failed, master is unaffected: {e}")
        if not clips:
            print("  none cut (no beat carried enough standalone payoff)")

    manifest = compose.write_manifest(b, result, thumb, out_dir / "manifest.json",
                                      shorts=clips)
    print(f"\nManifest -> {manifest}")
    print("  Upload is deliberately out of scope here; the manifest carries")
    print("  everything an uploader needs.")
    return 0


def cmd_publish(args) -> int:
    """Turn a manifest into upload payloads. Dry-run unless creds exist."""
    man_path = Path(args.manifest)
    if man_path.is_dir():
        man_path = man_path / "manifest.json"
    manifest = publish.load_manifest(man_path)
    plan = publish.plan(manifest, include_shorts=not args.no_shorts)

    print(f"\n{manifest.get('title','')}")
    print(f"  assets        {plan['assets']}")
    print(f"  quota cost    {plan['quota_units']:,} of {plan['daily_quota']:,} units/day "
          f"({publish.UPLOAD_COST_UNITS:,} per upload, max {plan['max_per_day']}/day)")
    if not plan["fits_today"]:
        print(f"  ! over one day's quota; stagger these across days")
    print(f"  mode          {'DRY RUN' if plan['dry_run'] else 'LIVE'}")
    print()
    for pl in plan["payloads"]:
        print(f"  [{pl['kind']:<6}] {Path(pl['file']).name}")
        print(f"           {pl['title']}")

    out = publish.write_payloads(manifest, man_path.parent / "upload-payloads.json",
                                 include_shorts=not args.no_shorts)
    print(f"\nPayloads -> {out}")
    if plan["dry_run"]:
        print("  Set YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN in Doppler")
        print("  to go live. This is the same blocker football-shorts-autopilot")
        print("  is parked on; fixing it once fixes both.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="formatrender", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="environment preflight").set_defaults(fn=cmd_check)
    sub.add_parser("briefs", help="list renderable briefs").set_defaults(fn=cmd_briefs)

    r = sub.add_parser("render", help="render a brief to video + thumbnail")
    r.add_argument("brief", help="brief path, filename, or slug fragment")
    r.add_argument("--force", action="store_true",
                   help="render even if the brief failed the differentiation gate")
    r.add_argument("--fresh", action="store_true", help="ignore the resume cache")
    r.add_argument("--shorts", type=int, default=4,
                   help="max vertical shorts to cut from the master")
    r.add_argument("--no-shorts", action="store_true", help="master only")
    r.add_argument("--broll", action="store_true",
                   help="try stock video clips before Ken-Burns stills "
                        "(needs PIXABAY_API_KEY or PEXELS_API_KEY)")
    r.add_argument("--best-still", action="store_true",
                   help="build from every beat still and keep the best-matching "
                        "one (slower; the starting image moves the score more "
                        "than any post-processing does)")
    r.set_defaults(fn=cmd_render)

    t = sub.add_parser("thumb", help="build the thumbnail only")
    t.add_argument("brief")
    t.add_argument("--still", help="image to composite from")
    t.add_argument("--best-still", action="store_true",
                   help="try every still from a previous render, keep the best")
    t.add_argument("--force", action="store_true")
    t.set_defaults(fn=cmd_thumb)

    pb = sub.add_parser("publish", help="manifest -> upload payloads (dry by default)")
    pb.add_argument("manifest", help="manifest.json or its output directory")
    pb.add_argument("--no-shorts", action="store_true", help="master only")
    pb.set_defaults(fn=cmd_publish)
    return p


def main(argv=None) -> int:
    # A render takes minutes and reports per beat. Python block-buffers stdout
    # when it is a pipe rather than a TTY, which hides all of that progress
    # until the process exits. Line buffering keeps it useful under `tee`,
    # CI logs, and background runs.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except BriefError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    except TTSError as e:
        print(f"\nTTS error: {e}", file=sys.stderr)
        return 3
    except PublishError as e:
        print(f"\nPublish error: {e}", file=sys.stderr)
        return 7
    except ComposeError as e:
        print(f"\nCompose error: {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nInterrupted (per-beat progress saved; rerun to resume).", file=sys.stderr)
        return 130
