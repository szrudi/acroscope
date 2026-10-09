# acroscope

A trick-learning tool for FPV: find the tricks (flips, rolls, powerloops, split-S, dives, orbits) and the crashes in
a flying session, measure each attempt from the blackbox, tag the moments in the DVR footage, and let an AI agent
coach from the numbers and the frames.

Two users over one data layer:

- the **person** uses the player (`acroscope serve`): tagged playback, tagging with in/out points, suggestions
- the **agent** (Claude Code) uses the CLI: sessions, arms, metrics, events, frames, tag

The blackbox is a supporting layer: it shows which flights the clips cover and feeds the metrics. Deep log reading
stays in the Betaflight App's blackbox viewer.

## Data layout

The data dir (`ACROSCOPE_DATA`, default `~/gdrive/fpv`, the Google Drive "FPV drone" folder) is the source of truth:

```
videos/<YYYY-MM-DD-session>/<date>_NNN.mp4   DVR clips (cobra-compress.py output; H.264 since 2026-10-10)
videos/<session>/session.json                videos, blackbox files, matches (video <-> arm + offset), moments
blackbox/*.bbl                               full flash dumps, many arms per file
```

The cache (`ACROSCOPE_CACHE`, default `~/.cache/acroscope`) holds decoded arms (`arms/<bbl>/arm-NN.bin` plus
`arms.json`), extracted frames, ffprobe results and H.264 proxies. Delete it any time.

`video time = arm time + offset`. The clips have static cut out, so an offset only holds within one arm; a clip can
have several matches.

## Install

```
python3 -m venv .venv && .venv/bin/pip install -e .     # needs ffmpeg/ffprobe on the PATH
.venv/bin/acroscope --help
```

`pip install orangebox` ends with an "Invalid script entry point: bb2csv" error from orangebox 0.5.0's wheel;
the package is installed anyway (the Dockerfile tolerates it and checks the import).

## Deployment

The homelab runs it as the Komodo stack `acroscope` (one container from `Dockerfile` + `compose.yaml`: the rclone
Drive mount plus `acroscope serve`), reachable at `acroscope.hakhorst.eu`. Hosting notes live in the homelab repo,
`services/acroscope.md`.

## CLI (the agent side)

```
acroscope sessions [session]                     list sessions / show one: clips, arms with coverage, moments
acroscope refresh <session>                      create or update session.json from the folder (durations, bbl by date)
acroscope arms <bbl>                             list the arms (decodes the whole file into the cache once, ~30 s)
acroscope decode <bbl> <arm> [--csv]             cache one arm; --csv dumps it like blackbox_decode for the old scripts
acroscope match <session> <video> <bbl> <arm> <offset>   record a video <-> arm match
acroscope metrics <session> <video> <from> <to> [--profile [STEP]] [--loop] [--orbit]
acroscope metrics --arm <bbl>:<n> <from> <to>    same, in arm seconds without a session
acroscope events <session> [video]               detector suggestions (flip, roll, powerloop, split-s, dive, crash, gyro-kick, motor-loss)
acroscope frame <session> <video> <t>... [--osd] JPEGs of frames; --osd enlarges the OSD strip (arm timer, total, vbat)
acroscope tag <session> <video> <from> <to> "<title>" [--tags flip,crash] [--note ...] [--metrics] [--id mNN]
acroscope untag <session> <id>
acroscope moments <session>
acroscope transcode <session> [clips...] [--replace]   H.264 proxies in the cache, or replace the clips in place
acroscope serve [--port 8070]
```

Sessions and clips accept unique fragments: `acroscope events 10-07 007`. Times are seconds or `m:ss.s`.

### Matching a clip to an arm (how the agent does it)

1. `acroscope arms <bbl>` for the arm lengths and battery voltages.
2. `acroscope frame <session> <clip> 0:10 0:40 1:10 --osd` and read the OSD: the top-right timer is the time since
   arming (resets every arm), the one below it the total armed time on the pack. `offset = video time - arm time`.
3. Confirm with the arm-length sequence and the vbat on the OSD vs the arm's `vbat_start`/`vbat_end`.
4. `acroscope match ...`, then `acroscope events` to fine-tune the offset on a sharp event (a crash or a flip start).

## Player

`acroscope serve` on the box that holds the data; open `http://<host>:8070/` in Chrome. Pick a session, pick a clip,
play. Keys: space, arrows (2 s, shift 10 s), `,` `.` (frame), `i` `o` (in/out), `t` (tag from in/out), `n` `p`
(next/previous moment), `l` (loop the current moment). The strip under the video shows the moments, the suggested
untagged events (hollow), the arm coverage and the throttle. The readout shows arm time, throttle, tilt and vbat at
the playhead.

Chrome on Linux has no HEVC decoder: clips before the H.264 switch need `acroscope transcode <session>` (proxies in
the cache; `--replace` rewrites the files in the data dir).

## Units and conventions (from the log)

`gyroADC`/`gyroUnfilt` in deg/s; `rcCommand[0..2]` ±500 = ±100 % stick, `rcCommand[3]` 1000–2000; `vbatLatest` in
0.01 V; `accSmooth` /2048 = g; `imuQuaternion` /32767. Roll + = right, pitch + = nose down, yaw + = nose right; a
back flip or a powerloop integrates to a negative pitch angle. The firmware's `flightModeFlags` labels are wrong
(shows ANGLE while the OSD shows AIR).
