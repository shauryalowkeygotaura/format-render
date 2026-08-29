"""Build a thumbnail TO a measured format spec, then verify it against that spec.

This is the reason format-render exists as its own project rather than a flag on
an existing pipeline. Every other renderer in this vault composites a thumbnail
that looks good. This one composites a thumbnail that *matches numbers measured
off channels the algorithm is currently rewarding*, then re-measures its own
output and corrects until it agrees.

The loop:

    format-engine deconstruct  ->  measured checklist (layout, palette,
                                   header band, saturation, contrast, brightness)
    format-render thumbnail    ->  composite built to those targets
    format-engine analyze      ->  re-measure the composite
    correction passes          ->  close the gap on the continuous traits

format-engine is soft-imported. When it is missing the renderer still produces a
perfectly good thumbnail from defaults; it just cannot verify it, and says so.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from . import config

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

_FILLER = {"the", "a", "an", "of", "in", "on", "to", "and", "is", "are", "for",
           "every", "explained", "minutes", "minute"}


def _analyzer():
    """Soft-import format-engine's thumbnail analyzer. None when unavailable."""
    home = config.FORMAT_ENGINE_HOME
    if not home.exists():
        return None
    if str(home) not in sys.path:
        sys.path.insert(0, str(home))
    try:
        from formatengine.packaging import analyze_thumbnail
        return analyze_thumbnail
    except ImportError:
        return None


def _font(size: int):
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def choose_overlay(title: str, overlay_text: str = "", max_words: int = 4) -> str:
    """What actually goes on the thumbnail.

    Prefers purpose-written text from the brief. Deriving the overlay FROM the
    title, which is what _punchy does, structurally guarantees the thumbnail
    repeats the title, and repeating the title is the most common thumbnail
    mistake there is: the viewer has already read it, so it adds nothing and
    closes the curiosity gap it should be opening.

    It is also how "Every Asteroid That Could Actually Hit Earth Explained"
    became the overlay "ASTEROID THAT COULD ACTUALLY", a truncated fragment
    whose payload words were precisely the ones cut off.
    """
    t = (overlay_text or "").strip()
    if t:
        return " ".join(t.split()[:max_words]).upper()
    return _punchy(title, max_words)


def _punchy(title: str, max_words: int = 4) -> str:
    """Strip a title down to what is legible at thumbnail scale.

    Runtime clauses are dropped whole rather than word by word. Filtering
    "explained in 12 minutes" a token at a time removes the words and strands
    the number, so the overlay reads "SCARIEST ASTEROID 12".
    """
    head = re.split(r"\s[|\-–—:]\s", title)[0]
    # "... explained in 12 minutes", "... in 100 seconds", "... (12 min)"
    head = re.sub(r"\s*\(?\b(explained\s+)?in\s+\d+\s*(minutes?|mins?|seconds?|secs?)\b\)?",
                  "", head, flags=re.I)
    head = re.sub(r"\s*\b\d+\s*(minutes?|mins?|seconds?|secs?)\b", "", head, flags=re.I)
    tokens = re.findall(r"[A-Za-z0-9']+", head)
    words = [w for w in tokens if w.lower() not in _FILLER]
    # A bare number that survived is a leftover, not a hook. A LEADING number
    # is the listicle count and earns its place ("25 MOST INCREDIBLE BIRDS").
    words = [w for i, w in enumerate(words) if i == 0 or not w.isdigit()]
    if not words:
        words = tokens[:max_words]
    return " ".join(words[:max_words]).upper()


def _hexes(spec: dict, n: int = 2) -> list[str]:
    pal = (spec.get("thumbnail_checklist", {}) or {}).get("signature_palette", [])
    out = [c["hex"] for c in pal if c.get("hex")][:n]
    while len(out) < n:
        out.append("#101820" if not out else out[-1])
    return out


def _rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _base_canvas(size, colors: list[str]) -> Image.Image:
    """Vertical gradient in the format's own signature palette."""
    w, h = size
    top, bottom = _rgb(colors[0]), _rgb(colors[-1])
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h))


