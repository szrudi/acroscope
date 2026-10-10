# Working on acroscope

acroscope is Rudi's FPV trick-learning tool: agent CLI + player over one data layer (`README.md` has the
architecture and the CLI). This file is for the session that changes the code.

## Where things are
- Code: this repo (`szrudi/acroscope`, main). Venv `.venv` (`.venv/bin/acroscope`), Python 3.13; the dependencies are
  the stdlib plus orangebox, nothing else (no numpy: the container is python:3.13-slim). ffmpeg/ffprobe on the PATH.
  `.venv/bin/python -m unittest discover tests`: the database, the API against a server child, ingest end to end on
  a synthetic batch, and the real-data pins of 10-07 (through the server from joan); the smoke checks below are
  the rest. `gh` on this machine is logged in as `joan-grazo`, so issues and PRs filed from here carry
  that account.
- Data: the data dir (`ACROSCOPE_DATA`) is CT 109's `/data`, a bind mount of the homelab's `Data/fpv` dataset
  (szrudi/homelab#102; this repo's #5 was the app side). joan's `~/gdrive/fpv` is the old Drive mount, frozen at
  the 2026-10-10 copy: fine for the real-data tests, not for anything new.
  It holds clips, logs, the inbox and the originals; matches, moments and notes are in the server's SQLite database
  (`acroscope/db.py`), live at `/opt/acroscope/state/acroscope.db` on LXC 109 with a nightly copy in the data dir's
  `state/`. The CLI on joan must talk to that server: `export ACROSCOPE_URL=http://10.10.10.17:8070` (without it
  the CLI opens a private local database and says so on stderr). After the move joan has no mount: frames and
  sheets come from the server. The `videos/<session>/session.json` files are the pre-database format, imported
  once; nothing reads them any more. With `ACROSCOPE_URL` set, the CLI's data-reading commands (`cli.PROXIED`)
  run on the server through `POST /api/cli` (child process, allow-listed in `server.PROXIED`) and frames/sheets
  come back through `GET /api/file` into the local cache; nothing on joan needs the clips.
- Ingest (`acroscope/ingest.py`) replaces the laptop's `cobra-import.sh`/`cobra-compress.py`: the laptop only
  drops a batch into `inbox/` with a `.done` marker. Test it with `tests/test_ingest.py` (a synthetic MJPEG clip
  with a grey stretch, the CLI in a child process against a scratch data dir); the compressor's cut rules are the
  old script's, pinned in `test_keep_segments`. A clip's file name is its id; its readable `<date>_NNN` name is a
  column; `resolve_clip` takes either, or the number.
- The data dir has its own docs. Its `CLAUDE.md` is the flight-analysis session's brief (gear, decoder quirks,
  history, and the acroscope workflow that session follows: keep that bullet in step with CLI changes).
  `app-handover.md` holds the plan, the decisions (H.264 for Chrome, digit templates instead of OCR, VLC playlists
  dropped) and the status. `video-blackbox-index.md` is the hand-made index everything was checked against.
- Cache: `~/.cache/acroscope` (decoded arms, frames, OSD readings, probes, proxies). Disposable.
- Hosted player: Komodo stack `acroscope` on LXC 109 (10.10.10.17), `https://acroscope.hakhorst.eu`. The URL sits
  behind Authelia (curl gets a 302); health is `curl -s http://10.10.10.17:8070/api/sessions`, logs are
  `ssh root@10.10.10.17 docker logs --tail 50 acroscope`. Git-backed against this repo but **not auto-deployed**:
  after `git push`, redeploy with the Komodo API (`POST http://10.10.10.55:9120/execute`, body
  `{"type":"DeployStack","params":{"stack":"acroscope"}}`, headers `X-Api-Key`/`X-Api-Secret` = BWS
  `komodo_api_key`/`komodo_api_secret`; `bws` on joan needs the token the homelab repo describes) or the Komodo UI. A
  redeploy rebuilds the image. Hosting docs: homelab repo `services/acroscope.md`; `~/repos/homelab` is a shared
  checkout that is usually on another branch, so read it with `git show origin/main:services/acroscope.md`, and any
  change there follows homelab's own CLAUDE.md (worktree per session, never self-merge).
- A local `acroscope serve --port 8070` is often already running from an earlier session (log in
  `~/.cache/acroscope/serve.log`). It loaded the code at start: restart it after changing `server.py`; `static/` is
  read per request. To try things beside it with a throwaway database: `ACROSCOPE_DB=/tmp/x.db acroscope serve
  --port 8071` (an empty database imports the session.json files at start), then the CLI against it with
  `ACROSCOPE_URL=http://127.0.0.1:8071`. `serve` refuses to run with `ACROSCOPE_URL` set.
- Backlog: GitHub issues on `szrudi/acroscope`. The flight-analysis session files them; this session implements.

## Conventions
- Commit as you go with full messages (the trail of how a conclusion was reached is the record); don't squash. Rudi
  reviews; never self-merge anything in the homelab repo.
- Test against real data before committing: 10-07 schammer (outdoor tricks, 20 arms, every event cross-checked by hand
  in `~/gdrive/fpv/video-blackbox-index.md`), 10-09 foxeer 008 (21 known offsets for `automatch`), 10-09 attic (73
  arms, 7 boots, two logs in one session). Smoke checks with known answers (everything is cached, seconds each):
  `sessions 10-07` (20 arms, 12 matches, 29 moments); `events 10-07 011` (motor 1 power loss at 1:23.4, hard landing
  at 1:24.4); `automatch 10-07 008` without `--write` (arms 14/15/17 at +4.7/+20.2/+64.2, about 20 s per clip the
  first time, the OSD readings are cached under `osd/`); `metrics 10-07 007 1:20 1:25 --loop` (-385 deg pitch).
- `transcode --replace` rewrites clips in the Drive folder (a second lossy generation, a multi-GB re-sync); that is
  Rudi's call, never run it unasked. Clips before 2026-10-09 are still HEVC; proxies in the cache are fine.
- Pure-Python work that takes seconds (decoding) must run in a child process inside the server: a thread starves
  requests through the GIL.
- orangebox 0.5.0's wheel has a broken script entry point; pip exits 1 after installing. The Dockerfile tolerates it.
- OSD digit templates (`acroscope/static/osd-templates.json`) are learned from `scripts/osd-labels.json`; label the exact
  frame the reader sees (ASCII dump via `osd._frames`), never a JPEG from another extraction: the timer ticks between them.
- ffmpeg filter graphs: `:` in drawtext text must be escaped; concat of several inputs needs `trim=end_frame=1` per input.
- Conventions the metrics ship (`metrics.CONVENTIONS`) are the single place for sign/scaling facts. Keep them true.

## Known gaps / ideas (also as issues)
- Cruise metrics for indoor sessions (throttle variance, tilt excursions, speed-up/over-correct cycles): Rudi's current
  stage is indoor cruising, not tricks.
- `cuts.json` sidecars (written by `cobra-compress.py` since 2026-10-09, `{"source": "<mov>", "kept": [[from, to],
  ...]}` in source seconds) are not read yet; they would carry a boot offset across cut static exactly.
- The Drive mount is fine for the footage for now; a different store for the videos is expected later.
- An MCP server over the same store and metrics, for agents other than Claude Code.
- Container runs as root (rclone mount). Hardening to the non-root pattern is a to-do.
