"""Fetch every `photos` query in a brief and tile them into one contact sheet.

    python scripts/preview_photos.py <brief.json> <out.jpg>

Run BEFORE a render: a Commons hit is not the right picture ("James Cameron"
returned a different man in a suit). Prints each query with found/MISSING.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from formatrender import visuals  # noqa: E402


def main(brief_path: str, out: str) -> int:
    d = json.loads(Path(brief_path).read_text(encoding="utf-8"))
    queries = list(dict.fromkeys(
        list(d.get("hook_photos") or []) + [q for b in d.get("beats", []) for q in (b.get("photos") or [])]))
    tmp = Path(tempfile.mkdtemp())
    found = []
    for n, q in enumerate(queries):
        p, ok = visuals.fetch_exact(q, tmp / f"{n:02d}.jpg")
        print(("ok      " if ok else "MISSING ") + f"[{n:02d}] {q}")
        if ok:
            found.append(p)
    if not found:
        return 1
    cols = 4
    while len(found) % cols:
        blank = tmp / f"blank{len(found)}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:480x270",
                        "-frames:v", "1", str(blank)], check=True)
        found.append(blank)
    inputs, chains = [], []
    for i, p in enumerate(found):
        inputs += ["-i", str(p)]
        # No drawtext labels: it needs fontconfig, absent on this machine (crash 0xC0000005).
        chains.append(f"[{i}]scale=480:270:force_original_aspect_ratio=decrease,pad=480:270[v{i}]")
    layout = "|".join(f"{(i % cols) * 480}_{(i // cols) * 270}" for i in range(len(found)))
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(found))) + \
        f"xstack=inputs={len(found)}:layout={layout}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", graph,
                    "-frames:v", "1", out], check=True)
    print("sheet ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:3]))