def _place(canvas: Image.Image, still: Path | None, layout: str,
           subject_count: int) -> Image.Image:
    """Lay imagery onto the canvas according to the measured layout."""
    w, h = canvas.size
    if not still or not Path(still).exists():
        return canvas
    try:
        src = Image.open(still).convert("RGB")
    except OSError:
        return canvas

    def cover(im, tw, th):
        ratio = max(tw / im.width, th / im.height)
        im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))))
        left = (im.width - tw) // 2
        top = (im.height - th) // 2
        return im.crop((left, top, left + tw, top + th))

    if layout in ("three_subject", "multi_subject"):
        n = max(2, min(subject_count or 3, 4))
        pad = 8
        panel_w = (w - pad * (n - 1)) // n
        for i in range(n):
            panel = cover(src, panel_w, h)
            # Flanking panels sit back so the centre reads as the odd one out,
            # which is the composition the measured "middle_unique" rule wants.
            if n == 3 and i != 1:
                panel = panel.filter(ImageFilter.GaussianBlur(1.2))
                panel = ImageEnhance.Brightness(panel).enhance(0.72)
            canvas.paste(panel, (i * (panel_w + pad), 0))
        return canvas

    if layout == "two_subject_versus":
        half = w // 2
        canvas.paste(cover(src, half, h), (0, 0))
        right = ImageEnhance.Brightness(cover(src, w - half, h)).enhance(0.78)
        canvas.paste(right, (half, 0))
        return canvas

    if layout == "single_center_subject":
        cw, ch = int(w * 0.56), int(h * 0.78)
        canvas.paste(cover(src, cw, ch), ((w - cw) // 2, (h - ch) // 2))
        return canvas

    # scene_no_clear_subject: full bleed, the environment IS the subject
    canvas.paste(cover(src, w, h), (0, 0))
    return canvas


def _header_band(img: Image.Image, text: str) -> Image.Image:
    """Text band across the top, which is a measured rule on many formats.

    The scrim fades out rather than ending on a hard line. A flat rectangle
    puts a full-width horizontal edge in the frame, and the subject detector
    reads that edge as a region boundary: a scene thumbnail then measures as
    `two_subject_versus` and fails its own format check.
    """
    w, h = img.size
    band_h = int(h * 0.22)
    fade = int(band_h * 0.45)
    mask = Image.new("L", (1, band_h))
    px = mask.load()
    for y in range(band_h):
        if y < band_h - fade:
            px[0, y] = 175
        else:
            t = (y - (band_h - fade)) / max(1, fade)
            px[0, y] = int(175 * (1.0 - t))
    mask = mask.resize((w, band_h))
    scrim = Image.new("RGB", (w, band_h), (0, 0, 0))
    region = img.crop((0, 0, w, band_h))
    img.paste(Image.composite(scrim, region, mask), (0, 0))
    draw = ImageDraw.Draw(img)
    size = int(band_h * 0.55)
    while size > 12:
        font = _font(size)
        if draw.textlength(text, font=font) <= w * 0.92:
            break
        size -= 4
    font = _font(size)
    tw = draw.textlength(text, font=font)
    draw.text(((w - tw) / 2, (band_h - size) / 2 - size * 0.08), text,
              font=font, fill=(255, 255, 255),
              stroke_width=max(2, size // 18), stroke_fill=(0, 0, 0))
    return img


def _big_text(img: Image.Image, text: str) -> Image.Image:
    """Bottom-left punch words for formats without a header band."""
    w, h = img.size
    draw = ImageDraw.Draw(img)
    size = int(h * 0.20)
    while size > 20:
        font = _font(size)
        if draw.textlength(text, font=font) <= w * 0.86:
            break
        size -= 6
    font = _font(size)
    x, y = int(w * 0.05), int(h * 0.68)
    draw.text((x, y), text, font=font, fill=(255, 255, 255),
              stroke_width=max(3, size // 14), stroke_fill=(0, 0, 0))
    return img


def _targets(spec: dict) -> dict:
    cl = spec.get("thumbnail_checklist", {}) or {}
    return {
        "layout": cl.get("dominant_layout", "scene_no_clear_subject"),
        "subject_count": cl.get("subject_count_mode", 0),
        "header": cl.get("header_text_rate", 0) >= 0.6,
        "saturation": cl.get("avg_saturation"),
        "contrast": cl.get("avg_contrast"),
        "brightness": cl.get("avg_brightness"),
    }


def _correct(path: Path, targets: dict, analyze) -> dict:
    """Re-measure our own output and close the gap on continuous traits.

    Layout and subject count are structural: they are either built right or
    they are not. Saturation, contrast and brightness are continuous, so they
    can be driven toward the format's measured values by iteration.

    Some gaps cannot be closed by processing at all. `ImageEnhance.Color`
    multiplies the chroma that is already there, so a near-monochrome
    photograph stays near-monochrome no matter how many passes you run. When a
    trait stops moving, the loop says so and names the real fix rather than
    burning passes and reporting a mismatch with no explanation.
    """
    report = {"passes": 0, "history": [], "stalled": []}
    prev: dict[str, float] = {}
    stalled: set[str] = set()

    for _ in range(config.THUMB_CORRECTION_PASSES):
        try:
            got = analyze(path)
        except (ValueError, OSError):
            return report
        report["history"].append({k: got.get(k) for k in
                                  ("saturation", "contrast", "brightness", "layout")})

        img = Image.open(path).convert("RGB")
        changed = False
        for trait, enhancer in (("saturation", ImageEnhance.Color),
                                ("contrast", ImageEnhance.Contrast),
                                ("brightness", ImageEnhance.Brightness)):
            want = targets.get(trait)
            have = got.get(trait)
            if not want or not have or have <= 0 or trait in stalled:
                continue
            if abs(have - want) <= config.THUMB_MATCH_TOLERANCE:
                continue
            # A trait that barely moved after a full pass will not move on the
            # next one either; the headroom simply is not in the pixels.
            if trait in prev and abs(have - prev[trait]) < 0.01:
                stalled.add(trait)
                continue
            prev[trait] = have
            # Clamp the per-pass factor: a single aggressive correction
            # overshoots and the loop oscillates instead of converging.
            factor = max(0.65, min(1.5, want / have))
            img = enhancer(img).enhance(factor)
            changed = True
        if not changed:
            break
        img.save(path, quality=92)
        report["passes"] += 1

    advice = {
        "saturation": "source still is near-monochrome; this format's thumbnails "
                      "are colourful. Pick a beat with a colour-rich Commons image, "
                      "or pass --still explicitly.",
        "contrast": "source still is flat; choose an image with a clear "
                    "light/dark subject separation.",
        "brightness": "source still cannot reach the format's exposure; choose a "
                      "brighter or darker base image.",
    }
    report["stalled"] = [{"trait": t, "fix": advice.get(t, "")} for t in sorted(stalled)]
    return report


def build(title: str, still: Path | None, spec: dict, out: Path,
          overlay_text: str = "") -> dict:
    """Composite a thumbnail to the format's measured spec and verify it."""
    targets = _targets(spec)
    colors = _hexes(spec)
    canvas = _base_canvas(config.THUMB_SIZE, colors)
    canvas = _place(canvas, still, targets["layout"], targets["subject_count"])

    punch = choose_overlay(title, overlay_text)
    canvas = _header_band(canvas, punch) if targets["header"] else _big_text(canvas, punch)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=92)

    analyze = _analyzer()
    result = {"path": str(out), "targets": targets, "verified": False}
    if analyze is None:
        result["note"] = ("format-engine not importable from "
                          f"{config.FORMAT_ENGINE_HOME}; thumbnail built from "
                          "defaults and not verified")
        return result

    result["correction"] = _correct(out, targets, analyze)
    try:
        final = analyze(out)
    except (ValueError, OSError) as e:
        result["note"] = f"verification failed: {e}"
        return result

    checks = []
    if targets["layout"]:
        checks.append(("layout", final.get("layout"), targets["layout"],
                       final.get("layout") == targets["layout"]))
    for trait in ("saturation", "contrast", "brightness"):
        want = targets.get(trait)
        if want:
            have = final.get(trait, 0)
            checks.append((trait, round(have, 3), round(want, 3),
                           abs(have - want) <= config.THUMB_MATCH_TOLERANCE))
    result["verified"] = True
    result["checks"] = checks
    result["matches"] = sum(1 for c in checks if c[3])
    result["total"] = len(checks)

    # Layout is structural. No amount of post-processing turns a photograph of
    # one object into a scene composition, so when it misses, say what would
    # actually fix it rather than reporting a bare NO.
    got_layout = final.get("layout")
    if got_layout and got_layout != targets["layout"]:
        result["layout_advice"] = _layout_advice(got_layout, targets["layout"])
    return result


_LAYOUT_HINT = {
    "scene_no_clear_subject":
        "this format uses wide environmental frames with no single hero object. "
        "Your still is a subject on a plain background. Choose a beat whose cue "
        "names a landscape, a field of objects, or a wide view.",
    "three_subject":
        "this format lines up three subjects with a distinct one in the middle. "
        "Supply three stills, or pick a cue that returns a group shot.",
    "two_subject_versus":
        "this format is a two-way split. Pick a cue naming two things in "
        "opposition.",
    "single_center_subject":
        "this format puts one object dead centre filling the frame. Choose a cue "
        "naming a single concrete thing.",
}


def _layout_advice(got: str, want: str) -> str:
    return (f"built {got}, format wants {want}: "
            + _LAYOUT_HINT.get(want, "pick a still whose composition matches."))


def build_best(title: str, stills: list[Path], spec: dict, out: Path,
               overlay_text: str = "") -> dict:
    """Build from every candidate still and keep the one that matches best.

    Which still you start from changes the score more than any amount of
    post-processing does: one beat's image lands three of four measured traits
    while another lands two, on the same format and the same correction loop.
    Choosing by measured match beats choosing by "looks nice", and the
    verification pass is already built, so this is just running it in a loop.
    """
    import shutil
    import tempfile

    stills = [Path(p) for p in stills if p and Path(p).exists()]
    if not stills:
        return build(title, None, spec, out, overlay_text)
    # Without the analyzer there is no score to choose on, so trying every
    # still would be N full composites to pick arbitrarily.
    if len(stills) == 1 or _analyzer() is None:
        return build(title, stills[0], spec, out, overlay_text)

    scratch = Path(tempfile.mkdtemp(prefix="fr_cand_", dir=str(out.parent)))
    best_path, best_still, best_score = None, None, -1.0
    tried = []
    try:
        for i, s in enumerate(stills):
            # Index-prefixed: two stills with the same stem from different
            # directories would otherwise share a scratch file, and since the
            # correction loop rewrites that file in place the second candidate
            # would be scored against the first one's enhanced pixels.
            cand = scratch / f"{i:02d}_{s.stem}.jpg"
            try:
                res = build(title, s, spec, cand, overlay_text)
            except (OSError, ValueError) as e:
                tried.append({"still": s.name, "matches": f"skipped ({e})"})
                continue
            total = res.get("total") or 0
            score = (res.get("matches", 0) / total) if total else -1.0
            tried.append({"still": s.name,
                          "matches": f"{res.get('matches', 0)}/{total}"})
            if score > best_score:
                best_path, best_still, best_score = cand, s, score

        if best_path is None:
            return build(title, stills[0], spec, out, overlay_text)

        # Reuse the winning composite instead of rebuilding it; the correction
        # loop is the expensive part and it has already run on this file.
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(best_path, out)
        final = _reverify(out, spec)
        final["chosen_still"] = best_still.name
        final["candidates"] = tried
        return final
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _reverify(path: Path, spec: dict) -> dict:
    """Re-measure an already-built thumbnail without re-running corrections."""
    targets = _targets(spec)
    result = {"path": str(path), "targets": targets, "verified": False,
              "correction": {"passes": 0, "history": [], "stalled": []}}
    analyze = _analyzer()
    if analyze is None:
        return result
    try:
        final = analyze(path)
    except (ValueError, OSError) as e:
        result["note"] = f"verification failed: {e}"
        return result
    checks = []
    if targets["layout"]:
        checks.append(("layout", final.get("layout"), targets["layout"],
                       final.get("layout") == targets["layout"]))
    for trait in ("saturation", "contrast", "brightness"):
        want = targets.get(trait)
        if want:
            have = final.get(trait, 0)
            checks.append((trait, round(have, 3), round(want, 3),
                           abs(have - want) <= config.THUMB_MATCH_TOLERANCE))
    result.update(verified=True, checks=checks,
                  matches=sum(1 for c in checks if c[3]), total=len(checks))
    got_layout = final.get("layout")
    if got_layout and got_layout != targets["layout"]:
        result["layout_advice"] = _layout_advice(got_layout, targets["layout"])
    return result
