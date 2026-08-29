"""Regression tests for the quality gates added 2026-08-26/27.

Every test here corresponds to a defect that actually shipped in the asteroid
render. They exist so those defects cannot ship again silently, which is the
whole failure mode: none of them raised, they just produced a bad video.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from formatrender import config
from formatrender.brief import BriefError, load_brief
from formatrender.captions import (
    MAX_WORDS,
    _DANGLING,
    build,
    chunk_words,
)
from formatrender.compose import (
    BeatRender,
    RenderResult,
    title_duration_conflict,
    write_manifest,
)


def timed(sentence: str, gap_after: set[int] | None = None):
    """Turn a sentence into word timings, optionally with pauses after words."""
    gap_after = gap_after or set()
    out, t = [], 0.0
    for i, w in enumerate(sentence.split()):
        out.append((w, t, t + 0.28))
        t += 0.30 + (0.5 if i in gap_after else 0.0)
    return out


# ── caption chunking ────────────────────────────────────────────────────────

def test_no_word_is_lost_or_reordered():
    """The chunker must be a partition. Dropping a word silently corrupts the
    transcript, which is worse than any formatting flaw."""
    words = timed("The asteroid Apophis is expected to pass close to Earth in 2029.")
    chunks = chunk_words(words)
    flat = [w[0] for c in chunks for w in c]
    assert flat == [w[0] for w in words]


def test_caption_never_ends_on_a_dangling_function_word():
    """Real output read "Asteroids can also be used as a", which stops
    mid-thought because the ceiling cut landed on an article."""
    words = timed("Asteroids can also be used as a stepping stone for deeper space exploration.")
    for chunk in chunk_words(words):
        last = chunk[-1][0]
        if last.rstrip(".,;:!?").lower() in _DANGLING:
            assert last[-1] in ".,;:!?", f"chunk ends on a dangling word: {last!r}"


def test_chunk_growth_stays_bounded_on_a_run_on_sentence():
    """Peeling dangling words moves them to the next chunk. That must not let a
    chunk grow without limit, or one caption swallows the screen."""
    words = timed(
        "the asteroid belt between mars and jupiter is home to millions of these "
        "space rocks ranging in size from a few feet to hundreds of miles in diameter"
    )
    sizes = [len(c) for c in chunk_words(words)]
    assert max(sizes) <= MAX_WORDS + 2, f"runaway chunk: {sizes}"


def test_a_pause_closes_a_chunk():
    """edge-tts word timings carry no punctuation, so the pause gap is the only
    boundary signal actually available on real data."""
    words = timed("one two three four five six", gap_after={2})
    chunks = chunk_words(words)
    assert len(chunks) >= 2
    assert [w[0] for w in chunks[0]] == ["one", "two", "three"]


# ── caption rendering ───────────────────────────────────────────────────────

def test_ass_states_the_real_canvas():
    """SRT carries no resolution, so libass assumes 384x288 and every FontSize
    and MarginV stops meaning pixels. Stating PlayRes is the entire fix."""
    out = Path(tempfile.mkdtemp()) / "c.ass"
    build(timed("Apophis passes Earth in 2029."), 0.0, 10.0, out, config.RESOLUTION)
    text = out.read_text(encoding="utf-8")
    w, h = config.RESOLUTION
    assert f"PlayResX: {w}" in text
    assert f"PlayResY: {h}" in text
    assert text.count("Dialogue:") >= 1


def test_empty_window_writes_an_empty_file_not_a_broken_one():
    """The subtitles filter crashes on an empty track, so callers size-check.
    A header-only file would pass that check and then crash ffmpeg."""
    out = Path(tempfile.mkdtemp()) / "c.ass"
    build(timed("nothing here"), 500.0, 600.0, out, config.RESOLUTION)
    assert out.read_text(encoding="utf-8") == ""


# ── title honesty ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,seconds,should_flag", [
    ("Every Scariest Asteroid Explained in 12 Minutes", 174.0, True),   # what shipped
    ("The 1 Hour Deep Dive", 600.0, True),
    ("Explained in 10 Minutes", 480.0, False),                          # normal rounding
    ("Explained in 3 Minutes", 174.0, False),
    ("No duration in this title", 174.0, False),
    ("5 min guide", 300.0, False),
])
def test_title_duration_conflict(title, seconds, should_flag):
    assert (title_duration_conflict(title, seconds) is not None) is should_flag


# ── manifest blockers ───────────────────────────────────────────────────────

def _manifest(tmp, *, title, seconds, images, thumb):
    beats = [BeatRender(i, 0, 1, "n", img, True, []) for i, img in enumerate(images)]
    result = RenderResult(Path("v.mp4"), seconds, beats, None, 1.0)

    class B:  # only the fields write_manifest reads
        pass
    b = B()
    b.title, b.description, b.hashtags, b.tags = title, "", [], []
    b.end_screen_followup, b.format_slug, b.topic, b.source = "", "", "", None
    out = Path(tmp) / "manifest.json"
    write_manifest(b, result, thumb, out)
    return json.loads(out.read_text(encoding="utf-8"))


def test_all_three_blockers_fire_on_the_real_asteroid_conditions():
    m = _manifest(
        tempfile.mkdtemp(),
        title="Every Scariest Asteroid Explained in 12 Minutes",
        seconds=174.0,
        images=["ceres.jpg"] * 3 + ["a.jpg", "b.jpg", "c.jpg"],
        thumb={"layout_advice": "built multi_subject, format wants scene_no_clear_subject"},
    )
    blockers = [c for c in m["upload_checklist"] if c.startswith("BLOCKER:")]
    joined = " ".join(blockers).lower()
    assert len(blockers) == 3, blockers
    assert "retitle" in joined
    assert "thumbnail" in joined
    assert "repeats" in joined
    # Blockers must sort ABOVE the routine checklist, or they get skimmed past.
    assert m["upload_checklist"][0].startswith("BLOCKER:")


def test_a_clean_render_produces_no_blockers():
    """A gate that fires on everything is a gate nobody reads."""
    m = _manifest(
        tempfile.mkdtemp(),
        title="The Asteroid We Spotted 6 Hours Before Impact",
        seconds=900.0,
        images=[f"img{i}.jpg" for i in range(6)],
        thumb={},
    )
    assert [c for c in m["upload_checklist"] if c.startswith("BLOCKER:")] == []
    assert m["title_duration_conflict"] is None


# ── hook gate ───────────────────────────────────────────────────────────────

def _brief_file(tmp, **extra):
    data = {"title": "T", "beats": [{"narration": "n", "visual": "v"}]}
    data.update(extra)
    p = Path(tmp) / "b.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_a_brief_with_no_hook_is_refused():
    """compose() leads with the hook, so an empty one opens the video cold on
    its first expository beat. That is what shipped."""
    p = _brief_file(tempfile.mkdtemp())
    with pytest.raises(BriefError, match="hook"):
        load_brief(str(p))


def test_force_still_renders_a_hookless_brief():
    p = _brief_file(tempfile.mkdtemp())
    assert load_brief(str(p), force=True) is not None


def test_a_brief_with_a_hook_passes():
    p = _brief_file(tempfile.mkdtemp(), hook="In 2008 a rock hit Sudan with six hours warning.")
    assert load_brief(str(p)).hook.startswith("In 2008")


def test_identical_images_under_different_names_are_caught(tmp_path):
    """compose() gives the hook beat[0]'s visual cue, so the two fetch the same
    picture into different files. A filename comparison reported "5 distinct
    stills" and stayed silent while the video opened on the same image twice.
    """
    same = b"\x89PNG-pretend-this-is-a-real-image" * 40
    a, b_ = tmp_path / "hook.jpg", tmp_path / "beat0.jpg"
    a.write_bytes(same)
    b_.write_bytes(same)
    c = tmp_path / "beat1.jpg"
    c.write_bytes(b"a genuinely different image" * 40)

    m = _manifest(tmp_path, title="An Honest Title", seconds=300.0,
                  images=[str(a), str(b_), str(c)], thumb={})
    blockers = [x for x in m["upload_checklist"] if x.startswith("BLOCKER:")]
    assert any("byte-identical" in x for x in blockers), blockers


def test_distinct_images_do_not_trip_the_duplicate_check(tmp_path):
    imgs = []
    for i in range(4):
        f = tmp_path / f"b{i}.jpg"
        f.write_bytes(f"distinct image {i}".encode() * 40)
        imgs.append(str(f))
    m = _manifest(tmp_path, title="An Honest Title", seconds=300.0,
                  images=imgs, thumb={})
    assert not [x for x in m["upload_checklist"] if "byte-identical" in x]
