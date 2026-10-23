"""A clip's arm runs (read off its OSD) paired with a log's arms: by length and the OSD's total first, then along a
power cycle's uptime (a boot's fit, video = a * uptime + b: the two clocks differ by a few tenths of a percent), and
the arms a clip shows no run for derived along that line.
"""


# The two clocks (DVR and FC) differ by a few tenths of a percent (#4: 0.3 %). A boot's line beyond this is pairs
# that are not one boot in one clip (duration-only matches on the cumulative timer, #9), not a clock.
MAX_CLOCK = 0.01


def _elapsed(v: dict, l: dict) -> float | None:
    """For a run the clip starts inside: how far into log arm `l` the clip begins, from the totals (None if unknown)."""
    if v.get("partial_start") and v.get("total_before") is not None and l.get("cum_before") is not None:
        return v["total_before"] - l["cum_before"]
    return None


def _may(v: dict, l: dict) -> bool:
    """Whether run v may be log arm l at all: not after the flash filled (no_log), and the run at which it filled
    only the log's cut-off last arm (only_arm)."""
    return not v.get("no_log") and v.get("only_arm", l["index"]) == l["index"]


def align(video_arms: list[dict], log_arms: list[dict], tol: float = 2.0, w_total: float = 0.3) -> list[tuple[int, int]]:
    """Order-preserving alignment of detected arms to log arms. Cost of a pair = |length difference| +
    w_total * |OSD total-before - log cumulative length in the boot| (when both known); skipping either side costs tol.
    A run the clip starts inside (partial_start) is compared against what is left of the log arm after the point
    the totals say the clip begins. Both lists are in time order. Returns (video index, log index) pairs."""
    n, m = len(video_arms), len(log_arms)
    INF = float("inf")
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            c = cost[i][j]
            if c == INF:
                continue
            if i < n and j < m:
                v, l = video_arms[i], log_arms[j]
                e = _elapsed(v, l)
                if e is not None:
                    d = abs(v["length"] - (l["length"] - e)) if e >= -1 else INF
                else:
                    d = abs(v["length"] - l["length"])
                    if v.get("total_before") is not None and l.get("cum_before") is not None:
                        d += w_total * abs(v["total_before"] - l["cum_before"])
                if d <= 2 * tol and _may(v, l) and c + d < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = c + d, ("m", i, j)
            if i < n and c + tol < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = c + tol, ("v", i, j)
            if j < m and c + tol < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = c + tol, ("l", i, j)
    pairs, i, j = [], n, m
    while back[i][j]:
        kind, pi, pj = back[i][j]
        if kind == "m":
            pairs.append((pi, pj))
        i, j = pi, pj
    return pairs[::-1]


