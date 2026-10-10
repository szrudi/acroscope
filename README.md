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

The data dir (`ACROSCOPE_DATA`; on the server a bind mount of the homelab's `Data/fpv` dataset) holds the footage,
the logs, and what arrives:

```
videos/<YYYY-MM-DD[-session]>/<id>.mp4       clips: H.264, no-signal stretches cut; the file name is the clip's id,
                                             its readable name (<date>_NNN) is in the database (older clips still
                                             carry that name as the file name)
blackbox/*.bbl                               full flash dumps, many arms per file
inbox/<batch>/                               what the laptop drops: VID*.mov off the card, *.bbl off a quad, an optional
                                             import.json ({date, session, imported_at, host}) and a .done marker written
                                             last (optionally a manifest {"files": {name: size}}); consumed by ingest
originals/<session>/<id>.mov                 the originals, kept 7 days after the compressed clip verified
state/acroscope-<date>.db                    nightly copies of the database (seven kept)
```

**Ingest** (`acroscope/ingest.py`, the server runs it on complete batches, one at a time): the date and session from
the sidecar (else the import time; `<date>-unsorted` when unnamed), the clips numbered `<date>_NNN` continuing the
day's sequence over all its sessions in card order, each compressed (no-signal runs of 5 s or more cut with 1 s of
padding, libx264 crf 23, keyframe every 0.5 s) and verified against the kept stretches, the logs moved to
`blackbox/` and attached to the session, the arms decoded, automatch run on the new clips. A failed batch stays in
`inbox/` with its error shown; a retry does not import a clip twice.

Everything acroscope knows about a session (its clips with duration and codec, its blackbox files, the matches
video <-> arm + offset, the moments, the notes) is in one SQLite database (`ACROSCOPE_DB`, default
`~/.local/share/acroscope/acroscope.db`), owned by the server. The server walks the data dir in the background (at
start, every 5 minutes, and on `refresh`): new session folders and clips are registered, a clip whose file is gone
is marked `missing_since` and keeps its matches and moments until an explicit `purge`. A folder that cannot be
listed, or lists no clip at all, marks nothing, so an unmounted or half-synced Drive never looks like a deletion.

A CLI on another machine does not open the database: set `ACROSCOPE_URL=http://<server>:8070` and it uses the
server's API for everything in the database, while metrics, events and frames still run locally against that
machine's own mount of the data dir. The pre-database `videos/<session>/session.json` files are imported once by
`acroscope import-json` (a server with an empty database does it at start) and not read afterwards.

The cache (`ACROSCOPE_CACHE`, default `~/.cache/acroscope`) holds decoded arms (`arms/<bbl>/arm-NN.bin` plus
`arms.json`), extracted frames, ffprobe results, OSD readings and H.264 proxies. Delete it any time.

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

The homelab runs it as the Komodo stack `acroscope` (one container from `Dockerfile` + `compose.yaml`: `acroscope
serve` over a bind mount of the data dir), reachable at `acroscope.hakhorst.eu`. The live database is on the host in
`/opt/acroscope/state`; a nightly copy lands in the data dir's `state/`, so clips and index restore from one
snapshot. Hosting notes live in the homelab repo, `services/acroscope.md`.

## CLI (the agent side)

```
acroscope sessions [session]                     list sessions / show one: clips, arms with coverage, moments
acroscope scan                                   register every session folder (the server does it every 5 min)
acroscope refresh <session>                      the folder's clips in (durations, codecs), gone clips marked missing, bbl by date
acroscope purge <session> [clip] [--days N --yes]   remove clips marked missing, with their matches and moments
acroscope note <session> "<text>" [--clip NNN]   session or clip note
acroscope attach|detach <session> <bbl>          a blackbox file dated another day
acroscope import-json [session]                  one-off: the pre-database session.json files
acroscope session rename|date|move|merge ...     rename a session (the date is its first ten characters), change its date,
                                                 move a clip to another session, merge one session into another
acroscope inbox                                  the batches in inbox/ and their status
acroscope ingest [batch...]                      consume batches now (the server does it on its own)
acroscope housekeeping                           originals past retention go; the database is backed up into state/
acroscope arms <bbl>                             list the arms (decodes the whole file into the cache once, ~30 s)
acroscope decode <bbl> <arm> [--csv]             cache one arm; --csv dumps it like blackbox_decode for the old scripts
acroscope match <session> <video> <bbl> <arm> <offset>   record a video <-> arm match
acroscope metrics <session> <video> <from> <to> [--profile [STEP]] [--loop] [--orbit]
acroscope metrics --arm <bbl>:<n> <from> <to>    same, in arm seconds without a session
acroscope events <session> [video]               detector suggestions (flip, roll, powerloop, split-s, dive, crash, gyro-kick, motor-loss)
acroscope frame <session> <video> <t>... [--osd] JPEGs of frames; --osd enlarges the OSD strip (arm timer, total, vbat)
acroscope sheet <session> <video> <t>... | --from --to --every   one JPEG tiling several timestamped frames (one look for the agent)
acroscope tag <session> <video> <from> <to> "<title>" [--tags flip,crash] [--note ...] [--metrics] [--id mNN]
acroscope untag <session> <id>
acroscope moments <session>
acroscope automatch <session> [clips...] [--write]   match clips to arms from the OSD timers (see below)
acroscope osd <session> <clip>                   the timer readings and arm runs of a clip
acroscope transcode <session> [clips...] [--replace]   H.264 proxies in the cache, or replace the clips in place
acroscope serve [--port 8070]
```

