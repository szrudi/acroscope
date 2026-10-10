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
  cuts TEXT, original TEXT, original_until TEXT,
  PRIMARY KEY (session, file));
CREATE TABLE IF NOT EXISTS blackbox (
  session TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE, file TEXT NOT NULL, pos INTEGER NOT NULL,
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
  error TEXT, progress TEXT);
CREATE TABLE IF NOT EXISTS batch_clips (
  batch TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE, source TEXT NOT NULL, file TEXT NOT NULL, session TEXT NOT NULL,
  PRIMARY KEY (batch, source));
CREATE TABLE IF NOT EXISTS tag_categories (name TEXT PRIMARY KEY, color TEXT NOT NULL, pos INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tags (
  name TEXT PRIMARY KEY, category TEXT NOT NULL REFERENCES tag_categories(name), color TEXT, pos INTEGER NOT NULL);
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
        for col, decl in (("name", "TEXT"), ("cuts", "TEXT"), ("original", "TEXT"), ("original_until", "TEXT")):
            if col not in have:
                self.c.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl}")
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
            for k in ("missing_since", "cuts", "original", "original_until"):
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
        """Set name, cuts (a list), original, original_until on a clip."""
        allowed = {"name", "cuts", "original", "original_until", "codec", "duration"}
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
        files = self._blackbox(session)
        if bbl not in files:
            self.set_blackbox(session, files + [bbl])
        return self._blackbox(session)

    def detach(self, session: str, bbl: str) -> list[str]:
        self.set_blackbox(session, [f for f in self._blackbox(session) if f != bbl])
        return self._blackbox(session)

    # ---- matches ----------------------------------------------------------------------------------------------

    def set_match(self, session: str, video: str, bbl: str, arm: int, offset: float, note: str = "") -> dict:
        self.ensure_session(session)
        self.c.execute("""INSERT INTO matches (session, video, bbl, arm, offset, note) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session, video, bbl, arm) DO UPDATE SET offset = excluded.offset, note = excluded.note""",
                       (session, video, bbl, arm, round(offset, 2), note))
        self.attach(session, bbl)
        return {"video": video, "bbl": bbl, "arm": arm, "offset": round(offset, 2), "note": note}

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
            self.c.execute("DELETE FROM videos WHERE session = ? AND file = ?", (session, r["file"]))
            out.append({"video": r["file"], "missing_since": r["missing_since"], "matches": n_m, "moments": n_t})
        if out:
            self._touch(session)
        return out

    # ---- editing sessions -------------------------------------------------------------------------------------

    def rename_session(self, old: str, new: str) -> None:
        """Rename a session (and everything that names it); the date becomes the new name's first ten characters."""
        if self.exists(new):
            raise ValueError(f"session {new} exists")
        row = self.c.execute("SELECT * FROM sessions WHERE name = ?", (old,)).fetchone()
        if not row:
            raise ValueError(f"session {old}: no such session")
        self.c.execute("BEGIN")
        try:
            self.c.execute("INSERT INTO sessions (name, date, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (new, new[:10], row["note"], row["created_at"], now()))
            for t in ("videos", "blackbox", "matches", "moments", "batch_clips"):
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
        self.ensure_session(to, to[:10])
        self.c.execute("BEGIN")
        try:
            # moment ids count per session (m01, m02, ...): one that `to` already uses gets the next free id there
            for r in self.c.execute("SELECT id FROM moments WHERE session = ? AND video = ? ORDER BY start", (session, file)).fetchall():
                taken = self.c.execute("SELECT 1 FROM moments WHERE session = ? AND id = ?", (to, r["id"])).fetchone()
                self.c.execute("UPDATE moments SET session = ?, id = ? WHERE session = ? AND id = ?",
                               (to, self.next_id(to) if taken else r["id"], session, r["id"]))
            self.c.execute("UPDATE matches SET session = ? WHERE session = ? AND video = ?", (to, session, file))
            self.c.execute("UPDATE batch_clips SET session = ? WHERE session = ? AND file = ?", (to, session, file))
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
            out.append(b)
        return out

    def set_batch(self, batch: str, **fields) -> None:
        allowed = {"status", "session", "date", "started_at", "finished_at", "error", "progress"}
        cols = {k: (json.dumps(v) if k == "progress" and v is not None else v) for k, v in fields.items() if k in allowed}
        self.c.execute("INSERT OR IGNORE INTO batches (id, status) VALUES (?, 'pending')", (batch,))
        if cols:
            self.c.execute(f"UPDATE batches SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?", (*cols.values(), batch))

    def batch_clips(self, batch: str) -> dict:
        return {r["source"]: r["file"] for r in self.c.execute("SELECT source, file FROM batch_clips WHERE batch = ?", (batch,))}

    def add_batch_clip(self, batch: str, source: str, file: str, session: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO batch_clips (batch, source, file, session) VALUES (?, ?, ?, ?)", (batch, source, file, session))

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

    def set_category(self, name: str, color: str | None = None) -> dict:
        """Create a category (colour required) or change its colour; the position is kept."""
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
