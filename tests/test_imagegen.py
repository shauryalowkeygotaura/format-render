"""Guards for prompt construction. No network in these tests."""
from __future__ import annotations

import pathlib

from formatrender import imagegen


def test_absence_rewriting_respects_word_boundaries():
    """Absence words are rewritten, words that merely contain them are not.

    A plain str.replace turned "darkened room" into
    "unlit, lit only by a single sourceened room" and "emptying" into
    "bare strippeding", sending malformed prompts to the generator.
    """
    assert imagegen._positive("empty shelves") == "bare stripped shelves"
    assert imagegen._positive("a darkened room") == "a darkened room"
    assert imagegen._positive("emptying the warehouse") == "emptying the warehouse"
    assert imagegen._positive("a darkroom") == "a darkroom"


def test_absence_rewriting_is_case_insensitive_and_multiword():
    """"pitch dark".title() is "Pitch Dark", so title-casing missed sentence case."""
    assert "near black" in imagegen._positive("Pitch dark hallway")
    assert "near black" in imagegen._positive("pitch dark hallway")


def test_longest_key_wins():
    """"pitch dark" must be consumed before the shorter "dark" matches inside it."""
    out = imagegen._positive("pitch dark")
    assert "near black with one faint light" == out


def test_prompt_prefers_the_visual_field_over_narration():
    """The brief's `visual` describes what should be SEEN; narration what is SAID."""
    p = imagegen.build_prompt("a substation arcing at night",
                              narration="Level three, the local kill.")
    assert "substation" in p
    assert "Level three" not in p


def test_prompt_falls_back_to_narration_when_visual_is_empty():
    p = imagegen.build_prompt("", narration="the lights stutter")
    assert "lights stutter" in p


def test_tone_is_carried_into_every_prompt():
    """Without tone, sunny footage appears in a blackout story."""
    p = imagegen.build_prompt("a supermarket aisle", tone="night, blackout, unlit")
    assert "night" in p and "blackout" in p


def test_generate_rejects_an_empty_prompt_without_calling_out():
    assert imagegen.generate("   ", pathlib.Path("unused.jpg")) is None


def test_generate_returns_none_and_keeps_no_file_when_the_body_is_not_an_image(monkeypatch, tmp_path):
    """An error page must not be saved as a .jpg and handed on as imagery."""
    monkeypatch.setattr(imagegen.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(imagegen.time, "sleep", lambda s: None)
    dest = tmp_path / "x.jpg"
    assert imagegen.generate("a street", dest) is None
    assert not dest.exists()
