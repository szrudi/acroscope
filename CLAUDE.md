# Working on acroscope

acroscope is Rudi's FPV trick-learning tool: agent CLI + player + API over one data layer (`README.md` has the
architecture, the data layout, the CLI and the API). This file is for the session that changes the code.

## Where things are
- Code: this repo (`szrudi/acroscope`, main). Venv `.venv` (`.venv/bin/acroscope`), Python 3.13; the dependencies are
  the stdlib plus orangebox, nothing else (no numpy: the container is python:3.13-slim). ffmpeg/ffprobe on the PATH.
  `gh` on this machine is logged in as `joan-grazo`, so issues and PRs filed from here carry that account.
- Data: the data dir (`ACROSCOPE_DATA`) is CT 109's `/data`, a bind mount of the homelab's `Data/fpv` dataset
  (szrudi/homelab#102): `videos/`, `blackbox/`, `inbox/`, `originals/`, `state/`. Matches, moments, notes, tags and
  batches are in the server's SQLite database (`acroscope/db.py`), live at `/opt/acroscope/state/acroscope.db` on
  LXC 109 with a nightly copy in `/data/state/`. **joan has no data**: `~/.config/acroscope/url` points the CLI at
  the server, the store calls go to the API, and the commands that need clips, logs or the cache (`cli.PROXIED`)
  run on the server through `POST /api/cli` (a child process, allow-listed in `server.PROXIED`); frames and sheets
  come back through `GET /api/file` into the local cache. Without a server configured the CLI opens a private local
  database and says so on stderr. joan's `~/gdrive/fpv` is Rudi's Drive folder: notes and gear docs only now (the
  flight-analysis session's `CLAUDE.md`, `app-handover.md`, `video-blackbox-index.md`); the clips and logs left it
  on 2026-10-10.
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
- The flight-analysis session's brief in the Drive folder (`~/gdrive/fpv/CLAUDE.md`) carries the acroscope workflow
  that session follows: keep its acroscope bullet in step with CLI changes.
- Cache: `~/.cache/acroscope` on joan (frames and sheets fetched from the server, the real-data test leftovers);
  `/opt/acroscope/cache` on CT 109 (decoded arms, frames, OSD readings, probes). Disposable.
- Hosted player: Komodo stack `acroscope` on LXC 109 (10.10.10.17), `https://acroscope.hakhorst.eu`. The URL sits
  behind Authelia (curl gets a 302); health is `curl -s http://10.10.10.17:8070/api/sessions`. Git-backed against
  this repo but **not auto-deployed**: after `git push`, Rudi redeploys (or the Komodo API: `POST
  http://10.10.10.55:9120/execute`, body `{"type":"DeployStack","params":{"stack":"acroscope"}}`, headers
  `X-Api-Key`/`X-Api-Secret` = BWS `komodo_api_key`/`komodo_api_secret`). A redeploy rebuilds the image; the
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
- **Several sessions share this checkout** (on 2026-10-11 two dev sessions were editing it at once, one on #9 and
  one on #11). Never switch branches in `~/repos/acroscope` itself and never `git add -A`: do branch work in a
  worktree (`git worktree add <scratchpad>/wt-<name> <branch>`, remove it when pushed), stage explicit paths,
  and when `git status` shows files you did not touch, leave them alone; they are another session's.
- Features and anything Rudi should see before it goes live go on a branch (`feat/...`) with a **draft PR** (since
  2026-10-11; #12 was the first); small fixes and docs may go to main. Commit as you go with full messages (the
  trail of how a conclusion was reached is the record); don't squash. Rudi reviews and merges; never self-merge
  anything, here or in the homelab repo. Komodo can deploy the stack from a branch to try a PR.
- `.venv/bin/python -m unittest discover tests` before committing: the database, the API against a server child,
  ingest end to end on a synthetic batch, the aligner on made-up data, and the real-data pins (10-07 schammer by
  hand in `video-blackbox-index.md`; the #4 truths on 10-10 001) through the server. A pin of new behaviour is red
  until the server runs it. Red/green: write the test first for store, API and aligner work.
- Smoke checks with known answers through the server, seconds each: `sessions 10-07` (20 arms, 12 matches, 29
  moments); `events 10-07 011` (motor 1 power loss at 1:23.4, hard landing at 1:24.4); `automatch 10-07 008`
  without `--write` (arms 14/15/17 at about +4.75/+20.25/+64.25); `metrics 10-07 007 1:20 1:25 --loop` (-385 deg
  pitch); `automatch 2026-10-10 001` (15 arms, arm 1 at 8.0, arm 9 at 150.3, boot fit a = 0.9971).
- `transcode --replace` rewrites clips in the data dir (a second lossy generation); Rudi's call, never run it unasked.
  Clips before 2026-10-09 are still HEVC; proxies in the cache are fine.
- Pure-Python work that takes seconds (decoding, the OSD reader, detection over many arms) must run in a child
  process inside the server: a thread starves requests through the GIL. ffmpeg is a subprocess, a thread is fine.
- orangebox 0.5.0's wheel has a broken script entry point; pip exits 1 after installing. The Dockerfile tolerates it.
- ffmpeg filter graphs: `:` in drawtext text must be escaped; concat of several inputs needs `trim=end_frame=1` per input.
- Conventions the metrics ship (`metrics.CONVENTIONS`) are the single place for sign/scaling facts. Keep them true.
- The player has no test harness and joan no browser: `node --check` the script, read the renders you can, and say
  so in the commit; Rudi's eyes are the UI test.

## Known gaps / ideas (also as issues)
- #3: read the flight counter (top right since 2026-10-10, Betaflight stats on) so every arm names itself.
- Cruise metrics for indoor sessions (throttle variance, tilt excursions, speed-up/over-correct cycles): Rudi's current
  stage is indoor cruising, not tricks.
- The kept stretches of a compressed clip are in the database (`videos.cuts`) but not used yet; they would carry a
  boot offset across cut static exactly. Legacy `cuts.json` sidecars next to older clips are not read.
- An MCP server over the same store and metrics, for agents other than Claude Code.
- The container runs as root (the dataset is root-owned); a non-root user is a to-do with the homelab side.
