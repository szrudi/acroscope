# Working on acroscope

acroscope is Rudi's FPV trick-learning tool: agent CLI + player + API over one data layer (`README.md` has the
architecture, the data layout, the CLI and the API). This file is for the session that changes the code.

## Where things are
- Code: this repo (`szrudi/acroscope`, main). Venv `.venv` (`.venv/bin/acroscope`), Python 3.13; the dependencies are
  the stdlib plus orangebox, nothing else (no numpy: the container is python:3.13-slim). ffmpeg/ffprobe on the PATH.
  `gh` on this machine is logged in as `joan-grazo`, so issues and PRs filed from here carry that account.
- Data: the data dir (`ACROSCOPE_DATA`) is the acroscope LXC's `/data`, a bind mount of the homelab's `Data/fpv` dataset
  (szrudi/homelab#102): `videos/`, `blackbox/`, `inbox/`, `originals/`, `state/`. Matches, moments, notes, tags and
  batches are in the server's SQLite database (`acroscope/db.py`), live at `/opt/acroscope/state/acroscope.db` on
  the LXC with a nightly copy in `/data/state/`. **joan has no data**: `~/.config/acroscope/url` points the CLI at
  the server, the store calls go to the API, and the commands that need clips, logs or the cache (`cli.PROXIED`)
  run on the server through `POST /api/cli` (a child process, allow-listed in `server.PROXIED`); frames and sheets
  come back through `GET /api/file` into the local cache. The server is `ACROSCOPE_URL`, else the URL file, so
  unsetting the variable still reaches the live server: a throwaway run needs `ACROSCOPE_URL=` (empty: a private
  local database, said on stderr); check with `acroscope --version` (it names the store) before anything that
  writes. joan's `~/gdrive/fpv` is Rudi's Drive folder: notes and gear docs only now (the flight-analysis session's
  `CLAUDE.md`, `app-handover.md`, `video-blackbox-index.md`); the clips and logs left it on 2026-10-10.
- Ingest (`acroscope/ingest.py`): the laptop drops a batch into `inbox/` (`cobra-import-inbox.sh` on Rudi's laptop,
  via the `fpvpush` user, write-only), the server numbers, compresses, files, decodes and automatches it. The inbox
  is hostile ground (the push user owns everything in a batch): keep the regular-file/plain-name rules in
  `batch_entries`/`plain_name`/`_take`. A clip's file name is its id; its readable `<date>_NNN` name is a column;
  `sessions.resolve_clip`/`clip_path` take either, or the number. Never `video.resolve_video` in a command.
- OSD reader (`acroscope/osd.py`): one profile per OSD layout Rudi has flown (`LAYOUTS`: cells, rows, formats, glyph
  box, which row is the arm timer, which template set), templates per font in `acroscope/static/osd-templates.json`
  learned from `scripts/osd-labels.json`. After any OSD change: survey one frame (column/row brightness runs on a
  gray frame fetched through the server), add a layout; relabel only if the font changed (read cells from ASCII
  dumps, never from sheet thumbnails: three wrong labels once poisoned the rare digits). The aligner: length and
  total alignment, then the boot's uptime (offset can only go down along a boot; a line video = a·uptime + b per
  boot, the two clocks differ by ~0.3 %), then arms with no run derived along the line. `OSD_LAG` = 0.25 s.
