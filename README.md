# format-render

Turns a [format-engine](../format-engine) brief into a finished video and a
thumbnail built to the format's **measured** specification, then verifies its
own output against that specification.

Running cost: **$0**. edge-tts, Wikimedia Commons and ffmpeg are all free and
keyless, so unlike format-engine this project needs no Doppler wrapper and no
API keys at all.

## Why this is its own project

format-engine answers "what should I make and is it safe to make it". This
answers "make it". Keeping them apart matters for two reasons:

1. **format-engine is quota-bound, this is not.** One is rationing 10,000
   YouTube API units a day; the other is CPU and ffmpeg. Mixing them puts a
   render loop inside something that has to count API calls.
2. **This is niche-agnostic.** `football-shorts-autopilot` has one channel's
   identity compiled into its config. Here everything topic-specific arrives in
   the brief, so pointing it at a different channel means passing a different
   file, not editing code.

The seam between them is one path (`FORMAT_ENGINE_HOME`) and two on-disk JSON
artifacts. format-engine is soft-imported; when it is absent the renderer still
works, it just cannot verify thumbnails and says so.

## Install

```bash
pip install -r requirements.txt   # edge-tts, Pillow, requests
```

Also needs `ffmpeg` and `ffprobe` on PATH.

```bash
python -m formatrender check     # preflight: binaries, packages, the seam
```

## Use

```bash
python -m formatrender briefs                      # what's renderable
python -m formatrender render <brief>              # video + thumbnail + manifest
python -m formatrender render <brief> --best-still # slower, better thumbnail
python -m formatrender thumb  <brief>              # thumbnail only, fast iteration
```

`<brief>` accepts a path, a filename, or any unique fragment of one.

## The gate is enforced here too

A brief that failed format-engine's differentiation gate is **refused**:

```
every-notorious-asteroid-...json FAILED format-engine's differentiation gate:
  This title is 82% similar to one of your own last 1 uploads. Format
  consistency is good; near-identical titles are what the inauthentic-content
  policy actually flags.
```

Rendering a blocked brief is the exact mistake the gate exists to prevent, so
it costs an explicit `--force`. A gate that the next tool in the chain ignores
is not a gate.

## The thumbnail loop

Every other renderer in this vault composites a thumbnail that looks good. This
one composites a thumbnail that matches numbers measured off channels the
algorithm is currently rewarding, then re-measures its own output:

```
format-engine deconstruct  ->  measured checklist (layout, palette, header
                               band, saturation, contrast, brightness)
format-render build        ->  composite built to those targets
format-engine analyze      ->  re-measure the composite
correction passes          ->  close the gap on the continuous traits
```

Structural traits (layout, subject count) are built right or not at all.
Continuous traits (saturation, contrast, brightness) are driven toward the
measured values by iteration, clamped to 1.5x per pass so the loop converges
instead of oscillating.

When a gap cannot be closed the tool says why, rather than reporting a bare
mismatch:

```
  layout       three_subject      scene_no_clear_subject   NO
  ! built three_subject, format wants scene_no_clear_subject: this format uses
    wide environmental frames with no single hero object. Your still is a
    subject on a plain background. Choose a beat whose cue names a landscape.
```

`--best-still` builds from every beat's image and keeps the best-matching one.
The starting image moves the score more than any post-processing does: on the
same format and the same loop, one beat's still landed 3 of 4 measured traits
while another landed 2.

## Shorts (free, no key)

Every render also cuts up to four vertical 9:16 shorts with burned captions.

Segment selection is the interesting part. football-shorts-autopilot needed a
`shortable` flag set by hand; here the windows are ranked by overlap with the
brief's **`substance` array** - format-engine's list of original, checkable
claims. A beat carrying one of those is by construction the beat with a
self-contained payoff, so the same thing that keeps the channel monetised also
tells you which 30 seconds are worth clipping.

```
Shorts (payoff-ranked from the brief's substance claims)
  short0:   75.6- 110.5s (34.8s, payoff 3.62)
  short1:  110.5- 153.8s (43.3s, payoff 3.60)
```

Captions are written as **ASS, not SRT**. SRT carries no canvas, so libass
assumes a 384x288 script resolution and a `force_style` MarginV of 260 - meant
as 260px up from the bottom of a 1920-tall frame - renders the captions at the
TOP. Stating PlayResX/PlayResY makes every value real pixels. The same bug was
found and fixed in football-shorts-autopilot.

`--no-shorts` for master only, `--shorts N` to change the cap.

## Optional upgrades (all free, all degrade cleanly)

`python -m formatrender check` reports which of these this machine has.

| Upgrade | Enable | Without it |
|---|---|---|
| Kokoro local TTS | `pip install kokoro-onnx` + `FR_TTS_PROVIDER=kokoro` | edge-tts (the default) |
| Pixabay b-roll | `PIXABAY_API_KEY` | Ken-Burns stills |
| Pexels b-roll | `PEXELS_API_KEY` (comma-separated list rotates) | Ken-Burns stills |
| Publish | `YT_CLIENT_ID/SECRET/REFRESH_TOKEN` in Doppler | `publish` writes payloads, dry-run |

Two deliberate non-defaults:

- **Kokoro is not the default despite better timbre.** It emits no word
  timings, and the shorts captions are built from those. Better captions beat
  better voice.
- **Stock b-roll is not the default despite motion.** A generic "space" clip
  under narration about the Chelyabinsk meteor is worse than a real photograph
  of the Chelyabinsk meteor.

