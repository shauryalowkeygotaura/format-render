"""Load and validate a format-engine brief, plus its format spec.

The brief is the contract between the two projects. Everything the renderer
needs arrives in it; nothing here reaches back into format-engine's internals
except the format spec JSON, which is a documented on-disk artifact.

A brief that failed format-engine's differentiation gate is refused by default.
Rendering a blocked brief is exactly the mistake the gate exists to prevent, so
it takes an explicit --force to do it anyway.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import config


class BriefError(RuntimeError):
    pass


@dataclass
class Beat:
    index: int
    narration: str
    visual: str
    seconds: float


@dataclass
class Brief:
    title: str
    hook: str
    beats: list[Beat]
    topic: str
    format_slug: str
    description: str = ""
    # Purpose-written thumbnail overlay. Defaulted, so it must sit with the
    # other defaulted fields: a default before a non-default field is a
    # dataclass TypeError at import, which compile() cannot catch.
    thumbnail_text: str = ""
    hashtags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    substance: list[dict] = field(default_factory=list)
    end_screen_followup: str = ""
    gate_passed: bool = True
    gate_failures: list[str] = field(default_factory=list)
    source: Path | None = None

    @property
    def narration_text(self) -> str:
        return " ".join(b.narration for b in self.beats)


def _resolve(ref: str) -> Path:
    """Accept a path, a bare filename, or a brief slug."""
    p = Path(ref)
    if p.exists():
        return p
    for cand in (config.BRIEF_DIR / ref,
                 config.BRIEF_DIR / f"{ref}.json"):
        if cand.exists():
            return cand
    matches = sorted(config.BRIEF_DIR.glob(f"*{ref}*.json"))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = "\n  ".join(m.name for m in matches)
        raise BriefError(f"{ref!r} matches several briefs:\n  {names}")
    raise BriefError(
        f"No brief matching {ref!r}. Looked in {config.BRIEF_DIR}.\n"
        f"Generate one with: python -m formatengine brief <format> --bend 0")


def load_brief(ref: str, *, force: bool = False) -> Brief:
    path = _resolve(ref)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BriefError(f"{path.name} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise BriefError(f"{path.name} is not a brief object")

    gate = raw.get("gate") or {}
    passed = bool(gate.get("passed", True))
    failures = list(gate.get("failures", []))
    if not passed and not force:
        detail = "\n  ".join(failures) or "(no detail recorded)"
        raise BriefError(
            f"{path.name} FAILED format-engine's differentiation gate:\n  {detail}\n\n"
            f"That gate is what keeps a channel out of the inauthentic-content\n"
            f"policy. Fix the brief and regenerate, or pass --force if you have\n"
            f"decided the risk is acceptable.")

    hook = (raw.get("hook") or "").strip()
    if not hook and not force:
        raise BriefError(
            f"{path.name} has no `hook`.\n\n"
            f"compose() leads the video with the hook, so an empty one ships a\n"
            f"video that opens cold on its first expository beat. That is exactly\n"
            f"what produced the asteroid render Shaurya rejected as 'no hooks were\n"
            f"used very boring info overall'. It was silently accepted because\n"
            f"this field used to default to \"\".\n\n"
            f"Fix the brief in format-engine and regenerate, or pass --force if\n"
            f"you have decided a hookless video is acceptable.")

    beats_raw = raw.get("beats") or []
    if not beats_raw:
        raise BriefError(f"{path.name} has no beats to render")
    beats = []
    for i, b in enumerate(beats_raw):
        if not isinstance(b, dict):
            continue
        narration = (b.get("narration") or "").strip()
        if not narration:
            continue
        beats.append(Beat(
            index=len(beats),
            narration=narration,
            visual=(b.get("visual") or narration)[:200],
            seconds=float(b.get("seconds") or 0) or 0.0,
        ))
    if not beats:
        raise BriefError(f"{path.name} has beats but none carry narration")

    return Brief(
        title=raw.get("title") or path.stem,
        hook=hook,
        thumbnail_text=(raw.get("thumbnail_text") or "").strip(),
        beats=beats,
        topic=raw.get("topic") or "",
        format_slug=raw.get("format_slug") or "",
        description=raw.get("description") or "",
        hashtags=list(raw.get("hashtags") or []),
        tags=list(raw.get("tags") or []),
        substance=list(raw.get("substance") or []),
        end_screen_followup=raw.get("end_screen_followup") or "",
        gate_passed=passed,
        gate_failures=failures,
        source=path,
    )


def load_format_spec(slug: str) -> dict:
    """The measured format spec. Empty dict when unavailable, never an error.

    The renderer must still work when format-engine is not on this machine; it
    just falls back to sane defaults instead of building to measured numbers.
    """
    if not slug:
        return {}
    path = config.FORMAT_SPECS / f"{slug}.json"
    if not path.exists():
        return {}
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        return spec if isinstance(spec, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}