Sessions and clips accept unique fragments: `acroscope events 10-07 007`. Times are seconds or `m:ss.s`.
`acroscope --version` says which store the CLI is using (the local database, or the server from `ACROSCOPE_URL`).

### API

What the player and the remote CLI use, JSON over HTTP on the server:

```
GET  /api/sessions                      the session list (counts of clips, missing clips, matches, moments)
GET  /api/session/<s>                   the session plus its arms (from the cache) and coverage
GET  /api/session/<s>/data              the session as stored: videos, blackbox, matches, moments
GET  /api/session/<s>/events?video=     detector suggestions for a clip
GET  /api/session/<s>/series?video=     throttle/tilt/vbat/gyro lanes per arm for the strip
GET  /api/session/<s>/metrics?video=&from=&to=
POST /api/session/<s>/moment            {id?, video, start, end, title, tags, note, metrics?}   DELETE /api/session/<s>/moment/<id>
POST /api/session/<s>/match             {video, bbl, arm, offset, note}                        DELETE /api/session/<s>/match?video=&arm=
POST /api/session/<s>/note              {note, video?}
POST /api/session/<s>/blackbox          {add: [...], remove: [...]}
POST /api/session/<s>/refresh           {probe?}      walks the folder, returns the session
POST /api/session/<s>/purge             {video?, days?}
POST /api/session/<s>/rename            {name}        POST /api/session/<s>/date   {date}
POST /api/session/<s>/move              {video, to}   POST /api/session/<s>/merge  {into}
GET  /api/inbox                         batches and their status    POST /api/inbox/<batch>/retry
POST /api/scan   POST /api/import       GET /api/store
```

### Matching clips to arms

`acroscope automatch <session> [clips] --write` does it from the footage: it reads the two OSD timers (time since
arming, total armed time) at 2 fps with digit templates learned from labelled frames, turns the timer runs into arm
starts in video time, and aligns them to the log's arms by length and by the total counter against the boot's
cumulative length. Offsets come out within about half a second, and the method carries across the static stretches
`cobra-compress.py` cuts out. Arms shorter than about 2 s and arms whose timer never shows up readable stay
unmatched, as do clips without the OSD.

By hand, when needed: `acroscope arms <bbl>` lists the arms and the **boots** (power cycles; the log's time field is
the FC uptime, so one offset covers every arm of a boot). Read one frame per boot with `acroscope frame ... --osd`,
record `acroscope match <session> <clip> <bbl> <arm> <offset> --boot`, and refine on a sharp event: an impact in
`acroscope events`, or the STATS screen the OSD shows right after a disarm (its TOTAL ARM equals the arm length).
`acroscope unmatch` removes matches. The OSD seconds are floor values, so a single frame gives ±1 s.

The digit templates live in `acroscope/static/osd-templates.json`; `acroscope osd-learn scripts/osd-labels.json`
rebuilds them when the OSD font or layout changes (label a few frames by eye first, and label the exact frame the
reader sees: `acroscope osd` and the ASCII dumps in the labels workflow, not a JPEG from another extraction).

## Player

`acroscope serve` on the box that holds the data; open `http://<host>:8070/` in Chrome. Pick a session, pick a clip,
play. Keys: space, arrows (2 s, shift 10 s), `,` `.` (frame), `i` `o` (in/out), `t` (tag from in/out), `n` `p`
(next/previous moment), `l` (loop the current moment). The strip under the video shows the moments, the suggested
untagged events (hollow), the arm coverage and the throttle. The readout shows arm time, throttle, tilt and vbat at
the playhead.

Chrome on Linux has no HEVC decoder: clips before the H.264 switch need `acroscope transcode <session>` (proxies in
the cache; `--replace` rewrites the files in the data dir).

## Units and conventions (from the log)

Every `metrics` result carries a `conventions` block with these facts, so they never need re-deriving.

`gyroADC`/`gyroUnfilt` in deg/s; `rcCommand[0..2]` ±500 = ±100 % stick, `rcCommand[3]` 1000–2000; `vbatLatest` in
0.01 V; `accSmooth` /2048 = g; `imuQuaternion` /32767. Roll + = right, pitch + = nose down, yaw + = nose right; a
back flip or a powerloop integrates to a negative pitch angle. The firmware's `flightModeFlags` labels are wrong
(shows ANGLE while the OSD shows AIR).
