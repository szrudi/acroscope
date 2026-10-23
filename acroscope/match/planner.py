"""A session planned in one alignment (#26): every clip's arm runs against the logs' arms in order, so each clip's
arms come from one plan of the whole session, not from the picks of the clips before it.
"""
import re

from ..sources import blackbox, osd
from ..store import sessions


def clip_order(name: str | None) -> str | None:
    """A clip's place in time: its readable name `<date>_NNN` sorts chronologically as a string. Anything else (a
    clip without a name falls back to its file name, an id) has no known place: None, and the order rules skip it."""
    return name if name and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d+", name) else None


def arm_window(key: str | None, others: list[tuple[str, int]]) -> tuple[int | None, int | None]:
    """Clips are chronological (their readable names sort by date and number) and so are a log's arms: a clip's
    arms come after every arm matched to an earlier clip and before every arm matched to a later one. `others`:
    (clip name, arm) of the log's matches in the other clips. (lo, hi): the arms must lie strictly between. A clip
    or a match without a place in time (clip_order) bounds nothing."""
    key = clip_order(key)
    if key is None:
        return None, None
    lo = max((a for k, a in others if clip_order(k) and k < key), default=None)
    hi = min((a for k, a in others if clip_order(k) and k > key), default=None)
    return lo, hi


PLAN_TOL = 2.0                     # session plan: seconds a run or an arm left without a partner costs
PLAN_MARGIN = 1.0                  # a pick whose best alternative costs less than this more is ambiguous: nothing written
PLAN_W_TOTAL = 0.3                 # weight of the OSD's total-before against the arm's place in its boot (as align())
PLAN_SLACK = 1                     # a clip's window reaches this many arms into its neighbours' (the clip's own pass,
                                   # with its boot fit, settles a boundary the lengths put one arm off: 2026-10-11 004)


def _pair_cost(r: dict, a: dict) -> float:
    """A run against an arm: the length misfit, plus (on a cumulative-timer OSD, which shows the boot's flying time
    before the run) the misfit of that total against the arm's place in its boot, as align() weighs it."""
    c = abs(r["length"] - a["length"])
    if r.get("total_before") is not None and a.get("cum_before") is not None:
        c += PLAN_W_TOTAL * abs(r["total_before"] - a["cum_before"])
    return c


def _plan_cost(runs: list[dict], arms: list[dict], forbid: tuple[int, int] | None = None, tol: float = PLAN_TOL):
    """Order-preserving alignment of every run of a session's clips (card order) to its arms (log order): a pair
    costs _pair_cost, a run or an arm without a partner costs `tol`. (cost, pairs)."""
    n, m = len(runs), len(arms)
    INF = float("inf")
    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        D[i][0] = i * tol
    for j in range(1, m + 1):
        D[0][j] = j * tol
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pair = INF if forbid == (i - 1, j - 1) else D[i - 1][j - 1] + _pair_cost(runs[i - 1], arms[j - 1])
            D[i][j] = min(pair, D[i - 1][j] + tol, D[i][j - 1] + tol)
    pairs, i, j = [], n, m
    while i and j:
        if forbid != (i - 1, j - 1) and D[i][j] == D[i - 1][j - 1] + _pair_cost(runs[i - 1], arms[j - 1]):
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif D[i][j] == D[i - 1][j] + tol:
            i -= 1
        else:
            j -= 1
    return D[n][m], pairs[::-1]


def plan_session_pairs(runs: list[dict], arms: list[dict], margin: float = PLAN_MARGIN) -> tuple[list[tuple[int, int]], set]:
    """The session's runs (each with its `clip`) against its arms in one go (#26): clip by clip, one locally best pick
    (2026-10-14 009 took arm 17, 0.7 s nearer than arm 7) bounded every later clip out of its arms. Returns (pairs,
    ambiguous clips), the ambiguous clips' pairs left out. A runner-up within `margin` makes clips ambiguous when it
    gives another clip than the one whose pair it drops other arms (two clips moving together count, #41): then where
    their windows lie is a coin toss. A close runner-up inside one clip (a dozen short hops of near equal length,
    2026-10-16) changes no other clip; which run is which arm is the clip's own pass to settle."""
    cost, pairs = _plan_cost(runs, arms)

    def arms_of(ps):
        out = {}
        for ri, ai in ps:
            out.setdefault(runs[ri].get("clip"), set()).add(ai)
        return out
    best = arms_of(pairs)
    ambiguous = set()
    for ri, ai in pairs:
        alt_cost, alt = _plan_cost(runs, arms, forbid=(ri, ai))
        if alt_cost - cost >= margin:
            continue
        other = arms_of(alt)
        changed = {c for c in set(best) | set(other) if best.get(c, set()) != other.get(c, set())}
        if changed - {runs[ri].get("clip")}:
            ambiguous |= changed
    return [(ri, ai) for ri, ai in pairs if runs[ri].get("clip") not in ambiguous], ambiguous