ElevenLabs is deliberately absent. The point of this pipeline is $0, and a
metered voice inside an unattended render loop is how a cheap channel becomes
an expensive one.

## Publish

```bash
python -m formatrender publish output/<slug>
```

Converts the manifest into neutral upload payloads and reports the quota
arithmetic before anything is spent:

```
  assets        5
  quota cost    8,000 of 10,000 units/day (1,600 per upload, max 6/day)
  mode          DRY RUN
```

A master plus four shorts is 8,000 of the 10,000 free units, so one render is
close to a full day's upload budget on one channel.

format-render never touches OAuth. There is already a working, dry-run-safe
uploader in `football-shorts-autopilot/modules/uploader.py`; `publish.py`
shells it under Doppler rather than becoming a second OAuth implementation to
maintain. `upload-payloads.json` is uploader-agnostic, so any other uploader
can consume it.

## Imagery: Commons, not generated

Stills come from Wikimedia Commons under CC/public-domain licences. Generated
imagery is fine in principle, but it triggers YouTube's altered-content
disclosure and, at volume, is precisely the "mass-produced" signature the
inauthentic-content policy looks for. Real sourced photography over original
narration is the safer default for a channel meant to stay monetised.

Two hard-won details in the search ladder:

- **Every query carries the video's topic.** A bare entity name is ambiguous in
  exactly the way that ruins a faceless video: `Ceres` returns the Roman
  goddess long before the dwarf planet, and an early build shipped a 19th
  century oil painting of a reclining nude on an astronomy channel. All numeric
  checks passed. Only looking at the image caught it.
- **Filenames are filtered on word boundaries.** Substring matching was tried
  and was wrong: blocking `venus` also blocks `Venus_transit.jpg`, silently
  pushing legitimate beats to a gradient card. Ambiguous words are handled by
  disambiguating the query, not by a blocklist.

When Commons has nothing usable the beat gets a gradient card in the format's
own signature palette, and the run reports the ratio:

```
  real imagery      100% (rest are gradient cards)
```

Below 50% the tool tells you the brief's visual cues are too abstract and need
rewriting as concrete named subjects.

## Output

```
output/<title-slug>/
  <title-slug>.mp4     1920x1080 h264 + aac
  thumbnail.jpg        1280x720, built to spec
  manifest.json        title, description, tags, beats, upload checklist
  work/                per-beat audio, stills, clips, resume state
```

Upload is deliberately out of scope. The manifest carries everything an
uploader needs, so this project never touches OAuth.

## Resume

Every beat is checkpointed. A killed render resumes without re-synthesising
audio or re-downloading imagery:

```
  [1/9] hook: cached (15.1s)
  [2/9] beat0: cached (21.1s)
```

Use `--fresh` to ignore the cache.

## Layout

```
formatrender/
  config.py      resolution, voice, the FORMAT_ENGINE_HOME seam
  brief.py       load + validate a brief, enforce the gate
  tts.py         edge-tts narration with word timings
  visuals.py     Commons search, safety filter, gradient fallback, Ken Burns
  thumbnail.py   build to spec, verify, correct, pick the best still
  compose.py     beats -> clips -> concat -> master + manifest
  cli.py         command line
```

## Honest limits

- **Beat durations come from the narration, not the brief.** A model's guess at
  speech length is not what edge-tts produces.
- **Commons is not a stock library.** Abstract cues ("the weight of history")
  will not resolve. Concrete named subjects will.
- **Layout matching is only as good as the still.** No amount of
  post-processing turns a photograph of one object into a scene composition.
- **Numeric verification is not visual review.** The nude-painting incident
  passed every automated check. Look at the thumbnail before you upload it.


## The learning loop closes itself now (2026-09-10)

`format-engine` ranks formats by how the **source** channel performed. That is a
prior, not evidence. It becomes evidence only once our own uploads have view
counts attached, which needs a video id on the shipped brief row.

That closing step used to be a chore a human had to type:

```
python -m formatengine perf --attach "<brief title>" <videoId>
```

Across four shipped briefs it had been run **zero** times, so `feedback.status()`
read `OPEN - no reward data, rankings are still source-channel priors` and
nothing ever said so out loud. Two changes:

- `publish.upload_via_football()` reads the video id back off the uploader and
  calls `publish.attach_to_engine()` itself. Best-effort: format-engine may not
  be present, and bookkeeping never turns a good upload into an error.
- `format-render publish` prints `feedback.status()` at the end of every run.

### The phantom-success bug this uncovered

`publish.py` shelled `football-shorts-autopilot/modules/uploader.py` as

```
python modules/uploader.py --file X --title Y --description Z
```

against a module that had **no `__main__` block and no argument parser**. That
command imported it, defined three functions, and exited 0 having uploaded
nothing - and `upload_via_football()` read returncode 0 and returned
`{"status": "uploaded"}`. The only thing keeping a phantom success off the
record was the missing YT OAuth creds, which short-circuit to a dry run first.
The day that token landed, this would have reported a channel full of videos
that do not exist.

Fixed at both ends: the uploader now has a real CLI that emits one line of JSON
(`{"ok": true, "video_id": ...}`), it accepts `--tags` and `--thumbnail` (both
silently dropped before), and `upload_via_football()` **raises** rather than
claiming an upload it cannot prove. Returncode 0 is no longer accepted as proof.
