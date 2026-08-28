"""Alignment tests for the Aura-2 narration path.

No network: the STT response is stubbed. These cover the reason this module
exists, which is that a nicer voice must not cost the burned captions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from formatrender import aura


def audio_file(tmp_path):
    """_word_timings reads the file before calling STT, so it must exist.
    Without this the read raises OSError, the handler correctly returns [], and
    the test passes for entirely the wrong reason."""
    f = tmp_path / "a.mp3"
    f.write_bytes(bytes(64))  # content irrelevant, STT is stubbed
    return f


def stub_stt(monkeypatch, heard_words):
    """Make _post return a Deepgram-shaped response with these words."""
    payload = {"results": {"channels": [{"alternatives": [{"words": [
        {"word": w, "start": s, "end": e} for w, s, e in heard_words
    ]}]}]}}
    monkeypatch.setattr(aura, "_post",
                        lambda *a, **k: json.dumps(payload).encode())


def test_numbers_expand_and_still_align(monkeypatch, tmp_path):
    """The case this rewrite exists for. Deepgram turns "2029." into three
    tokens, so a token-count equality check returned no timings at all."""
    spoken = "Apophis passes Earth in 2029."
    stub_stt(monkeypatch, [
        ("apophis", 0.0, 0.5), ("passes", 0.5, 0.9), ("earth", 0.9, 1.3),
        ("in", 1.3, 1.4), ("twenty", 1.4, 1.7), ("twenty", 1.7, 2.0),
        ("nine", 2.0, 2.4),
    ])
    out = aura._word_timings(audio_file(tmp_path), spoken)
    assert [w for w, _, _ in out] == spoken.split()
    # "2029." must span the whole "twenty twenty nine" run, not just its first
    # token, or the caption vanishes before the number is finished being said.
    last = out[-1]
    assert last[0] == "2029."
    assert last[1] == pytest.approx(1.4, abs=0.01)
    assert last[2] == pytest.approx(2.4, abs=0.01)


def test_words_are_the_submitted_text_not_the_transcript(monkeypatch, tmp_path):
    """A mishearing must never reach the screen."""
    spoken = "Apophis is a rock"
    stub_stt(monkeypatch, [
        ("a", 0.0, 0.2), ("office", 0.2, 0.6), ("is", 0.6, 0.8),
        ("a", 0.8, 0.9), ("rock", 0.9, 1.3),
    ])
    out = aura._word_timings(audio_file(tmp_path), spoken)
    joined = " ".join(w for w, _, _ in out)
    assert "office" not in joined
    if out:
        assert joined == spoken


def test_unrelated_transcript_returns_nothing(monkeypatch, tmp_path):
    """Captions degrade to ABSENT rather than wrong when the transcript is not
    describing this audio at all."""
    stub_stt(monkeypatch, [
        ("completely", 0.0, 0.4), ("different", 0.4, 0.8),
        ("words", 0.8, 1.2), ("entirely", 1.2, 1.6),
    ])
    out = aura._word_timings(audio_file(tmp_path), "Apophis passes Earth in 2029")
    assert out == []


def test_timings_are_monotonic(monkeypatch, tmp_path):
    """Captions are consumed in order; an out-of-order pair would make one
    caption replace its predecessor mid-word."""
    spoken = "one two three four five six seven"
    stub_stt(monkeypatch, [
        ("one", 0.0, 0.2), ("two", 0.2, 0.4), ("three", 0.4, 0.6),
        ("for", 0.6, 0.8), ("five", 0.8, 1.0), ("six", 1.0, 1.2),
        ("seven", 1.2, 1.4),
    ])
    out = aura._word_timings(audio_file(tmp_path), spoken)
    assert out
    starts = [s for _, s, _ in out]
    assert starts == sorted(starts)


def test_stt_failure_degrades_to_no_captions(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(aura, "_post", boom)
    assert aura._word_timings(audio_file(tmp_path), "anything at all") == []


@pytest.mark.parametrize("bad", [
    "aura-2-andromeda-en&model=evil",   # query-parameter injection
    "../../etc/passwd",
    "UPPERCASE-voice",
    "has spaces",
])
def test_voice_ids_are_whitelisted_not_escaped(bad):
    with pytest.raises(aura.AuraError):
        aura.resolve_voice(bad)


def test_the_default_voice_is_the_one_shaurya_picked():
    assert aura.resolve_voice() == "aura-2-andromeda-en"
