"""Designed full-frame cards: the "every shot carries information" layer.

Measured 2026-10-23 (Research/YouTube/editing-grammar): the reference channels
never show voice-over-stock. Each shot carries something designed: the actual
email, a quote, a one-word typographic card, a big number. This module renders
those as HTML/CSS screenshots (Playwright, 1920x1080), which gives real
typography and layout. ffmpeg drawtext cannot even run here (no fontconfig).

A beat lists them in its brief as `cards`, rendered as that beat's first shots:

    {"kind": "email",  "from": "Mark Negley", "to": "Stockton Rush",
     "subject": "Titan hull analysis", "date": "March 2018",
     "body": "We think you are at high risk ...", "highlight": "high risk"}
    {"kind": "quote",  "text": "She is unsinkable.", "by": "Rob McCallum, email, 2018"}
    {"kind": "title",  "text": "WARNING", "sub": "3 / 6"}
    {"kind": "stat",   "value": "3,346 m", "label": "last ping"}

Text is HTML-escaped; only the documented fields are read.
"""
from __future__ import annotations

import html
from pathlib import Path

W, H = 1920, 1080

_BASE_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:%dpx; height:%dpx; overflow:hidden; }
body { background:#07090d; color:#f2f2f2;
       font-family:'Segoe UI', 'Inter', 'Helvetica Neue', Arial, sans-serif; }
.vignette { position:absolute; inset:0;
  background:radial-gradient(ellipse at center, rgba(40,60,90,.35) 0%%, rgba(0,0,0,0) 55%%, rgba(0,0,0,.65) 100%%); }
""" % (W, H)


def _e(v) -> str:
    return html.escape(str(v or ""))


def _hl(text: str, phrase: str) -> str:
    """Escape text, then wrap the first occurrence of phrase in a highlight."""
    t = _e(text)
    p = _e(phrase)
    if p and p in t:
        t = t.replace(p, f'<mark>{p}</mark>', 1)
    return t


def _email(c: dict) -> str:
    return f"""
<div class="vignette"></div>
<div class="win">
  <div class="bar"><span></span><span></span><span></span><b>Inbox</b></div>
  <div class="head">
    <div class="subj">{_e(c.get('subject'))}</div>
    <div class="row"><div class="av">{_e((c.get('from') or '?')[:1])}</div>
      <div><div class="from">{_e(c.get('from'))}</div>
      <div class="to">to {_e(c.get('to'))} &middot; {_e(c.get('date'))}</div></div></div>
  </div>
  <div class="body">{_hl(c.get('body'), c.get('highlight'))}</div>
</div>
<style>
.win {{ position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
  width:1240px; background:#fbfbfa; color:#1d1f23; border-radius:22px;
  box-shadow:0 40px 120px rgba(0,0,0,.65); overflow:hidden; }}
.bar {{ height:58px; background:#eceef1; display:flex; align-items:center; gap:12px; padding:0 26px; }}
.bar span {{ width:16px; height:16px; border-radius:50%; background:#d0d3d8; }}
.bar span:first-child {{ background:#ff5f57; }} .bar span:nth-child(2) {{ background:#febc2e; }}
.bar span:nth-child(3) {{ background:#28c840; }}
.bar b {{ margin-left:18px; font-weight:600; color:#6a6f78; font-size:24px; }}
.head {{ padding:40px 56px 18px; border-bottom:1px solid #e3e5e8; }}
.subj {{ font-size:46px; font-weight:700; margin-bottom:26px; }}
.row {{ display:flex; gap:22px; align-items:center; }}
.av {{ width:70px; height:70px; border-radius:50%; background:#2f5d9a; color:#fff;
  display:flex; align-items:center; justify-content:center; font-size:34px; font-weight:700; }}
.from {{ font-size:32px; font-weight:600; }} .to {{ font-size:26px; color:#6a6f78; }}
.body {{ padding:44px 56px 64px; font-size:44px; line-height:1.45; }}
mark {{ background:#ffe14d; color:#111; padding:0 6px; border-radius:4px; }}
</style>"""


def _quote(c: dict) -> str:
    return f"""
<div class="vignette"></div>
<div class="q"><div class="mark">&ldquo;</div><div class="t">{_e(c.get('text'))}</div>
<div class="by">{_e(c.get('by'))}</div></div>
<style>
.q {{ position:absolute; left:180px; right:180px; top:50%; transform:translateY(-50%); }}
.mark {{ font-family:Georgia,serif; font-size:260px; color:#e23b3b; line-height:.6; }}
.t {{ font-family:Georgia,'Times New Roman',serif; font-size:84px; line-height:1.18; font-weight:700; }}
.by {{ margin-top:46px; font-size:36px; color:#9aa3ad; letter-spacing:2px; text-transform:uppercase; }}
</style>"""


def _title(c: dict) -> str:
    return f"""
<div class="vignette"></div>
<div class="c"><div class="t">{_e(c.get('text'))}</div><div class="s">{_e(c.get('sub'))}</div></div>
<style>
.c {{ position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; }}
.t {{ font-size:200px; font-weight:900; letter-spacing:10px; color:#fff;
  text-shadow:0 0 60px rgba(226,59,59,.55); }}
.s {{ margin-top:10px; font-size:78px; font-weight:800; color:#e23b3b; letter-spacing:8px; }}
</style>"""


def _stat(c: dict) -> str:
    return f"""
<div class="vignette"></div>
<div class="c"><div class="v">{_e(c.get('value'))}</div><div class="l">{_e(c.get('label'))}</div></div>
<style>
.c {{ position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; }}
.v {{ font-size:230px; font-weight:900; color:#fff; font-variant-numeric:tabular-nums; }}
.l {{ font-size:54px; color:#9aa3ad; letter-spacing:6px; text-transform:uppercase; }}
</style>"""


_KINDS = {"email": _email, "quote": _quote, "title": _title, "stat": _stat}


def page_html(card: dict) -> str | None:
    fn = _KINDS.get(str(card.get("kind", "")).lower())
    if fn is None:
        return None
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{_BASE_CSS}</style></head><body>{fn(card)}</body></html>"


def render_cards(cards: list[dict], out_dir: Path, prefix: str) -> list[Path | None]:
    """Render every card to a PNG in ONE browser session. None for unknown kinds."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = [page_html(c) if isinstance(c, dict) else None for c in cards]
    if not any(pages):
        return [None] * len(cards)
    from playwright.sync_api import sync_playwright
    out: list[Path | None] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        for i, doc in enumerate(pages):
            if doc is None:
                out.append(None)
                continue
            dest = out_dir / f"{prefix}_card{i:02d}.png"
            page.set_content(doc, wait_until="load")
            page.screenshot(path=str(dest))
            out.append(dest)
        browser.close()
    return out
