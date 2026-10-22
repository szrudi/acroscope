"""The database: sessions, clips, blackbox files, matches and moments in one SQLite file.

One process owns it (the server); other processes on the same host may open it too (SQLite WAL, busy timeout), and
a CLI elsewhere goes through the server (remote.Remote has the same methods). Every read returns the plain dicts the
rest of the package has always used: load() gives {session, date, note, videos, blackbox, matches, moments}.

Clips are never deleted by a scan: a clip whose file is gone gets `missing_since` set (and cleared when it comes
back); purge() is the only thing that removes rows, and it is explicit.
"""
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  name TEXT PRIMARY KEY, date TEXT, note TEXT NOT NULL DEFAULT '', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS videos (
  session TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE, file TEXT NOT NULL,
  name TEXT, duration REAL, codec TEXT, note TEXT NOT NULL DEFAULT '', missing_since TEXT,
  cuts TEXT, original TEXT, original_until TEXT, timescale REAL,
  PRIMARY KEY (session, file));
CREATE TABLE IF NOT EXISTS blackbox (
  session TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE, file TEXT NOT NULL, pos INTEGER NOT NULL,
  PRIMARY KEY (session, file));
-- logs taken out of a session by hand: the refresh does not attach them back by their date
CREATE TABLE IF NOT EXISTS detached (
  session TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE, file TEXT NOT NULL,
  PRIMARY KEY (session, file));
CREATE TABLE IF NOT EXISTS matches (
  session TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE, video TEXT NOT NULL, bbl TEXT NOT NULL,
  arm INTEGER NOT NULL, offset REAL NOT NULL, note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (session, video, bbl, arm));
CREATE TABLE IF NOT EXISTS moments (
  session TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE, id TEXT NOT NULL, video TEXT NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL, title TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
  note TEXT NOT NULL DEFAULT '', metrics TEXT, created_at TEXT, updated_at TEXT,
  PRIMARY KEY (session, id));
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, session TEXT, date TEXT, started_at TEXT, finished_at TEXT,
  error TEXT, progress TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS batch_clips (
  batch TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE, source TEXT NOT NULL, file TEXT NOT NULL, session TEXT NOT NULL,
  PRIMARY KEY (batch, source));
