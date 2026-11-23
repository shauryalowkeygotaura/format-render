"""Print the next brief a scheduled CI run should render, or nothing.

    python scripts/next_brief.py            # -> brief stem, or empty when the queue is done

Next = the oldest brief (by filename) that passed format-engine's differentiation gate and is not
listed in rendered.txt. A blocked brief is never picked: the gate is enforced here too.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from formatrender import config  # noqa: E402

STEM = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")   # whitelist: the stem lands on a command line


def main() -> int:
    done_file = ROOT / "rendered.txt"
    done = set(done_file.read_text(encoding="utf-8").split()) if done_file.exists() else set()
    for p in sorted(config.BRIEF_DIR.glob("*.json")):
        if p.stem in done or not STEM.match(p.stem):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (d.get("gate") or {}).get("passed", True) and d.get("beats"):
            print(p.stem)
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
