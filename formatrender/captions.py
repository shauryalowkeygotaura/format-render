"""Burned captions, shared by the 16:9 master and the 9:16 shorts.

This lived inside shorts.py, which is why the long-form master shipped with no
captions at all: the only code that could build them was in the vertical
slicer. It is a module now so both renderers use one implementation.

ASS rather than SRT + `force_style`, on purpose. SRT carries no canvas, so
libass falls back to a 384x288 script resolution and scales from there:
FontSize and MarginV stop meaning pixels, and a MarginV of 260 intended as
"260px up from the bottom of a 1920-tall frame" lands the text near the TOP.
Writing ASS lets PlayResX/PlayResY be stated outright, so every number here is
real pixels on the real frame.
"""
from __future__ import annotations

from pathlib import Path

# Chunking bounds. A caption is cut at the first natural boundary that lands
# inside them, not at a fixed word count.
MIN_WORDS = 2
MAX_WORDS = 7
MAX_SECONDS = 3.2
# A gap this long between two words is a breath or a beat change. Splitting
# there matches the caption to the delivery instead of fighting it.
PAUSE_GAP = 0.32

_SENTENCE_END = (".", "!", "?", "…")
_CLAUSE_END = (",", ";", ":")

# A caption must never END on one of these. They point forward at the word that
# has not appeared yet, so the viewer reads a sentence that stops mid-thought:
# real output was "Asteroids can also be used as a", which hit the MAX_WORDS
# ceiling on an article. Dropping the trailing function word into the next
# chunk costs nothing and removes the whole class of dangling caption.
_DANGLING = {
    "a", "an", "the", "and", "or", "but", "as", "at", "by", "for", "from",
    "in", "into", "of", "on", "onto", "to", "with", "within", "is", "are",
    "was", "were", "be", "been", "that", "this", "these", "those", "its",
    "their", "his", "her", "our", "your", "my", "it", "than", "then",
}


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}.{int(round((t - int(t)) * 100)):02d}"


def chunk_words(words: list[tuple[str, float, float]]) -> list[list[tuple[str, float, float]]]:
    """Group timed words into caption-sized phrases.

    The old rule was a flat every-N-words cut, which is why real output read
    "pass close to" / "can also be" / "solar system as" -- fragments sliced
    mid-phrase that carry no meaning on screen. Here a chunk closes at the
    first boundary that makes linguistic sense:

      1. sentence-ending punctuation  -- always closes
      2. clause punctuation           -- closes once MIN_WORDS is met
      3. a PAUSE_GAP of silence       -- closes once MIN_WORDS is met
      4. MAX_WORDS or MAX_SECONDS     -- the hard ceiling

    A ceiling cut additionally refuses to end on a forward-pointing function
    word (see _DANGLING), and a trailing one-word orphan is merged backwards,
    because a lone "the" on screen for 300ms looks like a rendering bug.
    """
    chunks: list[list[tuple[str, float, float]]] = []
    cur: list[tuple[str, float, float]] = []

    for i, (word, start, end) in enumerate(words):
        cur.append((word, start, end))
        stripped = word.rstrip("\"')]")
        span = cur[-1][2] - cur[0][1]

        gap_next = words[i + 1][1] - end if i + 1 < len(words) else 0.0

        ends_punct = stripped.endswith(_SENTENCE_END + _CLAUSE_END)
        close = (
            stripped.endswith(_SENTENCE_END)
            or (len(cur) >= MIN_WORDS and stripped.endswith(_CLAUSE_END))
            or (len(cur) >= MIN_WORDS and gap_next >= PAUSE_GAP)
            or len(cur) >= MAX_WORDS
            or span >= MAX_SECONDS
        )
        if not close:
            continue

        # Peel trailing function words off a ceiling cut so they open the NEXT
        # caption instead of stranding at the end of this one. Punctuation cuts
        # are exempt: they are already complete thoughts, and "the." is not a
        # thing. Guarded by MIN_WORDS so peeling cannot empty the chunk.
        if not ends_punct:
            while (len(cur) > MIN_WORDS
                   and cur[-1][0].strip(",.;:!?\"')]").lower() in _DANGLING):
                cur.pop()

        chunks.append(cur)
        # Anything peeled stays unconsumed and is picked up by the next
        # iteration, because `cur` is rebuilt from the words that follow.
        consumed = sum(len(c) for c in chunks)
        cur = list(words[consumed:i + 1])

    if cur:
        chunks.append(cur)

    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-2].extend(chunks.pop())

    return chunks


def _norm_word(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


# Kinetic emphasis pop: big centred word, scale bounce, quick fade. Measured on
# the reference anime channel (1.7 s/shot): key words land as large on-screen
# type over the footage ('HOW HARD WOULD IT BE TO', 'MOST'). Ours had none.
POP_SECONDS = 0.85
_POP_ANIM = (r"{\fscx55\fscy55\t(0,110,\fscx118\fscy118)"
             r"\t(110,210,\fscx100\fscy100)\fad(0,160)}")


def build(word_timings, seg_start: float, seg_end: float, out: Path,
          size: tuple[int, int], *, margin_frac: float = 0.14,
          pops: set[str] | None = None) -> Path:
    """Write an ASS caption track for [seg_start, seg_end), timed to the clip.

    `word_timings` arrive on the MASTER timeline; output is clip-relative.
    `margin_frac` lifts the text off the bottom edge -- the 9:16 default clears
    the Shorts UI overlay. A 16:9 master has no such overlay and passes less.
    """
    w, h = size
    font_size = int(h * 0.036)
    margin_v = int(h * margin_frac)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\n"
        f"PlayResY: {h}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(3, font_size // 12)},2,2,70,70,{margin_v},1\n"
        f"Style: Pop,Arial Black,{int(h * 0.11)},&H0000E1FF,&H000000FF,&H00000000,&H90000000,"
        f"-1,0,0,0,100,100,2,0,1,{max(6, int(h * 0.009))},4,5,60,60,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    windowed = [(word, s - seg_start, e - seg_start)
                for word, s, e in word_timings
                if s >= seg_start and e <= seg_end]

    lines = []
    for chunk in chunk_words(windowed):
        text = " ".join(c[0] for c in chunk).replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ts(chunk[0][1])},{_ts(chunk[-1][2])},"
                     f"Cap,,0,0,0,,{text}")

    if pops:
        wanted = {_norm_word(w) for w in pops if _norm_word(w)}
        for word, st, _en in windowed:
            if _norm_word(word) in wanted:
                shown = word.strip(".,!?;:'\"").upper().replace("{", "(").replace("}", ")")
                lines.append(f"Dialogue: 1,{_ts(st)},{_ts(st + POP_SECONDS)},"
                             f"Pop,,0,0,0,,{_POP_ANIM}{shown}")

    out.parent.mkdir(parents=True, exist_ok=True)
    # No Dialogue lines means an empty track; callers check size before burning
    # because the subtitles filter crashes on an empty file.
    out.write_text((header + "\n".join(lines) + "\n") if lines else "",
                   encoding="utf-8")
    return out


def escape_filter_path(p: Path) -> str:
    """Escape a path for an ffmpeg filter argument.

    The Windows drive colon (C:) is a filter-syntax separator; unescaped it
    makes ffmpeg reject the entire filtergraph with EINVAL.
    """
    return p.as_posix().replace(":", "\\:")