def consistent_pairs(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop pairs that break the one thing a boot guarantees: the log's time field is the FC uptime, so within a
    boot `video start - uptime start` is the boot's offset, and it can only go DOWN along the boot (the compressor
    cuts video out, never adds any). A stray run that pairs with a short arm far from where the boot puts it
    makes that offset jump up; of two such pairs the one with fewer readings goes."""
    kept: list[tuple[int, int]] = []
    last: dict = {}                                   # boot -> (boot offset, index into kept)
    for vi, li in pairs:
        v, l = video_arms[vi], log_arms[li]
        if l.get("boot") is None or l.get("uptime_start") is None:
            kept.append((vi, li))
            continue
        off = v["start"] - l["uptime_start"]
        prev = last.get(l["boot"])
        if prev and off > prev[0] + 2.0:          # the starts read off a whole-second timer jitter by about a second
            pv = video_arms[kept[prev[1]][0]]
            if v["readings"] <= pv["readings"]:
                continue                              # the newcomer is the stray
            kept.pop(prev[1])                         # the earlier pair was the stray
            last = {b: (o, k if k < prev[1] else k - 1) for b, (o, k) in last.items() if k != prev[1]}
        kept.append((vi, li))
        last[l["boot"]] = (off, len(kept) - 1)
    return kept


def fit_boots(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]]) -> dict:
    """Per boot, video time as a line in the FC uptime: video = a * uptime + b, from the pairs. The two clocks
    differ by a few tenths of a percent (#4: about 0.3 % on the Cobra SD against this FC), half a second over a
    four-minute clip, so one offset per boot is not enough. Least squares when three or more pairs span a minute;
    the median offset with a = 1 otherwise. {boot: {a, b, n}}, with rejected=True when no clock explains the pairs
    (a line beyond MAX_CLOCK, or offsets further apart than the timer jitter and MAX_CLOCK allow): such a fit
    places and derives nothing."""
    pts: dict = {}
    for vi, li in pairs:
        l = log_arms[li]
        if l.get("boot") is not None and l.get("uptime_start") is not None:
            pts.setdefault(l["boot"], []).append((l["uptime_start"], video_arms[vi]["start"]))
    out = {}
    for b, xy in pts.items():
        xs, ys = [x for x, _ in xy], [y for _, y in xy]
        if len(xy) >= 3 and max(xs) - min(xs) >= 60:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            a = sum((x - mx) * (y - my) for x, y in xy) / sxx
            out[b] = {"a": round(a, 5), "b": round(my - a * mx, 3), "n": len(xy)}
            if abs(a - 1) > MAX_CLOCK:
                out[b]["rejected"] = True
        else:
            offs = sorted(y - x for x, y in xy)
            out[b] = {"a": 1.0, "b": round(offs[len(offs) // 2], 3), "n": len(xy)}
            if offs[-1] - offs[0] > 2.0 + MAX_CLOCK * (max(xs) - min(xs)):     # whole-second timers jitter by a second
                out[b]["rejected"] = True
    return out


def _at(fit: dict, uptime: float) -> float:
    return fit["a"] * uptime + fit["b"]


def place_by_boot(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]], tol: float = 2.0) -> list[tuple[int, int]]:
    """Second pass: the pairs give each boot's line (fit_boots), which puts every other arm of the boot at a known
    place in the video. An unmatched run whose start sits within `tol` of exactly one unmatched arm's place is that
    arm, however long the run reads: a run's length is only what the timer showed, and the end of an arm is often
    unreadable (a crash, a broken-up picture, the STATS screen)."""
    fits = {b: f for b, f in fit_boots(video_arms, log_arms, pairs).items() if not f.get("rejected")}
    used_v, used_l = {vi for vi, _ in pairs}, {li for _, li in pairs}
    out = list(pairs)
    for vi, v in enumerate(video_arms):
        if vi in used_v or v.get("partial_start"):
            continue
        hits = [li for li, l in enumerate(log_arms) if li not in used_l and l.get("boot") in fits and _may(v, l)
                and abs(v["start"] - _at(fits[l["boot"]], l["uptime_start"])) <= tol and v["length"] <= l["length"] + tol]
        if len(hits) == 1:
            out.append((vi, hits[0]))
            used_v.add(vi); used_l.add(hits[0])
    out.sort()
    return out


def derive_unread(log_arms: list[dict], pairs: list[tuple[int, int]], fits: dict, duration: float | None) -> list[tuple[int, float]]:
    """Arms of a fitted boot that no run was read for (too short for the reader, or unreadable), placed by the
    boot's line when they fall inside the clip: [(log index, video start)]. Marked as derived in the match note."""
    used_l = {li for _, li in pairs}
    out = []
    for li, l in enumerate(log_arms):
        if li in used_l or l.get("boot") not in fits or l.get("uptime_start") is None:
            continue
        f = fits[l["boot"]]
        if f["n"] < 2 or f.get("rejected"):
            continue
        start = _at(f, l["uptime_start"])
        if start < -0.5 or (duration is not None and start + l["length"] > duration + 0.5):
            continue
        out.append((li, round(start, 2)))
    return out


def in_order(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The pairs that keep a clip's arms in the log's order: a clip is one stretch of time, so a later run is a
    later arm. The alignment preserves order, but placing by a boot's line and partial runs need not. Keeps the
    order-preserving subset with the most readings (a longest increasing subsequence, weighted)."""
    ps = sorted(pairs, key=lambda p: (video_arms[p[0]]["start"], p[1]))
    best, prev = [], []
    for k, (vi, li) in enumerate(ps):
        w = video_arms[vi].get("readings", 1)
        cands = [(best[j], j) for j in range(k) if log_arms[ps[j][1]]["index"] < log_arms[li]["index"]]
        b, j = max(cands, default=(0, None))
        best.append(b + w)
        prev.append(j)
    if not ps:
        return []
    k = max(range(len(ps)), key=lambda i: best[i])
    out = []
    while k is not None:
        out.append(ps[k])
        k = prev[k]
    return sorted(out)
