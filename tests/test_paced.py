"""The paralang provider must never cost a render: on failure it falls back."""
from pathlib import Path

from formatrender import paced, voice
from formatrender.tts import Narration


def test_paralang_failure_falls_back_to_aura(monkeypatch, tmp_path):
    sentinel = Narration(audio_path=tmp_path / "x.mp3", duration_sec=1.0, word_timings=[])
    monkeypatch.setattr(paced, "narrate", lambda *a, **k: None)
    monkeypatch.setattr(voice.aura, "narrate", lambda *a, **k: sentinel)
    assert voice.narrate("hello", tmp_path, "s", provider="paralang", warn=False) is sentinel


def test_paralang_result_is_used_when_it_works(monkeypatch, tmp_path):
    mine = Narration(audio_path=tmp_path / "p.mp3", duration_sec=2.0, word_timings=[])
    monkeypatch.setattr(paced, "narrate", lambda *a, **k: mine)
    assert voice.narrate("hello", tmp_path, "s", provider="paralang", warn=False) is mine


def test_spoken_text_strips_all_marks():
    assert paced._spoken_text("[serious] Four rocks. [dramatic] None | are *real*.") == \
        "Four rocks. None are real."
