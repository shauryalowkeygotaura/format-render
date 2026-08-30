"""Guards for b-roll clip selection.

Two of these exist because of specific bugs that shipped:

`test_no_unresolved_names` catches a whole function going missing. A patch
script spliced broll.py by byte offset from one `def` to another and replaced
the span, which silently deleted `_download` and `_conform` because they lived
in between. The suite stayed green: 29 tests passed against a module whose
fetch_clip could not run a single line past the search. It only surfaced as a
NameError in a render, ten minutes in.

The dedupe tests exist because the fix for repeated footage was verified by
re-running a render and counting distinct files, and the render had crashed on
its first fetch, so the count being measured was the previous run's leftovers.
Logic worth trusting is worth asserting on directly.
"""
from __future__ import annotations

import ast
import builtins
import pathlib

from formatrender import broll


def test_no_unresolved_names():
    """Every name a broll function calls is actually defined in the module."""
    src = pathlib.Path(broll.__file__).read_text(encoding="utf-8")
    mod = ast.parse(src)
    top = {n.name for n in mod.body if isinstance(n, ast.FunctionDef)}
    top |= {t.id for n in mod.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}
    top |= {n.names[0].asname or n.names[0].name.split(".")[0]
            for n in mod.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    unresolved = {}
    for fn in (n for n in mod.body if isinstance(n, ast.FunctionDef)):
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        missing = {c for c in called if c not in top and not hasattr(builtins, c)}
        if missing:
            unresolved[fn.name] = sorted(missing)
    assert not unresolved, f"calls names that do not exist: {unresolved}"


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _videos(*ids):
    return {"videos": [
        {"id": i, "duration": 30,
         "video_files": [{"link": f"http://v/{i}.mp4", "width": 1920}]}
        for i in ids]}


def test_pexels_skips_excluded_and_keeps_looking(monkeypatch):
    """An excluded clip advances to the next result rather than giving up.

    The original bug `break`ed on a match, so one used clip ended the search and
    the caller fell back to the same footage anyway.
    """
    monkeypatch.setattr(broll, "_pexels_key", lambda: "k")
    monkeypatch.setattr(broll.requests, "get",
                        lambda *a, **k: _Resp(_videos(11, 22, 33)))

    assert broll._search_pexels("aurora", (1920, 1080)) == ("http://v/11.mp4", "pexels:11")
    # First two used: must return the third, not None and not a repeat.
    got = broll._search_pexels("aurora", (1920, 1080),
                               frozenset({"pexels:11", "pexels:22"}))
    assert got == ("http://v/33.mp4", "pexels:33")
    # All used: no clip rather than a duplicate.
    assert broll._search_pexels("aurora", (1920, 1080),
                                frozenset({"pexels:11", "pexels:22", "pexels:33"})) is None


def test_fetch_clip_records_only_on_success(monkeypatch, tmp_path):
    """A failed download must not blacklist footage that never reached the video."""
    monkeypatch.setattr(broll, "enabled", lambda: {"pexels": True, "pixabay": False})
    monkeypatch.setattr(broll, "_search_pexels",
                        lambda *a, **k: ("http://v/11.mp4", "pexels:11"))

    used: set[str] = set()
    monkeypatch.setattr(broll, "_download", lambda url, dest: False)
    assert broll.fetch_clip("aurora", tmp_path / "a.mp4", 4.0, (1920, 1080),
                            used_ids=used) is None
    assert used == set(), "a failed download must not consume the clip id"

    out = tmp_path / "b.mp4"
    monkeypatch.setattr(broll, "_download", lambda url, dest: True)
    monkeypatch.setattr(broll, "_conform", lambda src, o, d, s: o)
    assert broll.fetch_clip("aurora", out, 4.0, (1920, 1080), used_ids=used) == out
    assert used == {"pexels:11"}


def test_fetch_clip_forwards_used_ids(monkeypatch, tmp_path):
    """The set actually reaches the search. Without this the dedupe is inert."""
    seen = {}
    monkeypatch.setattr(broll, "enabled", lambda: {"pexels": True, "pixabay": False})

    def spy(term, size, exclude=frozenset()):
        seen["exclude"] = exclude
        return None

    monkeypatch.setattr(broll, "_search_pexels", spy)
    broll.fetch_clip("aurora", tmp_path / "c.mp4", 4.0, (1920, 1080),
                     used_ids={"pexels:99"})
    assert seen["exclude"] == frozenset({"pexels:99"})


def test_search_pool_deep_enough_for_a_full_video():
    """The result pool must outlast one video's shot count.

    Dedupe turns a shallow pool into a visible defect rather than a hidden one:
    with per_page=10 a 44-shot render exhausted the ~9 usable clips a cue
    returns and fell through to flat gradient shots for a whole beat. Both APIs
    return this many results in a single call, so depth is free.
    """
    assert broll.SEARCH_POOL >= 40, "too shallow; beats will fall back to gradient"
