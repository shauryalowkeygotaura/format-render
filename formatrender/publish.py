"""Bridge a render manifest to an uploader. format-render never touches OAuth.

There is already a working, dry-run-safe YouTube uploader in
`Code/football-shorts-autopilot/modules/uploader.py`: refresh-token auth out of
Doppler, no browser at run time, and it forces a dry run when creds are absent
so a scheduled job degrades instead of crashing.

Rebuilding that here would mean a second OAuth implementation to keep working.
Instead this module converts a manifest into the neutral payload an uploader
consumes, and shells the existing one under Doppler when asked. If that repo
moves or is retired, only this file changes.

Quota reality: a YouTube upload costs 1,600 units of the free 10,000/day, so
about SIX uploads a day per channel. A master plus four shorts is 8,000 units
in one go. `plan()` reports that budget rather than letting a loop discover it
by failing.

Credential gate, checked 2026-07-30: YT_CLIENT_ID / YT_CLIENT_SECRET /
YT_REFRESH_TOKEN are NOT set in any Doppler project on this machine. Until they
are, every path here is dry-run and says so. That is the same blocker
football-shorts-autopilot is parked on; fixing it once fixes both.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config

UPLOAD_COST_UNITS = 1600
DAILY_QUOTA = 10_000

FOOTBALL_HOME = Path(os.environ.get(
    "FOOTBALL_AUTOPILOT_HOME",
    str(config.ROOT.parent / "football-shorts-autopilot")))
DOPPLER_PROJECT = os.environ.get("FR_DOPPLER_PROJECT", "youtube-title-autoresearch")


class PublishError(RuntimeError):
    pass


def creds_present() -> bool:
    return all(os.environ.get(k, "").strip()
               for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise PublishError(f"unreadable manifest {path}: {e}") from e
    if not isinstance(data, dict) or not data.get("video"):
        raise PublishError(f"{path} is not a render manifest")
    return data


def to_payloads(manifest: dict, *, include_shorts: bool = True) -> list[dict]:
    """Manifest -> one neutral upload payload per asset.

    Shorts get `#Shorts` appended to the description, which is what still
    routes a vertical upload onto the Shorts shelf, and they intentionally
    carry NO thumbnail: YouTube ignores custom thumbnails for Shorts.
    """
    tags = list(manifest.get("tags") or [])[:15]
    hashtags = " ".join(manifest.get("hashtags") or [])
    base_desc = (manifest.get("description") or "").strip()
    followup = manifest.get("end_screen_followup") or ""

    desc = base_desc
    if hashtags:
        desc += f"\n\n{hashtags}"
    if followup:
        desc += f"\n\nNext: {followup}"
    # Commons stills are CC; attribution belongs in the description, not in a
    # comment nobody reads.
    if manifest.get("real_image_ratio", 0) > 0:
        desc += "\n\nImagery: Wikimedia Commons (CC / public domain)."

    payloads = [{
        "kind": "master",
        "file": manifest["video"],
        "title": (manifest.get("title") or "")[:100],
        "description": desc[:4900],
        "tags": tags,
        "thumbnail": (manifest.get("thumbnail") or {}).get("path"),
        "made_for_kids": False,
    }]

    if include_shorts:
        for i, s in enumerate(manifest.get("shorts") or []):
            if not s.get("path"):
                continue
            payloads.append({
                "kind": "short",
                "file": s["path"],
                "title": f"{(manifest.get('title') or '')[:80]} #{i + 1}",
                "description": (base_desc[:600] + "\n\n#Shorts " + hashtags).strip()[:4900],
                "tags": tags,
                "thumbnail": None,   # ignored for Shorts
                "made_for_kids": False,
            })
    return payloads


def plan(manifest: dict, *, include_shorts: bool = True) -> dict:
    payloads = to_payloads(manifest, include_shorts=include_shorts)
    cost = len(payloads) * UPLOAD_COST_UNITS
    return {
        "assets": len(payloads),
        "quota_units": cost,
        "daily_quota": DAILY_QUOTA,
        "fits_today": cost <= DAILY_QUOTA,
        "max_per_day": DAILY_QUOTA // UPLOAD_COST_UNITS,
        "dry_run": not creds_present(),
        "payloads": payloads,
    }


def write_payloads(manifest: dict, out: Path, *, include_shorts: bool = True) -> Path:
    """Emit upload-payloads.json for any uploader, ours or someone else's."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan(manifest, include_shorts=include_shorts),
                              indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def upload_via_football(payload: dict, *, dry: bool = True) -> dict:
    """Shell the existing uploader under Doppler. Dry by default, always.

    Kept as a subprocess rather than an import: that module does a
    `sys.path.insert` and pulls in football-specific config at import time.
    """
    uploader = FOOTBALL_HOME / "modules" / "uploader.py"
    if not uploader.exists():
        raise PublishError(
            f"uploader not found at {uploader}. Set FOOTBALL_AUTOPILOT_HOME, or "
            f"consume upload-payloads.json with your own uploader.")
    if not shutil.which("doppler"):
        raise PublishError("doppler CLI not on PATH; upload creds live in Doppler")
    if dry or not creds_present():
        return {"status": "dry-run", "file": payload["file"],
                "reason": "YT_CLIENT_ID/SECRET/REFRESH_TOKEN not set"
                          if not creds_present() else "dry requested"}
    cmd = ["doppler", "run", "-p", DOPPLER_PROJECT, "-c", "dev", "--",
           "python", str(uploader), "--file", payload["file"],
           "--title", payload["title"], "--description", payload["description"]]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(FOOTBALL_HOME))
    if proc.returncode != 0:
        raise PublishError(f"upload failed: {(proc.stderr or '').strip()[-300:]}")
    return {"status": "uploaded", "file": payload["file"],
            "output": (proc.stdout or "").strip()[-300:]}
