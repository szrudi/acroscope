# Working on acroscope

acroscope is Rudi's FPV trick-learning tool: agent CLI + player over one data layer (`README.md` has the
architecture and the CLI). This file is for the session that changes the code.

## Where things are
- Code: this repo (`szrudi/acroscope`, main). Venv `.venv` (`.venv/bin/acroscope`), Python 3.13; the dependencies are
  the stdlib plus orangebox, nothing else (no numpy: the container is python:3.13-slim). ffmpeg/ffprobe on the PATH.
  No unit tests (`tests/` is empty): the real-data runs below are the test suite. `gh` on this machine is logged in
  as `joan-grazo`, so issues and PRs filed from here carry that account.
- Data (source of truth): `~/gdrive/fpv` = Rudi's Drive "FPV drone" folder, mounted by rclone. Rudi's laptop syncs
  it with Insync and the hosted container mounts it with rclone, so a `session.json` written here is in the player
  within about a minute, and the other way round. `videos/<session>/session.json` holds matches and moments; never
  edit those by hand when a CLI command exists (`match`, `tag`, `automatch`). Session and clip notes have no CLI
  command yet (the player writes them). Every CLI command does load-modify-save; don't hold an edit across a slow step.
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
  read per request. To try things beside it: `acroscope serve --port 8071 --no-warm`.
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
- `refresh` never removes anything, so a clip deleted after compression stays listed (10-09 attic still carries
  `2026-10-09_016.mov` next to its `.mp4`).
- No CLI command for session and clip notes; the player's `POST /api/session/<s>/note` is the only writer.
- Container runs as root (rclone mount). Hardening to the non-root pattern is a to-do.