- Trick detection (`acroscope/tricks/`, #22): an engine (`segments.py` cuts an arm into windows, `features.py` measures
  them in `metrics.summary`'s vocabulary, `expr.py` is the condition language, never `eval`) plus the shipped
  `tricks.toml`, one entry per trick with `[params]` for the numbers; `library.py` loads and refuses a bad file at
  load. `events` is `tricks.detect`. A trick change: edit the TOML, `acroscope tricks explain <bbl>:<arm>` to see
  every window's verdict, add an `examples` entry for the real moment, `acroscope tricks check` (proxied: the
  server's logs). The pre-#22 detectors were ported window for window: on 234 real arms (eleven logs, 2026-10-07 to
  10-16) every event's tag, title, why, start and end were identical except the dives' tilt range (every frame now,
  not 40 samples) and the powerloops' inverted throttle (IMU tilt > 135, not integrated pitch, which for a loop
  entered from a dive measured the climb). The server's `_events` cache is keyed by the shipped file's mtime and the pilot's layer. The recall work (#31, #29, #20): `acroscope
  tricks eval <session>` scores the tricks against Rudi's tagged moments; on the eight tagged sessions of October
  2026 it went from 3 of 28 (10-14 alone) to orbits 24/25, split-S 6/6, rolls 11/13 (the two left are crashes),
  loops 3/3, barrel roll 1/1, dives 4/7; the 51 "dives" at an arm's first frame (the quad armed on the ground) and
  the crash tumbles are gone. The tuning loop: `eval` names the misses with arm and time, `explain` shows the
  window and the condition that failed, a param or a condition changes, the moment becomes an `examples` entry,
  `check` keeps it. The pilot's layer sits over the shipped file: skill presets, own
  params, per-craft sections by the header's `Craft name` (`Library.for_craft`). It lives in the database
  (`settings` row `tricks`, one JSON document shaped like the TOML tables; `tricks.LAYER_SOURCE` is how the server
  and the local CLI hand it to the library, so the package never opens a database on its own and tests set it
  explicitly): what `acroscope tricks set | unset | on | off | skill` and the player's configuration page (#45)
  write, checked by `tricks.check_layer` before it is stored. A tricks file is the exchange format, not storage
  (Rudi, 2026-10-17): `tricks export | import` and the page's share row, `toml_out.py` writes it. `GET /api/tricks` is `tricks.describe()`. The player's `/config` page
  (`acroscope/static/config.html`, one file like the player, five tabs) edits that layer whole through
  `POST /api/tricks/layer`; its tests (`tests/test_config_page.py`, Playwright like the player's) check each knob
  against what the server then holds.
- Sharing (#57, `acroscope/share.py`): `acroscope share <session> <moment>` renders `shares/<token>/` (a cut of the
  clip without sound or metadata, `static/share.html` filled with the stick samples and the text) and records it in
  the `shares` table; `unshare` takes one down, `shares` lists them, `housekeeping` removes expired bundles and
  day-old previews. The three commands are proxied (the clips are on the server); the share text is in
  `server.FREE_TEXT`. The player's panel (⤴ on a moment, `index.html`) and the configuration page's shares tab use
  the API: `POST /api/share/preview` renders into the cache's `share-preview/<id>/` (served back at
  `/share-preview/<id>/`, the viewer in an iframe), `POST /api/share` publishes (a preview whose facts match is
  moved, not cut again), `GET /api/shares`, `DELETE /api/share/<token>`; `share.resolve` is the one place the
  window, the text, the sticks and the poster are decided. The server never serves a published share: a static server
  does (`compose.yaml`'s `shares` service, `shares.Caddyfile`, `:8071`), or `ACROSCOPE_SHARE_PUSH`/`_REVOKE` push the
  bundle elsewhere. `ACROSCOPE_SHARE_URL` is the public base URL; it and the vhost in front of `:8071` are homelab
  settings (never in this repo). The bundle must name nothing (no session, clip, log, craft, date, moment id) and
  fetch nothing but its own files: `tests/test_share.py`, `tests/test_share_api.py` and `tests/test_share_page.py`
  pin that (the panel and the tab are in `test_player.py` and `test_config_page.py`); keep them when the viewer
  changes. Tokens are `[a-z0-9]{24}` and `share.token_ok` guards every path built from one; preview ids likewise
  (`preview_ok`).
- `flightModeFlags` in the log is Betaflight's box-switch mask, not the flight modes (arming is bit 0, which is why a
  decoder calls an acro arm "ANGLE"); bit 31 is the crash-flip (turtle) switch on Rudi's quads (`blackbox.CRASH_FLIP_BIT`,
  2026-10-18: fifty arms, all throttle 0 throughout). `segments.flying` keeps every scan off those frames.
- Trick work needs real logs on joan: `acroscope fetch-log <fragment> --to <dir>/blackbox` (runs here, no deploy), then
  `ACROSCOPE_DATA=<dir> ACROSCOPE_CACHE=<cache> ACROSCOPE_URL=` with the branch's
  checkout on `PYTHONPATH` (a script run by path imports the main checkout's package otherwise): `arms` decodes, then
  `events`, `tricks check`, `tricks explain` work offline. `sessions <s> --json` saved from the server gives the
  matches and moments to score against by hand; `tricks eval` itself needs the database, so it runs on the server.
- The flight-analysis session's brief in the Drive folder (`~/gdrive/fpv/CLAUDE.md`) carries the acroscope workflow
  that session follows: keep its acroscope bullet in step with CLI changes.
- Cache: `~/.cache/acroscope` on joan (frames and sheets fetched from the server, the real-data test leftovers);
  `/opt/acroscope/cache` on the LXC (decoded arms, frames, OSD readings, probes). Disposable.
- Hosted player: Komodo stack `acroscope` on the homelab's acroscope LXC, behind the homelab's SSO. Git-backed
  against this repo but **not auto-deployed**: after `git push`, Rudi redeploys (or the Komodo API; the call, the
  addresses and the health check are in the homelab repo, private, see below). A redeploy rebuilds the image; the
  proxied CLI commands run the deployed code, so a change there is only testable from joan after a redeploy (or
  against a local server with data pulled through `/video/...` and `/api/file`). Hosting docs: homelab repo
  `services/acroscope/README.md`; `~/repos/homelab` is a shared checkout usually on another branch, so read it with
  `git show origin/main:services/acroscope/README.md`; any change there follows homelab's own CLAUDE.md.
- A local server for trying things: `ACROSCOPE_DB=/tmp/x.db ACROSCOPE_URL= acroscope serve --port 8071`, then the CLI
  against it with `ACROSCOPE_URL=http://127.0.0.1:8071`. `serve` refuses to run with a server URL set. A local
  server on joan has no clips unless you pull some (the `d4` recipe: `/video/<session>/<file>` for the clip,
  `/api/file?path=/cache/arms/<stem>/...` for the decoded arms, an empty file with the log's name in `blackbox/`).
- Backlog: GitHub issues on `szrudi/acroscope`. The flight-analysis session files them; this session implements.

## Conventions
- **This repo is public.** No internal addresses, hostnames, network layout or security posture in the code, the docs,
  commit messages, issues or PR text: that goes in the homelab repo (private), and this side links to it. Committed
  examples, tests and issue text use neutral session and place names (the synthetic `2026-10-04-motor-jitter-testing`
  kind), not the parks around Rudi's house; the real-data pins, which name their real session, are the one exception.
- **Several sessions share this checkout** (on 2026-10-11 two dev sessions were editing it at once, one on #9 and
  one on #11). Never switch branches in `~/repos/acroscope` itself and never `git add -A`: do branch work in a
  worktree (`git worktree add <scratchpad>/wt-<name> <branch>`, remove it when pushed), stage explicit paths,
  and when `git status` shows files you did not touch, leave them alone; they are another session's.
- Features and anything Rudi should see before it goes live go on a branch (`feat/...`) with a **draft PR** (since
  2026-10-11; #12 was the first); small fixes and docs may go to main. Commit as you go with full messages (the
  trail of how a conclusion was reached is the record); don't squash. Rudi reviews and merges; never self-merge
  anything, here or in the homelab repo. Komodo can deploy the stack from a branch to try a PR.
- `.venv/bin/python -m unittest discover tests` before committing: the database, the API against a server child,
  ingest end to end on a synthetic batch, the aligner on made-up data, the player in a headless browser, and the real-data pins (10-07 schammer by
  hand in `video-blackbox-index.md`; the #4 truths on 10-10 001) through the server. A pin of new behaviour is red
  until the server runs it. Red/green: write the test first for store, API, aligner and player work.
- Smoke checks with known answers through the server, seconds each: `tricks check` (the tricks file's examples, the
  10-07 pins among them); `sessions 2026-10-07` (20 arms, 15 matches, 29
  moments); `events 2026-10-07 011` (motor 1 power loss at 1:23.4, hard landing at 1:24.4); `automatch 2026-10-07 008`
  without `--write` (arms 14/15/17 at about +4.75/+20.25/+64.25); `metrics 2026-10-07 007 1:20 1:25 --loop` (-385 deg
  pitch); `automatch 2026-10-10 001` (15 arms, arm 1 at 8.0, arm 9 at 150.3, boot fit a = 0.9971).
- `transcode --replace` rewrites clips in the data dir (a second lossy generation); Rudi's call, never run it unasked.
  Clips before 2026-10-09 are still HEVC; proxies in the cache are fine.
- Pure-Python work that takes seconds (decoding, the OSD reader, detection over many arms) must run in a child
  process inside the server: a thread starves requests through the GIL. ffmpeg is a subprocess, a thread is fine.
- orangebox 0.5.0's wheel has a broken script entry point; pip exits 1 after installing. The Dockerfile tolerates it.
- ffmpeg filter graphs: `:` in drawtext text must be escaped; concat of several inputs needs `trim=end_frame=1` per input.
- Conventions the metrics ship (`metrics.CONVENTIONS`) are the single place for sign/scaling facts. Keep them true.
- The player and the configuration page are tested in a headless Chromium (`tests/test_player.py`,
  `tests/test_config_page.py`, Playwright): a server child on a scratch
  data dir, the page driven at a desktop size and at Rudi's phone's (412 x 927 CSS px), the layout measured, the
  dialogs answered, touch gestures dispatched. One-time setup per machine, no root: `scripts/browser-setup.sh`
  (Playwright into `.venv` as the `e2e` extra, the browser into `~/.cache/ms-playwright`, the five libraries
  trixie lacks unpacked into `~/.cache/acroscope-browser`); without it the tests skip. A player change gets a test
  there, red first like the rest. For a look, `page.screenshot()` and Read the PNG. What it cannot stand in for:
  the installed app's own viewport on the phone (system bars, 100dvh) and how a gesture feels; say so in the
  commit, Rudi's phone is the last check.

## Known gaps / ideas (also as issues)
- #3: read the flight counter (top right since 2026-10-10, Betaflight stats on) so every arm names itself.
- Cruise metrics for indoor sessions (throttle variance, tilt excursions, speed-up/over-correct cycles): Rudi's current
  stage is indoor cruising, not tricks.
- The kept stretches of a compressed clip are in the database (`videos.cuts`) but not used yet; they would carry a
  boot offset across cut static exactly. Legacy `cuts.json` sidecars next to older clips are not read.
- An MCP server over the same store and metrics, for agents other than Claude Code.
- The container runs as root (the dataset is root-owned); a non-root user is a to-do with the homelab side.
- No authentication on the server itself: the SSO guards only the player's URL; the container port (the door the
  CLI uses) answers every read and write to anything on the home network, by design. Path checks are the only
  authorisation. Who reaches it is in the homelab README; the plan for a token, a localhost default bind and an
  origin check on writes is in the 2026-10-17 comment on #10.
