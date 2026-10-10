# Working on acroscope

acroscope is Rudi's FPV trick-learning tool: agent CLI + player over one data layer (`README.md` has the
architecture and the CLI). This file is for the session that changes the code.

## Where things are
- Code: this repo (`szrudi/acroscope`, main). Venv `.venv` (`.venv/bin/acroscope`). ffmpeg/ffprobe on the PATH.
- Data (source of truth): `~/gdrive/fpv` = Rudi's Drive "FPV drone" folder, mounted by rclone. `videos/<session>/session.json`
  holds matches and moments; never edit those by hand when a CLI command exists (`match`, `tag`, `automatch`).
- Cache: `~/.cache/acroscope` (decoded arms, frames, OSD readings, proxies). Disposable.
- Hosted player: Komodo stack `acroscope` on LXC 109 (10.10.10.17), `https://acroscope.hakhorst.eu`. Git-backed against
  this repo but **not auto-deployed**: after `git push`, redeploy the stack (Komodo API `DeployStack`, creds in BWS
  `komodo_api_key`/`komodo_api_secret`, or the Komodo UI). Hosting docs: homelab repo `services/acroscope.md`.
- Backlog: GitHub issues on `szrudi/acroscope`. The flight-analysis session files them; this session implements.

## Conventions
- Commit as you go with full messages (the trail of how a conclusion was reached is the record); don't squash. Rudi
  reviews; never self-merge anything in the homelab repo.
- Test against real data before committing: 10-07 schammer (outdoor tricks, 20 arms, every event cross-checked by hand
  in `~/gdrive/fpv/video-blackbox-index.md`), 10-09 foxeer 008 (21 known offsets for `automatch`), 10-09 attic (73
  arms, 7 boots, two logs in one session).
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
- `cuts.json` sidecars (written by `cobra-compress.py` since 2026-10-09) are not read yet; they would carry a boot offset
  across cut static exactly.
- Container runs as root (rclone mount). Hardening to the non-root pattern is a to-do.