CREATE TABLE IF NOT EXISTS tag_categories (name TEXT PRIMARY KEY, color TEXT NOT NULL, pos INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tags (
  name TEXT PRIMARY KEY, category TEXT NOT NULL REFERENCES tag_categories(name), color TEXT, pos INTEGER NOT NULL);
-- what the pilot set in the player, as JSON documents: the tricks layer under 'tricks' (#45)
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
-- a moment published as a static bundle on a secret link (#57): what was shared, the window, until when; the row
-- stays after a revoke or an expiry as the record of what was public (the bundle itself is in the shares dir)
CREATE TABLE IF NOT EXISTS shares (
  token TEXT PRIMARY KEY, session TEXT NOT NULL, video TEXT NOT NULL, moment TEXT, start REAL NOT NULL, end REAL NOT NULL,
  text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT,
  sticks INTEGER, audio INTEGER, poster REAL,   -- what the bundle was made with, so an edit starts from it
  down_at TEXT);   -- when the bundle was taken out of the world (here and where it was pushed); NULL = may still be up
"""

# The tag vocabulary a new database starts with: categories with a colour, tags with the colour the player used
# per tag before categories existed (so the strip looks the same). Free-text tags on moments need no definition.
# tricks: the manoeuvres. milestones: firsts. poi: nice views and other things worth seeing again.
# technical: what went wrong with the quad (crashes, gyro kicks, motor loss).
SEED_CATEGORIES = [("tricks", "#f5a524"), ("milestones", "#5fd68b"), ("poi", "#9aa3b2"), ("technical", "#ff6b6b")]
SEED_TAGS = [("flip", "tricks", "#f5a524"), ("roll", "tricks", "#ffd166"), ("powerloop", "tricks", "#4cc2ff"),
             ("split-s", "tricks", "#8ab4ff"), ("dive", "tricks", "#9b7bff"), ("orbit", "tricks", "#5fd68b"),
             ("milestone", "milestones", None), ("poi", "poi", None),
             ("crash", "technical", "#ff6b6b"), ("gyro-kick", "technical", "#ff9f6b"), ("motor-loss", "technical", "#ff6bd6")]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Db:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.c.executescript(SCHEMA)
        self._migrate()
        if not self.c.execute("SELECT 1 FROM tag_categories LIMIT 1").fetchone():
            for name, color in SEED_CATEGORIES:
                self.set_category(name, color)
            for name, cat, color in SEED_TAGS:
                self.set_tag(name, cat, color)

    def __repr__(self):
        return f"sqlite {self.path}"

    @property
    def c(self) -> sqlite3.Connection:
        """A connection per thread. The server answers requests from threads; one sqlite3 connection shared between
        them fails under load (statement state is per connection) and a BEGIN in one thread swallows the others'
        writes into its transaction."""
        c = getattr(self._local, "c", None)
        if c is None:
            c = self._local.c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
        return c

    def _migrate(self) -> None:
        """Columns added after a table first shipped (CREATE TABLE IF NOT EXISTS does not add them)."""
        have = {r["name"] for r in self.c.execute("PRAGMA table_info(videos)")}
        for col, decl in (("name", "TEXT"), ("cuts", "TEXT"), ("original", "TEXT"), ("original_until", "TEXT"), ("timescale", "REAL")):
            if col not in have:
                self.c.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl}")
        if "result" not in {r["name"] for r in self.c.execute("PRAGMA table_info(batches)")}:
            self.c.execute("ALTER TABLE batches ADD COLUMN result TEXT")
        have = {r["name"] for r in self.c.execute("PRAGMA table_info(shares)")}
        for col, decl in (("sticks", "INTEGER"), ("audio", "INTEGER"), ("poster", "REAL")):
            if col not in have:
                self.c.execute(f"ALTER TABLE shares ADD COLUMN {col} {decl}")
        if "down_at" not in have:
            self.c.execute("ALTER TABLE shares ADD COLUMN down_at TEXT")
            self.c.execute("UPDATE shares SET down_at = revoked_at WHERE revoked_at IS NOT NULL")   # taken down when revoked, as it was then
        self._backfill_batch_results()

    def _backfill_batch_results(self) -> None:
        """A done batch from before results were recorded: its clips from batch_clips, its logs those attached to
        its session whose download stamp (YYYYmmdd_HHMMSS in the file name) is the batch's own stamp."""
        for b in self.c.execute("SELECT id, session FROM batches WHERE status = 'done' AND result IS NULL").fetchall():
            clips = [r["name"] or r["file"] for r in self.c.execute(
                "SELECT v.name, v.file FROM batch_clips bc JOIN videos v ON v.session = bc.session AND v.file = bc.file "
                "WHERE bc.batch = ? ORDER BY v.name", (b["id"],))]
            stamp = b["id"][:15].replace("-", "_")          # 20261010-203503-564331 -> 20261010_203503
            logs = [r["file"] for r in self.c.execute("SELECT file FROM blackbox WHERE session = ? AND file LIKE ?",
                                                      (b["session"], f"%_{stamp}_%"))] if b["session"] else []
            self.c.execute("UPDATE batches SET result = ? WHERE id = ?", (json.dumps({"clips": clips, "logs": logs}), b["id"]))
        # the readable name of a clip imported before ids: its file name without the extension
        rows = self.c.execute("SELECT session, file FROM videos WHERE name IS NULL OR name = '' OR name LIKE '%.'").fetchall()
        self.c.executemany("UPDATE videos SET name = ? WHERE session = ? AND file = ?",
                           [(Path(r["file"]).stem, r["session"], r["file"]) for r in rows])

    # ---- sessions ---------------------------------------------------------------------------------------------

    def sessions(self) -> list[dict]:
        rows = self.c.execute("""
            SELECT s.name, s.date, s.note,
              (SELECT count(*) FROM videos v WHERE v.session = s.name) AS videos,
              (SELECT count(*) FROM videos v WHERE v.session = s.name AND v.missing_since IS NOT NULL) AS missing,
              (SELECT count(*) FROM matches m WHERE m.session = s.name) AS matches,
              (SELECT count(*) FROM moments m WHERE m.session = s.name) AS moments
            FROM sessions s ORDER BY s.name""").fetchall()
        return [{"session": r["name"], "date": r["date"], "note": r["note"], "videos": r["videos"], "missing": r["missing"],
                 "matches": r["matches"], "moments": r["moments"], "blackbox": self._blackbox(r["name"])} for r in rows]

    def exists(self, session: str) -> bool:
        return self.c.execute("SELECT 1 FROM sessions WHERE name = ?", (session,)).fetchone() is not None

    def ensure_session(self, session: str, date: str | None = None) -> None:
        self.c.execute("INSERT OR IGNORE INTO sessions (name, date, created_at, updated_at) VALUES (?, ?, ?, ?)",
                       (session, date or session[:10], now(), now()))

    def load(self, session: str) -> dict:
        """The session as one dict; an unknown session gives the empty shape (nothing is created)."""
        s = self.c.execute("SELECT * FROM sessions WHERE name = ?", (session,)).fetchone()
        out = {"session": session, "date": s["date"] if s else session[:10], "note": s["note"] if s else "",
               "videos": [], "blackbox": [], "matches": [], "moments": []}
        if not s:
            return out
        for r in self.c.execute("SELECT * FROM videos WHERE session = ? ORDER BY name, file", (session,)):
            v = {"file": r["file"], "name": r["name"] or r["file"], "duration": r["duration"], "codec": r["codec"], "note": r["note"]}
            for k in ("missing_since", "cuts", "original", "original_until", "timescale"):
                if r[k]:
                    v[k] = json.loads(r[k]) if k == "cuts" else r[k]
            out["videos"].append(v)
        out["blackbox"] = self._blackbox(session)
        out["matches"] = [dict(r) for r in self.c.execute(
            "SELECT video, bbl, arm, offset, note FROM matches WHERE session = ? ORDER BY video, offset", (session,))]
        out["moments"] = [self._moment(r) for r in self.c.execute(
            "SELECT * FROM moments WHERE session = ? ORDER BY video, start", (session,))]
        return out

    def _blackbox(self, session: str) -> list[str]:
        return [r["file"] for r in self.c.execute("SELECT file FROM blackbox WHERE session = ? ORDER BY pos", (session,))]

    @staticmethod
    def _moment(r) -> dict:
        m = {"id": r["id"], "video": r["video"], "start": r["start"], "end": r["end"], "title": r["title"],
             "tags": json.loads(r["tags"]), "note": r["note"]}
        if r["metrics"]:
            m["metrics"] = json.loads(r["metrics"])
        return m

    def _touch(self, session: str) -> None:
        self.c.execute("UPDATE sessions SET updated_at = ? WHERE name = ?", (now(), session))

    def set_note(self, session: str, note: str, video: str | None = None) -> None:
        self.ensure_session(session)
        if video:
            self.c.execute("UPDATE videos SET note = ? WHERE session = ? AND file = ?", (note, session, video))
        else:
            self.c.execute("UPDATE sessions SET note = ? WHERE name = ?", (note, session))
        self._touch(session)

    # ---- clips and logs ---------------------------------------------------------------------------------------

    def upsert_video(self, session: str, file: str, duration=None, codec=None, missing_since: str | None = None,
                     keep_probe: bool = True, name: str | None = None) -> None:
        """Add or update a clip. With keep_probe, a known duration/codec is not overwritten by None. The readable
        name defaults to the file name without its extension and is kept once set."""
        self.ensure_session(session)
        self.c.execute("""INSERT INTO videos (session, file, name, duration, codec, missing_since) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session, file) DO UPDATE SET
              name = COALESCE(?, videos.name),
              duration = CASE WHEN excluded.duration IS NULL AND ? THEN videos.duration ELSE excluded.duration END,
              codec = CASE WHEN excluded.codec IS NULL AND ? THEN videos.codec ELSE excluded.codec END,
              missing_since = excluded.missing_since""",
                       (session, file, name or Path(file).stem, duration, codec, missing_since, name, keep_probe, keep_probe))
        self._touch(session)

    def set_video_fields(self, session: str, file: str, **fields) -> None:
        """Set name, cuts (a list), original, original_until, codec, duration, timescale (the factor a mis-stamped clip
        was re-timed by; None = on time) on a clip."""
        allowed = {"name", "cuts", "original", "original_until", "codec", "duration", "timescale"}
        cols = {k: (json.dumps(v) if k == "cuts" and v is not None else v) for k, v in fields.items() if k in allowed}
        if cols:
            self.c.execute(f"UPDATE videos SET {', '.join(k + ' = ?' for k in cols)} WHERE session = ? AND file = ?",
                           (*cols.values(), session, file))
            self._touch(session)

    def names_on(self, date: str) -> list[str]:
        """Every clip name starting with a date, over all sessions: numbering continues across the day's sessions."""
        return [r["name"] for r in self.c.execute("SELECT name FROM videos WHERE name LIKE ?", (date + "_%",))]

    def set_blackbox(self, session: str, files: list[str]) -> None:
        self.ensure_session(session)
        self.c.execute("DELETE FROM blackbox WHERE session = ?", (session,))
        self.c.executemany("INSERT INTO blackbox (session, file, pos) VALUES (?, ?, ?)",
                           [(session, f, i) for i, f in enumerate(files)])
        self._touch(session)

    def attach(self, session: str, bbl: str) -> list[str]:
        self.c.execute("DELETE FROM detached WHERE session = ? AND file = ?", (session, bbl))
        files = self._blackbox(session)
        if bbl not in files:
            self.set_blackbox(session, files + [bbl])
        return self._blackbox(session)

    def detach(self, session: str, bbl: str) -> list[str]:
        """The log out of the session, with its matches there (they would point at a log the session has not), and
        remembered as taken out, so the refresh does not attach it back by its date."""
        self.c.execute("BEGIN IMMEDIATE")
        try:
            self._detach(session, bbl)
            self.c.execute("COMMIT")
        except Exception:
            self.c.execute("ROLLBACK")
            raise
        self._touch(session)
        return self._blackbox(session)

    def _detach(self, session: str, bbl: str) -> None:
        self.c.execute("DELETE FROM matches WHERE session = ? AND bbl = ?", (session, bbl))
        self.c.execute("DELETE FROM blackbox WHERE session = ? AND file = ?", (session, bbl))
        if self.exists(session):
            self.c.execute("INSERT OR IGNORE INTO detached (session, file) VALUES (?, ?)", (session, bbl))

    def carry_detached(self, session: str, into: str) -> None:
        """A merge: the logs taken out of `session` stay out of `into`, unless `into` has them."""
        self.c.execute("""INSERT OR IGNORE INTO detached (session, file) SELECT ?, file FROM detached WHERE session = ?
            AND file NOT IN (SELECT file FROM blackbox WHERE session = ?)""", (into, session, into))

    def detached(self, session: str) -> set[str]:
        return {r["file"] for r in self.c.execute("SELECT file FROM detached WHERE session = ?", (session,))}

    def move_log(self, session: str, bbl: str, to: str) -> None:
        """A log out of one session (as detach) and into another, in one transaction; `to` must exist."""
        if not self.exists(session):
            raise ValueError(f"{session}: no such session")
        if to == session:
            return
        self.c.execute("BEGIN IMMEDIATE")
        try:
            if not self.exists(to):
                raise ValueError(f"{to}: no such session")
            if bbl not in self._blackbox(session):
                raise ValueError(f"{session}: no log {bbl}")
            if bbl not in self._blackbox(to):
                pos = self.c.execute("SELECT COALESCE(MAX(pos) + 1, 0) FROM blackbox WHERE session = ?", (to,)).fetchone()[0]
                self.c.execute("INSERT INTO blackbox (session, file, pos) VALUES (?, ?, ?)", (to, bbl, pos))
            self.c.execute("DELETE FROM detached WHERE session = ? AND file = ?", (to, bbl))
            self._detach(session, bbl)
            self.c.execute("COMMIT")
        except Exception:
            self.c.execute("ROLLBACK")
            raise
        self._touch(session); self._touch(to)

    # ---- matches ----------------------------------------------------------------------------------------------

    def set_match(self, session: str, video: str, bbl: str, arm: int, offset: float, note: str = "") -> dict:
        self.ensure_session(session)
        self.c.execute("""INSERT INTO matches (session, video, bbl, arm, offset, note) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session, video, bbl, arm) DO UPDATE SET offset = excluded.offset, note = excluded.note""",
                       (session, video, bbl, arm, round(offset, 2), note))
        self.attach(session, bbl)
        return {"video": video, "bbl": bbl, "arm": arm, "offset": round(offset, 2), "note": note}

    def add_match_if_attached(self, session: str, video: str, bbl: str, arm: int, offset: float, note: str = "",
                              replace: bool = False) -> bool:
        """automatch's write: the match goes in only while the log is in the session, checked in the same statement
        (set_match would attach a log taken out meanwhile back); an existing match stays unless replace. True if
        written."""
        cur = self.c.execute(f"""INSERT INTO matches (session, video, bbl, arm, offset, note)
            SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM blackbox WHERE session = ? AND file = ?)
            ON CONFLICT (session, video, bbl, arm) DO {"UPDATE SET offset = excluded.offset, note = excluded.note" if replace else "NOTHING"}""",
                             (session, video, bbl, arm, round(offset, 2), note, session, bbl))
        if cur.rowcount:
            self._touch(session)
        return cur.rowcount > 0

    def unmatch(self, session: str, video: str, arm: int | None = None) -> int:
        if arm is None:
            cur = self.c.execute("DELETE FROM matches WHERE session = ? AND video = ?", (session, video))
        else:
            cur = self.c.execute("DELETE FROM matches WHERE session = ? AND video = ? AND arm = ?", (session, video, arm))
        self._touch(session)
        return cur.rowcount

    # ---- moments ----------------------------------------------------------------------------------------------

    def next_id(self, session: str) -> str:
        ids = [r["id"] for r in self.c.execute("SELECT id FROM moments WHERE session = ?", (session,))]
        n = max((int(i[1:]) for i in ids if re.fullmatch(r"m\d+", i)), default=0)
        return f"m{n + 1:02d}"

    def tag(self, session: str, video: str, start: float, end: float, title: str, tags: list[str], note: str = "",
            metrics: dict | None = None, mid: str | None = None) -> dict:
        """Add a moment, or replace the one with id `mid` (a replace without metrics keeps the stored snapshot)."""
        self.ensure_session(session)
        mid = mid or self.next_id(session)
        self.c.execute("""INSERT INTO moments (session, id, video, start, end, title, tags, note, metrics, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session, id) DO UPDATE SET video = excluded.video, start = excluded.start, end = excluded.end,
              title = excluded.title, tags = excluded.tags, note = excluded.note,
              metrics = COALESCE(excluded.metrics, moments.metrics),
              updated_at = excluded.updated_at""",
                       (session, mid, video, round(start, 2), round(end, 2), title, json.dumps(tags), note,
                        json.dumps(metrics) if metrics else None, now(), now()))
        self._touch(session)
        return self._moment(self.c.execute("SELECT * FROM moments WHERE session = ? AND id = ?", (session, mid)).fetchone())

    def untag(self, session: str, mid: str) -> bool:
        cur = self.c.execute("DELETE FROM moments WHERE session = ? AND id = ?", (session, mid))
        if cur.rowcount:                                   # the next moment may get this id: a share made from it keeps no pointer
            self.c.execute("UPDATE shares SET moment = NULL WHERE session = ? AND moment = ?", (session, mid))
        self._touch(session)
        return cur.rowcount > 0

    # ---- removal (explicit only) ------------------------------------------------------------------------------

    def purge(self, session: str, video: str | None = None, days: float = 0) -> list[dict]:
        """Remove clips marked missing (one clip, or every clip missing for more than `days`), with their matches
        and moments. Returns what was removed."""
        q = "SELECT file, missing_since FROM videos WHERE session = ? AND missing_since IS NOT NULL"
        rows = [dict(r) for r in self.c.execute(q, (session,))]
        if video:
            rows = [r for r in rows if r["file"] == video]
        else:
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            rows = [r for r in rows if datetime.fromisoformat(r["missing_since"]).timestamp() <= cutoff]
        out = []
        for r in rows:
            n_m = self.c.execute("DELETE FROM matches WHERE session = ? AND video = ?", (session, r["file"])).rowcount
            n_t = self.c.execute("DELETE FROM moments WHERE session = ? AND video = ?", (session, r["file"])).rowcount
            self.c.execute("UPDATE shares SET moment = NULL WHERE session = ? AND video = ?", (session, r["file"]))
            self.c.execute("DELETE FROM videos WHERE session = ? AND file = ?", (session, r["file"]))
            out.append({"video": r["file"], "missing_since": r["missing_since"], "matches": n_m, "moments": n_t})
        if out:
            self._touch(session)
        return out

    def delete_clip(self, session: str, video: str) -> dict:
        """A clip's row with its matches and moments (the file is the caller's): {video, matches, moments}."""
        if not self.c.execute("SELECT 1 FROM videos WHERE session = ? AND file = ?", (session, video)).fetchone():
            raise ValueError(f"{session}: no clip {video}")
        n_m = self.c.execute("DELETE FROM matches WHERE session = ? AND video = ?", (session, video)).rowcount
        n_t = self.c.execute("DELETE FROM moments WHERE session = ? AND video = ?", (session, video)).rowcount
        self.c.execute("UPDATE shares SET moment = NULL WHERE session = ? AND video = ?", (session, video))   # the shares stay: the record
        self.c.execute("DELETE FROM videos WHERE session = ? AND file = ?", (session, video))
        self._touch(session)
        return {"video": video, "matches": n_m, "moments": n_t}

    # ---- editing sessions -------------------------------------------------------------------------------------

    def rename_session(self, old: str, new: str) -> None:
        """Rename a session (and everything that names it); the date becomes the new name's first ten characters."""
        if self.exists(new):
            raise ValueError(f"session {new} exists")
        row = self.c.execute("SELECT * FROM sessions WHERE name = ?", (old,)).fetchone()
        if not row:
            raise ValueError(f"session {old}: no such session")
        self.c.execute("BEGIN IMMEDIATE")   # the write lock first: a read-then-write transaction cannot wait out a concurrent commit
        try:
            self.c.execute("INSERT INTO sessions (name, date, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (new, new[:10], row["note"], row["created_at"], now()))
            for t in ("videos", "blackbox", "detached", "matches", "moments", "batch_clips", "shares"):
                self.c.execute(f"UPDATE {t} SET session = ? WHERE session = ?", (new, old))
            self.c.execute("UPDATE videos SET original = replace(original, ?, ?) WHERE session = ? AND original LIKE ?",
                           (f"originals/{old}/", f"originals/{new}/", new, f"originals/{old}/%"))   # the folder moves too
            self.c.execute("UPDATE batches SET session = ? WHERE session = ?", (new, old))
            self.c.execute("DELETE FROM sessions WHERE name = ?", (old,))
            self.c.execute("COMMIT")
        except Exception:
            self.c.execute("ROLLBACK")
            raise

    def move_clip(self, session: str, file: str, to: str) -> None:
        """A clip and its matches and moments go to another session (the file keeps its name: it is the id)."""
        if to == session:
            return
        self.ensure_session(to, to[:10])
        self.c.execute("BEGIN IMMEDIATE")   # the write lock first: a read-then-write transaction cannot wait out a concurrent commit
        try:
            # moment ids count per session (m01, m02, ...): one that `to` already uses gets the next free id there
            for r in self.c.execute("SELECT id FROM moments WHERE session = ? AND video = ? ORDER BY start", (session, file)).fetchall():
                taken = self.c.execute("SELECT 1 FROM moments WHERE session = ? AND id = ?", (to, r["id"])).fetchone()
                new_id = self.next_id(to) if taken else r["id"]
                self.c.execute("UPDATE moments SET session = ?, id = ? WHERE session = ? AND id = ?", (to, new_id, session, r["id"]))
                self.c.execute("UPDATE shares SET session = ?, moment = ? WHERE session = ? AND video = ? AND moment = ?",   # a share follows its moment's new id
                               (to, new_id, session, file, r["id"]))
            self.c.execute("UPDATE matches SET session = ? WHERE session = ? AND video = ?", (to, session, file))
            self.c.execute("UPDATE batch_clips SET session = ? WHERE session = ? AND file = ?", (to, session, file))
            self.c.execute("UPDATE shares SET session = ? WHERE session = ? AND video = ?", (to, session, file))   # the rest: no moment, or one kept its id
            self.c.execute("UPDATE videos SET session = ? WHERE session = ? AND file = ?", (to, session, file))
            self.c.execute("COMMIT")
        except Exception:
            self.c.execute("ROLLBACK")
            raise
        self._touch(session); self._touch(to)

    def delete_session(self, session: str) -> None:
        self.c.execute("DELETE FROM sessions WHERE name = ?", (session,))

    # ---- ingest batches ---------------------------------------------------------------------------------------

    def batches(self) -> list[dict]:
        out = []
        for r in self.c.execute("SELECT * FROM batches ORDER BY started_at"):
            b = dict(r)
            b["progress"] = json.loads(b["progress"]) if b["progress"] else None
            b["result"] = json.loads(b["result"]) if b["result"] else None
            out.append(b)
        return out

    def set_batch(self, batch: str, **fields) -> None:
        allowed = {"status", "session", "date", "started_at", "finished_at", "error", "progress", "result"}
        cols = {k: (json.dumps(v) if k in ("progress", "result") and v is not None else v) for k, v in fields.items() if k in allowed}
        self.c.execute("INSERT OR IGNORE INTO batches (id, status) VALUES (?, 'pending')", (batch,))
        if cols:
            self.c.execute(f"UPDATE batches SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?", (*cols.values(), batch))

    def batch_clips(self, batch: str) -> dict:
        return {r["source"]: r["file"] for r in self.c.execute("SELECT source, file FROM batch_clips WHERE batch = ?", (batch,))}

    def add_batch_clip(self, batch: str, source: str, file: str, session: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO batch_clips (batch, source, file, session) VALUES (?, ?, ?, ?)", (batch, source, file, session))

    def record_ingested(self, batch: str, source: str, session: str, file: str, name: str, duration, codec, **fields) -> None:
        """An ingested clip: its row, its fields and the batch's note of it, in one transaction. A clip is either
        recorded and done for the retry, or not there at all (half a row would show as missing and use up a number)."""
        self.ensure_session(session)
        self.c.execute("BEGIN IMMEDIATE")
        try:
            self.upsert_video(session, file, duration, codec, name=name)
            self.set_video_fields(session, file, **fields)
            self.add_batch_clip(batch, source, file, session)
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise

    def expired_originals(self, before: str) -> list[tuple[str, str, str]]:
        return [(r["session"], r["file"], r["original"]) for r in self.c.execute(
            "SELECT session, file, original FROM videos WHERE original IS NOT NULL AND original_until IS NOT NULL AND original_until < ?", (before,))]

    def backup_to(self, path: Path) -> None:
        """A consistent copy of the whole database (SQLite's online backup)."""
        dst = sqlite3.connect(path)
        try:
            self.c.backup(dst)
        finally:
            dst.close()

    # ---- tag vocabulary ---------------------------------------------------------------------------------------

    def tags(self) -> dict:
        """{categories: [{name, color, pos}], tags: [{name, category, color, pos}]}, both in display order."""
        return {"categories": [dict(r) for r in self.c.execute("SELECT name, color, pos FROM tag_categories ORDER BY pos")],
                "tags": [dict(r) for r in self.c.execute("SELECT name, category, color, pos FROM tags ORDER BY pos")]}

    @staticmethod
    def _color(color: str | None) -> str | None:
        """#rrggbb, or None/'' as given: the player puts it into an attribute and a style, so nothing else goes in."""
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError(f"{color!r}: not a colour (#rrggbb)")
        return color

    def set_category(self, name: str, color: str | None = None) -> dict:
        """Create a category (colour required) or change its colour; the position is kept."""
        color = self._color(color)
        row = self.c.execute("SELECT * FROM tag_categories WHERE name = ?", (name,)).fetchone()
        if row:
            if color:
                self.c.execute("UPDATE tag_categories SET color = ? WHERE name = ?", (color, name))
        else:
            if not color:
                raise ValueError(f"category {name}: a colour is needed to create it")
            pos = self.c.execute("SELECT COALESCE(MAX(pos) + 1, 0) FROM tag_categories").fetchone()[0]
            self.c.execute("INSERT INTO tag_categories (name, color, pos) VALUES (?, ?, ?)", (name, color, pos))
        return dict(self.c.execute("SELECT name, color, pos FROM tag_categories WHERE name = ?", (name,)).fetchone())

    def set_tag(self, name: str, category: str, color: str | None = None) -> dict:
        """Define a tag in a category, or move it there; color None keeps the colour, '' clears it."""
        color = self._color(color)
        if not self.c.execute("SELECT 1 FROM tag_categories WHERE name = ?", (category,)).fetchone():
            raise ValueError(f"category {category}: no such category")
        row = self.c.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone()
        if row:
            self.c.execute("UPDATE tags SET category = ?, color = ? WHERE name = ?",
                           (category, row["color"] if color is None else (color or None), name))
        else:
            pos = self.c.execute("SELECT COALESCE(MAX(pos) + 1, 0) FROM tags").fetchone()[0]
            self.c.execute("INSERT INTO tags (name, category, color, pos) VALUES (?, ?, ?, ?)", (name, category, color or None, pos))
        return dict(self.c.execute("SELECT name, category, color, pos FROM tags WHERE name = ?", (name,)).fetchone())

    def delete_tag(self, name: str) -> bool:
        """Drop the definition; moments keep the tag as free text."""
        return self.c.execute("DELETE FROM tags WHERE name = ?", (name,)).rowcount > 0

    def delete_category(self, name: str) -> bool:
        used = [r["name"] for r in self.c.execute("SELECT name FROM tags WHERE category = ?", (name,))]
        if used:
            raise ValueError(f"category {name} still has tags: {', '.join(used)}")
        return self.c.execute("DELETE FROM tag_categories WHERE name = ?", (name,)).rowcount > 0

    # ---- settings ------------------------------------------------------------------------------------------------

    def setting(self, key: str) -> dict | None:
        """{value, updated_at} of a setting, or None."""
        r = self.c.execute("SELECT value, updated_at FROM settings WHERE key = ?", (key,)).fetchone()
        return {"value": json.loads(r["value"]), "updated_at": r["updated_at"]} if r else None

    def set_setting(self, key: str, value, etag: str | None = None) -> dict:
        """Store a setting. With `etag` (a hash of the document the writer started from, tricks.layer_etag), the
        write is refused when the stored document is not that one any more: two writers never lose each other's
        changes silently."""
        if etag is not None:
            from .tricks import layer_etag
            row = self.setting(key)
            if layer_etag(row["value"] if row else None) != etag:
                raise ValueError(f"{key}: changed since you loaded it; load it again and redo the change")
        stamp = now()
        self.c.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                       (key, json.dumps(value, allow_nan=False), stamp))
        self.c.commit()
        return {"value": value, "updated_at": stamp}

    def delete_setting(self, key: str) -> bool:
        n = self.c.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount
        self.c.commit()
        return n > 0

    def tag_usage(self, session: str | None = None) -> dict:
        """{tag: number of moments carrying it}, over one session or all."""
        q, args = "SELECT tags FROM moments", ()
        if session:
            q, args = q + " WHERE session = ?", (session,)
        out: dict[str, int] = {}
        for r in self.c.execute(q, args):
            for t in json.loads(r["tags"]):
                out[t] = out.get(t, 0) + 1
        return out

    # ---- shares (#57) ------------------------------------------------------------------------------------------

    @staticmethod
    def _share(r) -> dict:
        """A share row with its state: active, expired (past expires_at) or revoked. sticks and audio are booleans
        (None on a row from before they were kept)."""
        d = dict(r)
        d["state"] = "revoked" if d["revoked_at"] else ("expired" if d["expires_at"] and d["expires_at"] <= now() else "active")
        for k in ("sticks", "audio"):
            d[k] = None if d.get(k) is None else bool(d[k])
        return d

    def add_share(self, token: str, session: str, video: str, moment: str | None, start: float, end: float, text: str,
                  expires_at: str | None, sticks: bool | None = None, audio: bool | None = None, poster: float | None = None) -> dict:
        self.c.execute("INSERT INTO shares (token, session, video, moment, start, end, text, created_at, expires_at, sticks, audio, poster) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (token, session, video, moment, start, end, text, now(), expires_at,
                        None if sticks is None else int(sticks), None if audio is None else int(audio), poster))
        return self.share(token)

    def update_share(self, token: str, **fields) -> dict | None:
        """An edit of a share that is up: start, end, text, expires_at, sticks, audio, poster (the ones given); the
        row as it is then, or None for an unknown token."""
        allowed = {"start", "end", "text", "expires_at", "sticks", "audio", "poster"}
        cols = {k: (None if v is None else int(v)) if k in ("sticks", "audio") else v for k, v in fields.items() if k in allowed}
        if cols:
            self.c.execute(f"UPDATE shares SET {', '.join(k + ' = ?' for k in cols)} WHERE token = ?", (*cols.values(), token))
        return self.share(token)

    def share(self, token: str) -> dict | None:
        r = self.c.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
        return self._share(r) if r else None

    def shares(self, all: bool = False) -> list[dict]:
        """The active shares, oldest first; with `all` the expired and revoked ones too."""
        rows = [self._share(r) for r in self.c.execute("SELECT * FROM shares ORDER BY created_at, token")]
        return rows if all else [r for r in rows if r["state"] == "active"]

    def revoke_share(self, token: str) -> bool:
        """Mark a share revoked (once). True when it was not yet."""
        return self.c.execute("UPDATE shares SET revoked_at = ? WHERE token = ? AND revoked_at IS NULL", (now(), token)).rowcount > 0

    def mark_down(self, token: str) -> bool:
        """The bundle is out of the world, here and wherever it was pushed (once)."""
        return self.c.execute("UPDATE shares SET down_at = ? WHERE token = ? AND down_at IS NULL", (now(), token)).rowcount > 0

    # ---- import of the old session.json files -----------------------------------------------------------------

    def import_json(self, path: Path) -> dict:
        """Upsert one videos/<session>/session.json (the pre-database format). Idempotent."""
        s = json.loads(Path(path).read_text())
        name = s["session"]
        self.ensure_session(name, s.get("date"))
        if s.get("note"):
            self.set_note(name, s["note"])
        for v in s.get("videos", []):
            self.upsert_video(name, v["file"], v.get("duration"), v.get("codec"))
            if v.get("note"):
                self.set_note(name, v["note"], v["file"])
        for b in s.get("blackbox", []):
            self.attach(name, b)
        for m in s.get("matches", []):
            self.set_match(name, m["video"], m["bbl"], m["arm"], m["offset"], m.get("note", ""))
        for m in s.get("moments", []):
            self.tag(name, m["video"], m["start"], m["end"], m.get("title", ""), m.get("tags", []), m.get("note", ""),
                     m.get("metrics"), m.get("id"))
        return {"session": name, "videos": len(s.get("videos", [])), "matches": len(s.get("matches", [])),
                "moments": len(s.get("moments", []))}
