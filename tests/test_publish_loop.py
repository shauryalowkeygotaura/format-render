"""Publish must not report an upload it cannot prove happened.

2026-09-10: publish.upload_via_football() shelled
`football-shorts-autopilot/modules/uploader.py`, a module that had NO __main__
block and NO argument parser. The command imported it, defined three functions,
and exited 0. The caller read returncode 0 and returned {"status": "uploaded"}.
Only the missing YT OAuth creds - which short-circuit to a dry run first - kept
that phantom success off the record.

These tests pin the two properties that stop it recurring: an id is required as
proof, and a real id closes format-engine's loop automatically.
"""
from unittest.mock import patch

import pytest

from formatrender import publish
from formatrender.publish import PublishError


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _payload():
    return {"kind": "master", "file": "v.mp4", "title": "T", "description": "D",
            "tags": ["a", "b"], "thumbnail": None}


def test_video_id_read_off_the_last_json_line():
    out = ('Doppler banner noise\n'
           'not json at all\n'
           '{"ok": true, "video_id": "abc123XYZ_-", "dry_run": false}\n')
    assert publish._video_id_from(out) == "abc123XYZ_-"


def test_video_id_ignores_a_failed_result_line():
    assert publish._video_id_from('{"ok": false, "error": "boom"}') is None


def test_video_id_survives_garbage():
    assert publish._video_id_from("") is None
    assert publish._video_id_from("{not json}") is None


def test_upload_refuses_to_claim_success_without_an_id(tmp_path):
    """Exit 0 with no id is the no-__main__ bug. It must raise, not 'succeed'."""
    up = tmp_path / "modules" / "uploader.py"
    up.parent.mkdir(parents=True)
    up.write_text("# no main block", encoding="utf-8")
    with patch.object(publish, "FOOTBALL_HOME", tmp_path), \
         patch.object(publish, "creds_present", return_value=True), \
         patch("formatrender.publish.shutil.which", return_value="/usr/bin/doppler"), \
         patch("formatrender.publish.subprocess.run", return_value=_Proc(0, "")):
        with pytest.raises(PublishError, match="no video id"):
            publish.upload_via_football(_payload(), dry=False)


def test_upload_attaches_the_id_to_format_engine(tmp_path):
    up = tmp_path / "modules" / "uploader.py"
    up.parent.mkdir(parents=True)
    up.write_text("# stub", encoding="utf-8")
    stdout = '{"ok": true, "video_id": "vid987", "dry_run": false}'
    with patch.object(publish, "FOOTBALL_HOME", tmp_path), \
         patch.object(publish, "creds_present", return_value=True), \
         patch("formatrender.publish.shutil.which", return_value="/usr/bin/doppler"), \
         patch("formatrender.publish.subprocess.run", return_value=_Proc(0, stdout)), \
         patch.object(publish, "attach_to_engine", return_value=True) as attach:
        res = publish.upload_via_football(_payload(), dry=False)
    assert res["status"] == "uploaded"
    assert res["video_id"] == "vid987"
    assert res["attached_to_engine"] is True
    attach.assert_called_once_with("T", "vid987")


def test_upload_forwards_tags_and_thumbnail(tmp_path):
    """The old command line dropped both, so nothing could ever be tagged."""
    up = tmp_path / "modules" / "uploader.py"
    up.parent.mkdir(parents=True)
    up.write_text("# stub", encoding="utf-8")
    pl = _payload(); pl["thumbnail"] = "thumb.jpg"
    stdout = '{"ok": true, "video_id": "v1"}'
    with patch.object(publish, "FOOTBALL_HOME", tmp_path), \
         patch.object(publish, "creds_present", return_value=True), \
         patch("formatrender.publish.shutil.which", return_value="/usr/bin/doppler"), \
         patch("formatrender.publish.subprocess.run", return_value=_Proc(0, stdout)) as run, \
         patch.object(publish, "attach_to_engine", return_value=False):
        publish.upload_via_football(pl, dry=False)
    cmd = run.call_args[0][0]
    assert "--tags" in cmd and "a,b" in cmd
    assert "--thumbnail" in cmd and "thumb.jpg" in cmd


def test_attach_to_engine_ignores_a_dry_run_id():
    """A DRYRUN- id is not a video and must never enter the ledger."""
    assert publish.attach_to_engine("T", "DRYRUN-v") is False
    assert publish.attach_to_engine("T", "") is False


def test_dry_run_still_short_circuits(tmp_path):
    up = tmp_path / "modules" / "uploader.py"
    up.parent.mkdir(parents=True)
    up.write_text("# stub", encoding="utf-8")
    with patch.object(publish, "FOOTBALL_HOME", tmp_path), \
         patch.object(publish, "creds_present", return_value=False), \
         patch("formatrender.publish.shutil.which", return_value="/usr/bin/doppler"):
        res = publish.upload_via_football(_payload(), dry=False)
    assert res["status"] == "dry-run"