def _log_order(bbl: str) -> str:
    """A log's place in time: the dump's date and time in its name (BTFL_..._YYYYMMDD_HHMMSS_...), else the name."""
    m = re.search(r"_(\d{8})_(\d{6})_", bbl)
    return m.group(1) + m.group(2) if m else bbl


def _craft(bbl: str) -> str:
    """The quad a log is from: the craft name in BTFL_BLACKBOX_LOG_<craft>_<date>_<time>_..., else the file itself."""
    m = re.match(r"BTFL_BLACKBOX_LOG_(.+?)_\d{8}_\d{6}_", bbl)
    return m.group(1) if m else bbl


def session_plan(session: str, fps: float = 2.0) -> dict:
    """{clip file: {"windows": {bbl: (lo, hi)}, "planned": [(bbl, arm)], "ambiguous": bool}} for every named,
    probed, present clip: the arms each clip may hold, from one alignment of the whole session (clips in card order,
    a quad's logs' arms in dump order) instead of from the picks of the clips before it. One alignment per quad (the
    craft in the log's name): two quads flown in turn and dumped at the end interleave in the clips, not end to end
    (#41). Matches already in the session are fixed points (the clips between two of them align against the arms
    between); arms another session holds, or a clip of this one the plan leaves out (a missing one), are left out."""
    s = sessions.load(session)
    clips = sorted((v for v in s["videos"] if clip_order(v.get("name")) and v.get("duration") and not v.get("missing_since")),
                   key=lambda v: v["name"])
    if not clips:
        return {}
    files = {v["file"] for v in clips}
    mine = {v.get("name") for v in s["videos"]} | {v["file"] for v in s["videos"]}
    other = {b: [(k, a) for k, a in ms if k not in mine] for b, ms in _log_matches(s, None, []).items()}
    elsewhere = {(b, a) for b, ms in other.items() for _, a in ms}
    elsewhere |= {(m["bbl"], m["arm"]) for m in s["matches"] if m["video"] not in files}   # a missing clip keeps its arm
    # a day's clips are numbered across all its sessions, so another session's matches bound this one's arms too: an
    # arm held by an earlier-named clip comes before them, one held by a later-named clip after (2026-10-02: a log
    # holding the day's first session's arms too)
    first, last = clips[0]["name"], clips[-1]["name"]
    bound = {b: (max((a for k, a in ms if clip_order(k) and k < first), default=None),
                 min((a for k, a in ms if clip_order(k) and k > last), default=None)) for b, ms in other.items()}
    chains: dict[str, list[dict]] = {}
    for b in sorted(s["blackbox"], key=_log_order):
        try:
            idx = blackbox.index_bbl(b)
        except FileNotFoundError:
            continue
        boot_of = {i: k for k, bt in enumerate(idx.get("boots", [])) for i in bt["arms"]}
        cum, chain = {}, chains.setdefault(_craft(idx["file"]), [])
        lo_b, hi_b = bound.get(idx["file"], (None, None))
        for a in idx["arms"]:
            if not (a.get("frames") and a.get("motors_spun", True)):
                continue
            k = boot_of.get(a["index"])
            if (idx["file"], a["index"]) not in elsewhere and (lo_b is None or a["index"] > lo_b) and (hi_b is None or a["index"] < hi_b):
                chain.append({"bbl": idx["file"], "index": a["index"], "length": a["length"],
                              "cum_before": round(cum.get(k, 0.0), 1) if k is not None else None})
            if k is not None:
                cum[k] = cum.get(k, 0.0) + a["length"]
    chains = {c: arms for c, arms in chains.items() if arms}
    out = {v["file"]: {"windows": {}, "ambiguous": False, "planned": []} for v in clips}
    if not chains:
        return out
    held_any = {m["video"] for m in s["matches"]}
    runs_of = {}
    for v in clips:                                          # a clip that cannot be read has no runs, the rest plan on
        if v["file"] in held_any:
            continue
        try:
            runs_of[v["file"]] = [dict(r, clip=v["file"]) for r in osd.video_arms(osd.read_timers(sessions.clip_path(session, v["file"]), fps), fps)]
        except Exception:  # noqa: BLE001  (gone but not marked missing yet, still syncing)
            runs_of[v["file"]] = []
    for arms in chains.values():
        pos = {(a["bbl"], a["index"]): k for k, a in enumerate(arms)}
        held = {}                                            # clip -> positions of its matches in this chain
        for m in s["matches"]:
            if m["video"] in files and (m["bbl"], m["arm"]) in pos:
                held.setdefault(m["video"], []).append(pos[(m["bbl"], m["arm"])])
        fixed = {m["video"] for m in s["matches"] if m["video"] in files}   # matched in any chain: not planned again
        assigned = {v["file"]: list(held.get(v["file"], [])) for v in clips}
        ambiguous = set()
        seg, lo = [], -1
        for v in clips + [None]:                             # segments of clips without matches, between fixed points
            if v is not None and v["file"] not in held:
                if v["file"] not in fixed:
                    seg.append(v)
                continue
            hi = min(held[v["file"]]) if v is not None else len(arms)
            if seg:
                runs = [r for c in seg for r in runs_of.get(c["file"], [])]
                pairs, amb = plan_session_pairs(runs, arms[lo + 1:hi] if hi > lo else [])
                for ri, ai in pairs:
                    assigned[runs[ri]["clip"]].append(lo + 1 + ai)
                ambiguous |= amb
            seg = []
            if v is not None:
                lo = max(lo, max(held[v["file"]]))
        for k, v in enumerate(clips):
            before = [p for c in clips[:k] for p in assigned[c["file"]]]
            after = [p for c in clips[k + 1:] for p in assigned[c["file"]]]
            lo_p, hi_p = max(before, default=-1) - PLAN_SLACK, min(after, default=len(arms)) + PLAN_SLACK
            for b in {a["bbl"] for a in arms}:
                below = [a["index"] for p, a in enumerate(arms) if a["bbl"] == b and p <= lo_p]
                above = [a["index"] for p, a in enumerate(arms) if a["bbl"] == b and p >= hi_p]
                out[v["file"]]["windows"][b] = (max(below) if below else None, min(above) if above else None)
            out[v["file"]]["planned"] += [(arms[p]["bbl"], arms[p]["index"]) for p in sorted(assigned[v["file"]]) if v["file"] not in held]
            out[v["file"]]["ambiguous"] |= v["file"] in ambiguous
    for v in clips:
        if v["file"] in fixed:
            out[v["file"]]["planned"] = sorted({(m["bbl"], m["arm"]) for m in s["matches"] if m["video"] == v["file"]})
    return out


def _log_matches(session: dict, video: str, pending: list[dict]) -> dict[str, list[tuple[str, int]]]:
    """{bbl: [(clip name, arm)]} of every match in every session except this clip's own, plus the proposals of
    clips earlier in this run (pending: [{session, video, name, bbl, arm}]). A log can be attached to several
    sessions (one flash dump over several days), so an arm taken in another session is taken here too."""
    out: dict[str, list[tuple[str, int]]] = {}
    for row in sessions.all_sessions():
        if not row.get("matches"):
            continue
        s = session if row["session"] == session["session"] else sessions.load(row["session"])
        names = {v["file"]: v.get("name") or v["file"] for v in s["videos"]}
        for m in s["matches"]:
            if s["session"] == session["session"] and m["video"] == video:
                continue
            out.setdefault(m["bbl"], []).append((names.get(m["video"], m["video"]), m["arm"]))
    for m in pending:
        if not (m["session"] == session["session"] and m["video"] == video):
            out.setdefault(m["bbl"], []).append((m["name"], m["arm"]))
    return out
