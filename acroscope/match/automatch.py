"""Automatch: a clip's arms matched from its OSD timers, inside the window the session plan and the other clips'
matches leave it, then written or proposed.
"""
from . import pairing, planner
from ..sources import blackbox, osd
from ..store import sessions


def full_point(s: dict, name: str, after: str | None, fps: float, here: tuple) -> dict | None:
    """Where a log that ended with the flash full ends: the first run, in the session's clips after `after` (the last
    clip holding arms of the log) up to this one (`here` = (read_timers result, runs, path)), whose STATS screen
    shows BLACKBOX 100 %. {clip, run, at, total_arm} (at = the run's start), or None.

    A 100 % only counts when it is this fill: after a clip holding arms of the log, or after a reading below 100 %.
    A flash is erased between fills (10-07: 001 shows the previous fill at 100 %, the erase is in 003, this log
    starts after it), and an erase leaves no trace in the footage the reader sees."""
    if planner.clip_order(name) is None:
        return None
    clips = [(planner.clip_order(v.get("name")), v) for v in s["videos"] if v.get("duration")]
    clips = sorted((k, v) for k, v in clips if k is not None and k < name and (after is None or k > after))
    seen_below = after is not None
    for key, v in clips + [(name, None)]:
        try:
            if v is None:
                rt, runs, path = here
            else:
                path = sessions.clip_path(s["session"], v["file"])
                rt = osd.read_timers(path, fps)
                runs = osd.video_arms(rt, fps)
            stats = osd.read_stats(path, runs, rt["layout"])
        except Exception:  # noqa: BLE001  (a clip gone or unreadable says nothing)
            continue
        for k, st in enumerate(stats):
            bb = st.get("blackbox")
            if bb is not None and bb < 100:
                seen_below = True
            elif bb == 100 and seen_below:
                return {"clip": key, "run": k, "at": runs[k]["start"], "total_arm": st.get("total_arm")}
    return None


def automatch(session: str, clip: str, write: bool = False, fps: float = 2.0, overwrite: bool = False,
              pending: list[dict] | None = None, windows: dict | None = None) -> dict:
    """Propose (and with write=True record) matches for one clip from its OSD timers and the session's logs.
    Existing matches of the clip are kept unless overwrite=True (a hand-refined offset beats an OSD one).
    `pending`: proposals of clips before this one in the same run (not written), taken like written ones."""
    s = sessions.load(session)
    path = sessions.clip_path(session, clip)
    name = next((v.get("name") or v["file"] for v in s["videos"] if v["file"] == path.name), path.name)
    rt = osd.read_timers(path, fps)
    rows = rt["rows"]
    varms = osd.video_arms(rt, fps)
    # arms matched to another clip (of any session) are taken: an arm is in one clip; and the clips before and
    # after this one bound its arms (#9: duration-only matches on the cumulative timer went anywhere in the log)
    others = planner._log_matches(s, path.name, pending or [])
    taken = {(b, a) for b, ms in others.items() for _, a in ms}
    bounds, fulls = {}, {}
    # a clip comes from one flash dump, so align against each log separately and keep the best alignment
    best = None
    for b in s["blackbox"]:
        idx = blackbox.index_bbl(b)
        # the arms this clip may hold: from the session plan when there is one (#26), else bounded by the other clips'
        # matches (#9), which a wrong pick before this clip cascades from
        lo, hi = planner.arm_window(name, others.get(idx["file"], []))
        if windows and idx["file"] in windows:
            # the plan's window, narrowed by what the clips before this one took in this run: the plan reaches one arm
            # into each neighbour (PLAN_SLACK), and an arm a clip before this one took bounds it as before
            plo, phi = windows[idx["file"]]
            lo = max(x for x in (lo, plo) if x is not None) if (lo is not None or plo is not None) else None
            hi = min(x for x in (hi, phi) if x is not None) if (hi is not None or phi is not None) else None
        bounds[idx["file"]] = (lo, hi)
        # a log whose last arm is cut off ended with the flash full (#9): once a STATS screen shows BLACKBOX 100 %,
        # no later run is in it, and the run at which it shows may only be that last arm
        vs = varms
        if idx["arms"] and idx["arms"][-1].get("truncated"):
            last = idx["arms"][-1]["index"]
            before = max((k for k, a in others.get(idx["file"], []) if planner.clip_order(k) and planner.clip_order(name) and k < name), default=None)
            f = full_point(s, name, before, fps, (rt, varms, path))
            if f:
                fulls[idx["file"]] = f
                if f["clip"] != name:
                    vs = [dict(v, no_log=True) for v in varms]
                else:
                    vs = [dict(v, no_log=True) if v["start"] > f["at"] else dict(v, only_arm=last) if k == f["run"] else v
                          for k, v in enumerate(varms)]
        boot_of = {i: k for k, bt in enumerate(idx.get("boots", [])) for i in bt["arms"]}
        cum, logs = {}, []
        for a in idx["arms"]:
            if a.get("frames") and a.get("motors_spun", True):
                k = boot_of.get(a["index"])
                if (idx["file"], a["index"]) not in taken and (lo is None or a["index"] > lo) and (hi is None or a["index"] < hi):
                    logs.append({"bbl": idx["file"], "index": a["index"], "length": a["length"], "vbat_start": a["vbat_start"],
                                 "cum_before": round(cum.get(k, 0.0), 1) if k is not None else None,
                                 "boot": k, "uptime_start": a.get("uptime_start")})
                if k is not None:
                    cum[k] = cum.get(k, 0.0) + a["length"]
        pairs = pairing.consistent_pairs(vs, logs, pairing.align(vs, logs))
        pairs = pairing.in_order(vs, logs, pairing.place_by_boot(vs, logs, pairs))
        # ties (a run the clip starts inside fits an arm of several logs) go to the log the session already uses
        in_use = sum(1 for m in s["matches"] if m["bbl"] == idx["file"])
        score = (len(pairs), -round(sum(abs(varms[vi]["length"] - logs[li]["length"]) for vi, li in pairs), 1), in_use)
        if best is None or score > best[0]:
            best = (score, pairs, logs, idx["file"])
    _, pairs, logs, bbl = best if best else ((0, 0), [], [], None)
    full = fulls.get(bbl)
    fits = pairing.fit_boots(varms, logs, pairs)
    duration = next((v.get("duration") for v in s["videos"] if v["file"] == path.name), None)
    matches = []
    for vi, li in pairs:
        v, l = varms[vi], logs[li]
        e = pairing._elapsed(v, l)
        offset = round(v["start"] - max(0.0, e), 2) if e is not None else v["start"]
        matches.append({"video": path.name, "bbl": l["bbl"], "arm": l["index"], "offset": offset,
                        "video_start": v["start"], "video_length": v["length"], "log_length": l["length"],
                        "note": f"osd automatch: timer run {v['length']} s vs log {l['length']} s, total before {v['total_before']} vs {l['cum_before']} ({v['readings']} readings)"
                                + ("" if abs(v["length"] - l["length"]) <= 2 else "; placed by the boot's offset, the run reads shorter than the arm")})
    real = [(varms[vi]["start"], logs[li]["index"]) for vi, li in pairs]
    for li, start in pairing.derive_unread(logs, pairs, fits, duration):
        l = logs[li]
        if any((t < start) != (i < l["index"]) for t, i in real):     # out of order with what was read
            continue
        if full and (full["clip"] != name or start > full["at"]):     # placed after the flash filled
            continue
        f = fits[l["boot"]]
        matches.append({"video": path.name, "bbl": l["bbl"], "arm": l["index"], "offset": start, "video_start": start,
                        "video_length": None, "log_length": l["length"], "derived": True,
                        "note": f"derived from the boot's fit (video = {f['a']:.5f} x uptime + {f['b']:.2f}, {f['n']} arms read), the arm's timer was not read"})
    matches.sort(key=lambda m: m["offset"])
    unmatched = [v for k, v in enumerate(varms) if k not in {vi for vi, _ in pairs}]
    if write:
        # the session as it is now, not as it was before the minutes of reading: a match set by hand meanwhile is
        # kept, and a log taken out meanwhile gets none (the write checks it again in the same statement, and never
        # attaches the log the way set_match does)
        now_s = sessions.load(session)
        have = {(m["bbl"], m["arm"]) for m in now_s["matches"] if m["video"] == path.name}
        attached = set(now_s["blackbox"])
        for m in matches:
            if m["bbl"] in attached and (overwrite or (m["bbl"], m["arm"]) not in have):
                m["written"] = sessions.store().add_match_if_attached(session, m["video"], m["bbl"], m["arm"], m["offset"], m["note"], replace=overwrite)
    return {"clip": path.name, "name": name, "layout": rt["layout"], "readings": len(rows), "boot_fits": fits,
            "bbl": bbl, "arm_window": bounds.get(bbl, (None, None)), "flash_full": full,
            "readable": sum(1 for r in rows if (r["top"] if not osd._layout(rt["layout"])["cumulative"] else r["bottom"]) is not None),
            "video_arms": varms, "matches": matches, "unmatched_video_arms": unmatched}
