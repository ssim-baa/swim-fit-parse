#!/usr/bin/env python3
"""fit_parse.py — deterministic extractor for Garmin lap-swimming .FIT files.

Emits a compact text report for LLM session context:
  1. session header
  2. lap table
  3. block aggregation as `block_metrics` JSON (Workout_L3_Swim_Sessions field)

Design constraints (Workout Schema Document v3.0, section A):
  - Deterministic extraction only. No judgement, no grading, no personal
    constants (age, weight, base times, gate thresholds). Verdicts live in
    Notion L2 `rule` records.
  - `record` (1Hz) messages are consumed for hrr_60 only and never printed
    (operating rule 11: ~2,700 samples/session would flood context).
  - No pool-length normalisation. Course-dependent metrics are reported raw;
    splitting by course is the consumer's job (operating rule 14).

Usage:
    pip install swim-fit-parse
    python -m swim_fit_parse <FIT path...>

Multiple paths are assembled into one session (Stage 0) when they are
adjacent in time; otherwise each group is reported separately.
"""

import bisect
import json
import statistics
import sys
from collections import Counter
from datetime import timedelta

try:
    from fitparse import FitFile
except ImportError:  # pragma: no cover
    sys.exit("fitparse is required:  pip install fitparse")

SCHEMA_VERSION = "3.3"
PARSER_TAG = "v3.3"

# block_metrics payload marker — see the emit site for why this is mandatory.
BM_PREFIX = "bm1|"

KST = timedelta(hours=9)

# Stage 0: files whose start times fall within this window belong to one session.
SESSION_GAP_LIMIT_S = 3 * 3600

# Detection thresholds (Schema v3.0 검출 규칙, 2026-07-31 정정).
#
# The PRIMARY index is total_strokes, not time: pausing mid-length corrupts
# duration but never stroke count, and that is exactly where a time-based
# test fails. Time ratio is retained as a secondary/diagnostic figure.
F1_STROKE_LOW = 0.65      # strokes below this fraction of group median -> candidate
F1_RESTORE_LOW = 0.75     # adjacent candidates summed must land in this band
F1_RESTORE_HIGH = 1.25    # to be confirmed as a split artifact

F2_STROKE_HIGH = 1.6      # missed turn: strokes AND duration both >= 1.6x
F2_DURATION_HIGH = 1.6

F3_DURATION_HIGH = 1.4    # time contamination: duration >= 1.4x ...
F3_STROKE_LOW = 0.75      # ... while strokes stay normal (0.75~1.25)
F3_STROKE_HIGH = 1.25     # -> distance is CORRECT, do not merge

# Group key = (file, wkt_step_index); free sessions use (file, swim_stroke).
# wkt_step_index is unique only within a file, so a multi-file session must not
# pool across sources. Below the minimum size the median is not a population
# statistic — report "평가 불가" rather than staying silent.
F_MIN_GROUP = 5

SWOLF_TOLERANCE = 1.0          # |garmin - derived| > 1 -> cross-check flag
HRR_MIN_REST_S = 60.0          # operating rule 13

# A lap crediting distance with zero recorded strokes in under this many
# seconds is a watch artifact, excluded from aggregates rather than silently
# averaged in.
PHANTOM_LAP_MAX_S = 10.0

STROKE_KO = {
    "freestyle": "자유",
    "backstroke": "배영",
    "breaststroke": "평영",
    "butterfly": "접영",
    "drill": "드릴",
    "im": "혼영",
    "mixed": "혼합",
}


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def _fields(msg):
    return {f.name: f.value for f in msg}


def read_fit(path):
    """Pull the messages we need out of one .FIT file."""
    ff = FitFile(path)
    out = {
        "path": path,
        "session": {},
        "laps": [],
        "lengths": [],
        "steps": {},
        "wkt_name": None,
        "hr_series": [],   # (datetime, hr) — consumed by hrr_60, never printed
    }
    for msg in ff.get_messages(("session", "lap", "length", "workout",
                                "workout_step", "record")):
        d = _fields(msg)
        if msg.name == "session":
            out["session"] = d
        elif msg.name == "lap":
            out["laps"].append(d)
        elif msg.name == "length":
            out["lengths"].append(d)
        elif msg.name == "workout":
            out["wkt_name"] = d.get("wkt_name")
        elif msg.name == "workout_step":
            out["steps"][d.get("message_index")] = d
        elif msg.name == "record":
            hr, ts = d.get("heart_rate"), d.get("timestamp")
            if hr is not None and ts is not None:
                out["hr_series"].append((ts, hr))
    out["laps"].sort(key=lambda l: (l.get("start_time") or 0))
    out["lengths"].sort(key=lambda l: (l.get("message_index") or 0))
    out["hr_series"].sort()
    return out


def group_sessions(files):
    """Stage 0 — assemble multi-file recordings into single sessions."""
    files = [f for f in files if f["session"].get("start_time")]
    files.sort(key=lambda f: f["session"]["start_time"])
    groups, cur = [], []
    for f in files:
        if not cur:
            cur = [f]
            continue
        prev = cur[-1]["session"]
        prev_end = prev["start_time"] + timedelta(
            seconds=prev.get("total_elapsed_time") or 0)
        if (f["session"]["start_time"] - prev_end).total_seconds() <= SESSION_GAP_LIMIT_S:
            cur.append(f)
        else:
            groups.append(cur)
            cur = [f]
    if cur:
        groups.append(cur)
    return groups


# --------------------------------------------------------------------------
# derived metrics — formulas are canonical per Schema v3.0 delta (1)
# --------------------------------------------------------------------------

def pace_per_100m(seconds, distance_m):
    if not distance_m:
        return None
    return seconds * 100.0 / distance_m


def derived_swolf(seconds, cycles, lengths):
    """SWOLF definition: time/length + cycles/length."""
    if not lengths:
        return None
    return seconds / lengths + cycles / lengths


def hr_efficiency(pace100, avg_hr):
    """Higher is better. Unchanged from v2.9 (course-independent of pool length
    in form, though the value itself is course-dependent)."""
    if not pace100 or not avg_hr:
        return None
    return round(600000.0 / (pace100 * avg_hr) * 10) / 10


def coefficient_of_variation(values):
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    if not mean:
        return None
    return round(statistics.stdev(values) / mean * 100, 1)


def fade_pct(values):
    """Second-half mean vs first-half mean, percent. Positive = slowed down.

    Uses every rep, not just the endpoints: a first/last comparison discards
    the interior (n=6 -> 4 values ignored) and lets a single fast opener or
    slow closer dominate. With an odd n the middle rep is excluded from both
    halves. Below n=4 there is no half to average, so fade is null.
    """
    n = len(values)
    if n < 4:
        return None
    half = n // 2
    first = statistics.fmean(values[:half])
    second = statistics.fmean(values[-half:])
    if not first:
        return None
    return round((second / first - 1) * 100, 1)


# --------------------------------------------------------------------------
# lap assembly
# --------------------------------------------------------------------------

def build_laps(group):
    """Flatten laps across the assembled files, attaching length-derived data."""
    laps = []
    for f in group:
        lengths = f["lengths"]
        for d in f["laps"]:
            dist = d.get("total_distance") or 0.0
            secs = d.get("total_timer_time") or d.get("total_elapsed_time") or 0.0
            active = d.get("num_active_lengths") or 0
            cycles = d.get("total_cycles") or 0

            # the length messages this lap is composed of — F1 operates on
            # these, since a lap is a sum that dilutes a single bad length
            first = d.get("first_length_index")
            n_len = d.get("num_lengths")
            window = []
            if first is not None and n_len:
                # `or -1` here silently dropped message_index 0: index 0 is
                # falsy, so the FIRST length of every session fell out of the
                # detection population. Lap aggregates read the lap message
                # directly and were unaffected, which is why this stayed
                # invisible until the design anchor made group totals matter.
                window = [x for x in lengths
                          if x.get("message_index") is not None
                          and first <= x["message_index"] < first + n_len]
            lap_lengths = [{
                "idx": x.get("message_index"),
                "secs": x.get("total_timer_time") or x.get("total_elapsed_time"),
                "strokes": x.get("total_strokes"),
                "stroke": x.get("swim_stroke"),
                "type": x.get("length_type"),
                "step_idx": d.get("wkt_step_index"),
                # wkt_step_index is only unique WITHIN a file. Two assembled
                # files each carry their own step 2, so the group key must be
                # scoped by source or the medians pool unrelated efforts.
                "src": f["path"],
                "lap_n": None,
            } for x in window]

            # stroke: lap field, else majority vote over its active lengths
            stroke = d.get("swim_stroke")
            if stroke is None:
                votes = Counter(x["stroke"] for x in lap_lengths
                                if x["type"] == "active" and x["stroke"])
                if votes:
                    stroke = votes.most_common(1)[0][0]

            laps.append({
                "lengths": lap_lengths,
                "src": f["path"],
                "start": d.get("start_time"),
                "step_idx": d.get("wkt_step_index"),
                "dist": dist,
                "secs": secs,
                "active_lengths": active,
                "cycles": cycles,
                "avg_hr": d.get("avg_heart_rate"),
                "max_hr": d.get("max_heart_rate"),
                "stroke": stroke,
                "garmin_swolf": d.get("avg_swolf"),
                "is_active": dist > 0,
            })
    laps.sort(key=lambda l: (l["start"] or 0))
    for i, lap in enumerate(laps, 1):
        lap["n"] = i
        for ln in lap["lengths"]:
            ln["lap_n"] = i
        # Phantom: distance credited with no strokes in near-zero time. Garmin
        # emits these when the watch mis-detects a wall touch. Keeping them
        # would corrupt every block aggregate they land in.
        lap["phantom"] = bool(
            lap["is_active"] and lap["cycles"] == 0
            and lap["secs"] < PHANTOM_LAP_MAX_S)
        lap["pace100"] = pace_per_100m(lap["secs"], lap["dist"])
        lap["dswolf"] = derived_swolf(lap["secs"], lap["cycles"],
                                      lap["active_lengths"])
        lap["dps"] = (lap["dist"] / lap["cycles"]) if lap["cycles"] else None
    return laps


def rest_after(laps, idx):
    """Seconds of non-active time between active lap idx and the next active lap."""
    total = 0.0
    for lap in laps[idx + 1:]:
        if lap["is_active"]:
            return total
        total += lap["secs"]
    return total if total else None


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------

def signature(dist_m, stroke, n_reps, rest_s):
    """Normal form per Schema v3.0 delta (9): [dist][stroke] xN @r[rest].
    Stroke is mandatory — the string is a comparison key, so drift splits
    the population. Distance is rep-distance, hence course-independent."""
    ko = STROKE_KO.get(stroke, stroke or "?")
    sig = f"{int(round(dist_m))}{ko}"
    if n_reps > 1:
        sig += f"×{n_reps}"
    if rest_s:
        sig += f"@r{int(rest_s // 60)}:{int(rest_s % 60):02d}"
    return sig


def group_blocks(laps, has_workout):
    """Map laps to blocks. wkt_step_index is deterministic when a workout is
    present (operating rule 8); free sessions fall back to run-length grouping
    of (distance, stroke)."""
    active = [(i, l) for i, l in enumerate(laps)
              if l["is_active"] and not l["phantom"]]
    if not active:
        return []

    # Key includes rep distance and stroke, not just step index: one workout
    # step can be reused by the watch for different content (observed 07-25,
    # where step 2 covers both a drill and a freestyle rep), and merging those
    # would pool physically different efforts under one signature.
    merged, order = {}, []
    for i, lap in active:
        kind = "step" if (has_workout and lap["step_idx"] is not None) else (
            "extra" if has_workout else "free")
        key = (kind, lap["step_idx"] if kind == "step" else None,
               round(lap["dist"]), lap["stroke"])
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append((i, lap))
    # order is first-appearance chronological; repeats rejoin their own block
    return [(k, merged[k]) for k in order]


def hrr_60(laps, idx, hr_series):
    """Operating rule 13: only when the following rest is >=60s AND no active
    lap starts within 60s. Otherwise null — never 0.

    Applies to the BLOCK's final rep (see build_block_metrics): evaluating
    every rep and taking the max lets one qualifying rep fill a block whose
    recovery window never existed.
    """
    # The last active lap of the session has no following rest window — the
    # trailing idle is session teardown, not recovery between efforts.
    if not any(l["is_active"] for l in laps[idx + 1:]):
        return None
    rest = rest_after(laps, idx)
    if rest is None or rest < HRR_MIN_REST_S:
        return None
    lap = laps[idx]
    end = (lap["start"] or None)
    if end is None or not hr_series:
        return None
    end = end + timedelta(seconds=lap["secs"])
    times = [t for t, _ in hr_series]

    def hr_at(when):
        pos = bisect.bisect_left(times, when)
        best, gap = None, None
        for p in (pos - 1, pos):
            if 0 <= p < len(hr_series):
                g = abs((hr_series[p][0] - when).total_seconds())
                if gap is None or g < gap:
                    best, gap = hr_series[p][1], g
        return best if (gap is not None and gap <= 5) else None

    start_hr = hr_at(end)
    end_hr = hr_at(end + timedelta(seconds=60))
    if start_hr is None or end_hr is None:
        return None
    return start_hr - end_hr


def build_block_metrics(laps, has_workout, hr_series, steps):
    out = []
    for key, entries in group_blocks(laps, has_workout):
        reps = [lap for _, lap in entries]
        idxs = [i for i, _ in entries]
        paces = [l["pace100"] for l in reps if l["pace100"]]
        dists = [l["dist"] for l in reps]
        hrs = [l["avg_hr"] for l in reps if l["avg_hr"]]
        cycles = sum(l["cycles"] for l in reps)
        act_len = sum(l["active_lengths"] for l in reps)
        dist_total = sum(dists)

        rests = [r for r in (rest_after(laps, i) for i in idxs[:-1]) if r]
        rest_med = statistics.median(rests) if rests else None
        # signature rest is quantised to 5s to keep the key stable
        sig_rest = round(rest_med / 5) * 5 if rest_med else None

        step_idx = key[1] if key[0] == "step" else None
        # Operating rule 13 is a BLOCK condition: evaluate the recovery window
        # that follows the block's final rep, not every rep with a max().
        block_hrr = hrr_60(laps, idxs[-1], hr_series)
        # The window the gate actually evaluated — distinct from
        # rest_median_s (which is rest BETWEEN reps). Emitting it makes an
        # hrr_60 value auditable instead of having to trust the gate.
        hrr_rest = rest_after(laps, idxs[-1])
        if not any(l["is_active"] for l in laps[idxs[-1] + 1:]):
            hrr_rest = None

        # Garmin does not count strokes on drill lengths, so total_cycles==0
        # means NOT MEASURED, not "zero strokes". Emitting 0 (or a swolf that
        # is really just lap time) would poison downstream averages.
        measured = cycles > 0
        swolfs = [l["dswolf"] for l in reps if l["dswolf"]] if measured else []

        out.append({
            "signature": signature(statistics.median(dists), reps[0]["stroke"],
                                   len(reps), sig_rest),
            "wkt_step_index": step_idx,
            "source": key[0],
            "n": len(reps),
            "laps": [l["n"] for l in reps],
            "distance_m": round(dist_total),
            "pace_median": round(statistics.median(paces), 1) if paces else None,
            "cv": coefficient_of_variation(paces),
            "fade": fade_pct(paces),
            "dps_m": round(dist_total / cycles, 2) if measured else None,
            "cycles_per_length": (round(cycles / act_len, 2)
                                  if measured and act_len else None),
            "swolf": round(statistics.median(swolfs), 1) if swolfs else None,
            "spi": round(dist_total / cycles / (statistics.median(paces) / 100), 3)
                if measured and paces and statistics.median(paces) else None,
            "rest_median_s": round(rest_med) if rest_med else None,
            "avg_hr": round(statistics.fmean(hrs)) if hrs else None,
            "peak_hr": max((l["max_hr"] for l in reps if l["max_hr"]), default=None),
            "hrr_60": block_hrr,
            "hrr_rest_s": round(hrr_rest) if hrr_rest else None,
        })
    return out


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------

def declared_stroke(steps, step_idx):
    """The stroke a workout step programs, or None.

    Garmin stores it in target_stroke_type, not target_value (confirmed
    against the 07-18 workout_step dump). The value can be a raw enum the
    watch never resolved (255 on the 09-03 cooldown); it is returned as-is
    so F4 keeps reporting the mismatch, and it simply never matches a
    measured stroke name when used to pick a host.
    """
    step = steps.get(step_idx) if step_idx is not None else None
    if not step or step.get("target_type") != "swim_stroke":
        return None
    return step.get("target_stroke_type")


def collect_flags(laps, lengths, steps, blocks, pool_length=None):
    """v3.0 detection rules F1~F4 (Schema: 병합 서브루틴 절).

    Detection descends from lap to length. In FIT each active length is exactly
    `pool_length`, so distance is a COUNT, not an estimate — the only failure
    mode is a missed turn merging or mis-creating lengths. Lap-level pace
    heuristics are prohibited: a lap is a sum of lengths, which dilutes the
    signal.

    Detection only. Cause attribution belongs to P1 문진; merging happens via
    --merge after USER GATE.
    """
    flags = []

    # --- repetition counts from the workout program ------------------------
    # FIT puts the count on a SEPARATE repeat step, not on the swim step the
    # laps point at: duration_step=6 with repeat_steps=4 means "repeat steps
    # 6..7, four times". Measured against 09-03 — `repeat_value`, which F5
    # read until v3.4, does not exist in these files at all.
    repeats = {}
    for st in steps.values():
        if st.get("duration_type") != "repeat_until_steps_cmplt":
            continue
        start = st.get("duration_step")
        cnt = st.get("repeat_steps")
        here = st.get("message_index")
        if not all(isinstance(v, int) for v in (start, cnt, here)) or cnt < 1:
            continue
        for i in range(start, here):        # nested repeats multiply
            repeats[i] = repeats.get(i, 1) * cnt

    # --- group the active lengths -----------------------------------------
    # Key = (file, wkt_step_index), or (file, swim_stroke) for free sessions.
    groups = {}
    for ln in lengths:
        if ln["type"] != "active":
            continue
        key = ("step", ln["src"], ln["step_idx"]) if ln["step_idx"] is not None \
            else ("stroke", ln["src"], ln["stroke"])
        groups.setdefault(key, []).append(ln)

    for key, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        label = (f"wkt_step_index={key[2]}" if key[0] == "step"
                 else f"영법={STROKE_KO.get(key[2], key[2])}")
        strokes = [l["strokes"] for l in group if l["strokes"]]
        times = [l["secs"] for l in group if l["secs"]]

        # How many lengths the program CALLS FOR: distance per rep / pool
        # length, times the repeat count. Design, not observation — it holds
        # even when the observed population is polluted.
        designed_per_lap = None
        designed_total = None
        if key[0] == "step" and pool_length:
            st = steps.get(key[2])
            dd = st.get("duration_distance") if st else None
            if dd:
                designed_per_lap = dd / pool_length
                designed_total = designed_per_lap * repeats.get(key[2], 1)

        # Undersized groups are reported, not silently skipped: silence would
        # read as "checked and clean".
        if len(strokes) < F_MIN_GROUP or len(times) < F_MIN_GROUP:
            flags.append({
                "type": "--",
                "title": "평가 불가",
                "length_idx": None,
                "lap_n": None,
                "detail": (f"그룹 {label} n={len(group)} "
                           f"(<{F_MIN_GROUP}) — F1~F3 평가 제외"),
                "suggest": "모집단 부족으로 중앙값 기준 성립 불가",
            })
            continue

        # The median collapses once fabricated lengths are a large share of
        # the population (09-03 step6: median 10 against a real band near 20)
        # and that fails the detectors BOTH ways — F1 misses real fragments
        # while F2 fires on normal ones. Total strokes, by contrast, are
        # INVARIANT under a length boundary error: splitting or merging
        # lengths does not change how many times the arm turned over. So
        # total / designed count is the per-length truth. Applied only when
        # the design is known AND the group is over-populated (E > 0);
        # otherwise the median is more robust against a genuine outlier.
        #
        # Both baselines move together on purpose. Anchoring strokes alone
        # would drop 09-03 #19/#25 out of the F2 band straight into F3's
        # normal-stroke band while their time ratio stayed inflated —
        # trading a false F2 for a false F3. The cost is that a real pause
        # (F3) lifts the time anchor, but it is divided across
        # designed_total lengths while the offending length keeps the whole
        # excess, so its ratio still rises.
        s_base = statistics.median(strokes)
        t_base = statistics.median(times)
        base_note = "중앙값"
        if designed_total and len(group) > designed_total:
            s_base = sum(strokes) / designed_total
            t_base = sum(times) / designed_total
            base_note = (f"설계앵커 {sum(strokes)}÷{designed_total:g}, "
                         f"E={len(group) - designed_total:+g}")
        if not s_base or not t_base:
            continue
        gnote = (f"| 그룹 {label}, n={len(strokes)}, "
                 f"기준선 {s_base:.1f}({base_note})")

        # per-length ratios; strokes primary, duration secondary
        for ln in group:
            ln["_sr"] = (ln["strokes"] / s_base) if ln["strokes"] else None
            ln["_tr"] = (ln["secs"] / t_base) if ln["secs"] else None

        # --- F1 split artifact: strokes far below median ------------------
        # Confirmed when adjacent candidates' strokes sum back into 0.75~1.25:
        # the watch fabricated extra lengths out of one real length.
        candidates = [ln for ln in group
                      if ln["_sr"] is not None and ln["_sr"] < F1_STROKE_LOW]
        cand_idx = {ln["idx"] for ln in candidates}
        consumed = set()

        # --- C1: design comparison (takes precedence over C2) --------------
        # E = observed active lengths - designed lengths. When a lap carries
        # exactly E surplus candidates, those candidates ARE the surplus and
        # the whole set can be dropped. This covers the single-fragment case
        # that C2 (adjacent-sum restoration) structurally cannot: a fragment
        # that was ADDED was never split, so nothing sums back.
        c1_laps = {}
        if designed_per_lap:
            designed = designed_per_lap
            per_lap = {}
            for ln in candidates:
                per_lap.setdefault(ln["lap_n"], []).append(ln)
            for lap_n, cands in per_lap.items():
                owner = next((l for l in laps if l["n"] == lap_n), None)
                # A phantom lap is excluded from every aggregate; letting
                # it ground a correction proposal would contradict that.
                if not owner or owner["phantom"]:
                    continue
                excess = owner["active_lengths"] - designed
                if excess > 0 and len(cands) == excess:
                    c1_laps[lap_n] = {
                        "excess": int(excess),
                        "designed": int(designed),
                        "observed": owner["active_lengths"],
                        "cands": sorted(x["idx"] for x in cands),
                        "dd": designed_per_lap * pool_length,
                    }
        for ln in candidates:
            if ln["idx"] in consumed:
                continue
            # greedily absorb following adjacent candidates
            run = [ln]
            nxt = ln["idx"] + 1
            while nxt in cand_idx:
                run.append(next(x for x in candidates if x["idx"] == nxt))
                nxt += 1
            total = sum(x["strokes"] for x in run)
            restored = total / s_base
            c1 = c1_laps.get(run[0]["lap_n"])
            # C1 wins when it covers this run: it is the superset test.
            c1_covers = bool(c1 and set(x["idx"] for x in run) <= set(c1["cands"]))
            c2_ok = (len(run) > 1
                     and F1_RESTORE_LOW <= restored <= F1_RESTORE_HIGH)
            confirmed = c1_covers or c2_ok
            test_used = "C1" if c1_covers else ("C2" if c2_ok else "C3")
            for x in run:
                consumed.add(x["idx"])
            idxs = "+".join("#%s" % x["idx"] for x in run)
            laps_involved = sorted({x["lap_n"] for x in run if x["lap_n"]})
            stroke_terms = "+".join(str(x["strokes"]) for x in run)
            indiv = ", ".join("%.2f" % x["_sr"] for x in run)
            time_terms = ", ".join(
                ("%.2f" % x["_tr"]) if x["_tr"] else "-" for x in run)
            lap_list = "+".join(str(n) for n in laps_involved)

            # Proposal: drop all but one of the fabricated run. The survivor
            # carries the real length; the rest are the watch's invention.
            proposal = None
            design = None
            if c1_covers:
                # the surplus candidates themselves are what to drop
                drop_idx = list(c1["cands"])
                proposal = "--drop-lengths " + ",".join(
                    str(d) for d in drop_idx)
                design = (
                    f"C1 설계 대조 — 실측 {c1['observed']} length vs 설계 "
                    f"{c1['dd']:.0f}m ÷ {pool_length:.0f}m = {c1['designed']} "
                    f"length, 초과 E={c1['excess']} = 후보 수 "
                    f"{len(c1['cands'])} 일치 ✅")
            elif c2_ok:
                # Which fragment survives is a stroke verdict, not
                # bookkeeping: apply_drops folds the others into it and the
                # lap takes its stroke as the label, which then decides
                # block grouping. Prefer the fragment matching what the step
                # programmed; fall back to the leading one. A wrong pick
                # still surfaces as F4, so the user keeps the chance to flip
                # the argument at the GATE.
                declared = (declared_stroke(steps, key[2])
                            if key[0] == "step" else None)
                host = run[0]
                if declared:
                    host = next((x for x in run
                                 if x["stroke"] == declared), run[0])
                drop_idx = [x["idx"] for x in run if x["idx"] != host["idx"]]
                proposal = "--drop-lengths " + ",".join(
                    str(d) for d in drop_idx)
                # Second evidence line: does the corrected lap distance match
                # the programmed step distance? Deterministic when present.
                owner = next((l for l in laps if l["n"] == run[0]["lap_n"]), None)
                step = steps.get(key[2]) if key[0] == "step" else None
                dd = step.get("duration_distance") if step else None
                if owner and dd and pool_length:
                    remaining = owner["active_lengths"] - len(drop_idx)
                    corrected = remaining * pool_length
                    design = (
                        f"설계값 {dd:.0f}m vs 교정 후 {remaining} length × "
                        f"{pool_length:.0f}m = {corrected:.0f}m — "
                        + ("일치 ✅" if abs(corrected - dd) < 1
                           else f"불일치 ⚠ (차 {corrected - dd:+.0f}m)"))
                elif key[0] != "step":
                    design = ("설계값 없음(free 세션) — 스트로크 복원 단독 "
                              "근거이므로 근거 약함, 육안 확인 권장")
                else:
                    design = "설계값 미보유 — 스트로크 복원 단독 근거"
            else:
                # C3 — neither test fires. Data cannot settle it; asking the
                # user is P1 문진's job, not a heuristic's.
                if key[0] != "step":
                    design = ("C3 미확정 — free 세션이라 설계값 부재, "
                              "인접 합산 복원도 미성립. **문진 대상**")
                else:
                    design = ("C3 미확정 — 설계 대조·합산 복원 모두 미성립. "
                              "**문진 대상**")
            flags.append({
                "type": "F1",
                "title": ("분할 아티팩트" + (f" [{test_used} 확정]" if confirmed
                                          else " 후보 [C3 미확정]")),
                "length_idx": run[0]["idx"],
                "lap_n": run[0]["lap_n"],
                "detail": (
                    f"length {idxs} strokes {stroke_terms}={total} / "
                    f"기준선 {s_base:.1f} = "
                    f"{'합산 ' if len(run) > 1 else ''}비율 {restored:.2f}"
                    + (f" (개별 {indiv})" if len(run) > 1 else "")
                    + f" | 시간 비율 {time_terms} (보조지표) "
                    + gnote),
                "suggest": (
                    f"분할 확정 — 랩 {lap_list} 거리 과대 기록 상태"
                    if confirmed else
                    "스트로크 과소 — 합산 복원 미확인. 단독 이상 여부 확인 필요"),
                "proposal": proposal,
                "design": design,
            })

        # --- F2 missed turn: strokes AND duration both high ---------------
        for ln in group:
            if ln["_sr"] is None or ln["_tr"] is None:
                continue
            if ln["_sr"] >= F2_STROKE_HIGH and ln["_tr"] >= F2_DURATION_HIGH:
                flags.append({
                    "type": "F2",
                    "title": "턴 미검출",
                    "length_idx": ln["idx"],
                    "lap_n": ln["lap_n"],
                    "detail": (f"length#{ln['idx']} strokes {ln['strokes']} "
                               f"= 비율 {ln['_sr']:.2f} (≥{F2_STROKE_HIGH}) | "
                               f"시간 {ln['secs']:.1f}초 = 비율 {ln['_tr']:.2f} "
                               f"(≥{F2_DURATION_HIGH}) " + gnote),
                    "suggest": "length 2개가 1개로 기록 — 거리 과소 기록 상태. 분할 검토",
                })

        # --- F3 time contamination: duration high, strokes normal ---------
        # Distance is CORRECT here. Never a merge candidate — exclude from
        # pace/SWOLF only.
        for ln in group:
            if ln["_sr"] is None or ln["_tr"] is None:
                continue
            if (ln["_tr"] >= F3_DURATION_HIGH
                    and F3_STROKE_LOW <= ln["_sr"] <= F3_STROKE_HIGH):
                flags.append({
                    "type": "F3",
                    "title": "시간 오염·거리 정상",
                    "length_idx": ln["idx"],
                    "lap_n": ln["lap_n"],
                    "detail": (f"length#{ln['idx']} 시간 {ln['secs']:.1f}초 "
                               f"= 비율 {ln['_tr']:.2f} (≥{F3_DURATION_HIGH}) "
                               f"이나 strokes {ln['strokes']} = 비율 "
                               f"{ln['_sr']:.2f} (정상 {F3_STROKE_LOW}~"
                               f"{F3_STROKE_HIGH}) " + gnote),
                    "suggest": ("**병합 금지 — 거리 정확**. 제자리 정지 추정. "
                                "페이스·SWOLF 산출에서만 제외"),
                })

    # --- F4: stroke mismatch vs declared workout step ----------------------
    for ln in lengths:
        if ln["type"] != "active" or ln["step_idx"] is None:
            continue
        step = steps.get(ln["step_idx"])
        if not step:
            continue
        declared = declared_stroke(steps, ln["step_idx"])
        if declared and ln["stroke"] and declared != ln["stroke"]:
            flags.append({
                "type": "F4",
                "title": "영법 불일치",
                "length_idx": ln["idx"],
                "lap_n": ln["lap_n"],
                "detail": (f"length#{ln['idx']} 실측 "
                           f"{STROKE_KO.get(ln['stroke'], ln['stroke'])} ≠ "
                           f"step {ln['step_idx']} 선언 "
                           f"{STROKE_KO.get(declared, declared)}"),
                "suggest": "영법 오검출 또는 실행 이탈 — 확인 필요",
            })

    # --- F5: structural mismatch vs the programmed length count -----------
    # v3.4 moves the comparison from laps to lengths. Detection and
    # correction both operate on lengths (Schema v3.1, "검출 단위와 조작
    # 단위 일치"); F5 was the last rule left counting laps, and a lap count
    # is blind to the failure that matters — 09-03 step6 ran its 4 laps
    # exactly as designed while carrying 7 lengths against a designed 4.
    #
    # It also read `repeat_value` off the swim step, a field these files do
    # not carry at all, so F5 never fired in any session. Its silence was
    # never evidence of a match.
    #
    # Deliberately reuses `groups`: E printed here and E printed in the
    # F1~F3 baseline note must be the same number, or the group note and
    # the structural verdict would contradict each other.
    for key, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if key[0] != "step" or not pool_length:
            continue
        st = steps.get(key[2])
        dd = st.get("duration_distance") if st else None
        if not dd:
            continue
        reps = repeats.get(key[2], 1)
        expected = dd / pool_length * reps
        actual = len(group)
        if not expected or actual == expected:
            continue
        flags.append({
            "type": "F5",
            "title": "구조 불일치",
            "length_idx": None,
            "lap_n": None,
            "detail": (f"step {key[2]} 실측 active length {actual}개 ≠ "
                       f"설계 {expected:g}개 ({dd:.0f}m ÷ "
                       f"{pool_length:.0f}m × {reps}회) — "
                       f"E={actual - expected:+g}"),
            "suggest": ("초과 — 유령 length 추정. F1 제안 인자 개수가 E와 "
                        "일치하는지 대조하라"
                        if actual > expected else
                        "부족 — 실행 이탈 또는 턴 미검출(F2) 확인"),
        })

    # --- F6: unassigned laps (informational, not an error) ----------------
    unassigned = [l["n"] for l in laps if l["is_active"] and l["step_idx"] is None]
    if unassigned:
        flags.append({
            "type": "F6",
            "title": "미할당 랩",
            "length_idx": None,
            "lap_n": None,
            "detail": (f"wkt_step_index=None 랩 {len(unassigned)}건 "
                       f"(랩 {', '.join(str(n) for n in unassigned)})"),
            "suggest": "루틴 외 추가로 분류 — 오류 아님",
        })

    order = {"F1": 0, "F2": 1, "F3": 2, "F4": 3, "F5": 4, "F6": 5, "--": 6}
    flags.sort(key=lambda f: order.get(f["type"], 9))
    return flags


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def fmt_pace(seconds):
    if not seconds:
        return "-"
    return f"{int(seconds // 60)}:{int(round(seconds % 60)):02d}"


def fmt_dur(seconds):
    if not seconds:
        return "-"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def render(group, merges=None, drops=None):
    sess = {}
    for f in group:
        for k, v in f["session"].items():
            if v is not None:
                sess.setdefault(k, v)
    # totals must sum across assembled files, not take the first
    total_dist = sum(f["session"].get("total_distance") or 0 for f in group)
    total_time = sum(f["session"].get("total_timer_time")
                     or f["session"].get("total_elapsed_time") or 0
                     for f in group)
    total_cycles = sum(f["session"].get("total_cycles") or 0 for f in group)
    act_lengths = sum(f["session"].get("num_active_lengths") or 0 for f in group)
    kcal = sum(f["session"].get("total_calories") or 0 for f in group)

    # Assembled files can each carry a different program (observed 07-25:
    # warmup + speed). Report all of them — collapsing to the first would
    # silently misattribute the routine mapping (operating rule 8).
    wkt_names = []
    for f in group:
        if f["wkt_name"] and f["wkt_name"] not in wkt_names:
            wkt_names.append(f["wkt_name"])
    wkt_name = wkt_names[0] if len(wkt_names) == 1 else None
    has_workout = any(f["steps"] for f in group)
    steps = {}
    for f in group:
        steps.update(f["steps"])

    hr_series = []
    for f in group:
        hr_series.extend(f["hr_series"])
    hr_series.sort()

    laps = build_laps(group)
    merge_note = None
    drop_note = None
    if drops:
        # v3.1 primary path — runs before merges so the fallback operates on
        # already-corrected lengths.
        garmin_dist_pre = sum(l["dist"] for l in laps if l["is_active"])
        laps = apply_drops(laps, drops, sess.get("pool_length"))
        drop_note = ",".join(str(d) for d in sorted(drops))
    if merges:
        laps = apply_merges(laps, merges)
        merge_note = ", ".join("+".join(str(m) for m in p) for p in merges)
    blocks = build_block_metrics(laps, has_workout, hr_series, steps)

    # Garmin's session totals include phantom laps and any dropped lengths.
    # Recompute from clean laps so the header agrees with block_metrics;
    # report the delta rather than silently diverging from Garmin Connect.
    phantoms = [l for l in laps if l["phantom"]]
    if phantoms or drops or merges:
        clean = [l for l in laps if l["is_active"] and not l["phantom"]]
        total_dist = sum(l["dist"] for l in clean)
        total_cycles = sum(l["cycles"] for l in clean)
        act_lengths = sum(l["active_lengths"] for l in clean)

    start = sess.get("start_time")
    start_kst = (start + KST) if start else None
    pool = sess.get("pool_length")
    course = "LCM" if pool and pool >= 50 else "SCM"
    avg_hrs = [f["session"].get("avg_heart_rate") for f in group
               if f["session"].get("avg_heart_rate")]
    avg_hr = round(statistics.fmean(avg_hrs)) if avg_hrs else None
    peak_hr = max((f["session"].get("max_heart_rate") or 0 for f in group),
                  default=None) or None
    temps = [f["session"].get("avg_temperature") for f in group
             if f["session"].get("avg_temperature") is not None]
    temp = round(statistics.fmean(temps), 1) if temps else None

    # Pace and SWOLF are swimming metrics: the numerator is time SPENT
    # SWIMMING, not elapsed session time. Using total_time (which includes
    # rest laps) inflates pace by roughly the rest fraction — 05-23 read
    # 177 s/100m against an actual 118 — and that error propagates into
    # hr_efficiency, which divides by pace.
    active_time = sum(l["secs"] for l in laps
                      if l["is_active"] and not l["phantom"])
    pace100 = pace_per_100m(active_time, total_dist)

    lines = []
    lines.append(f"schema_version: {SCHEMA_VERSION} | parser_tag: {PARSER_TAG}")
    lines.append("")
    lines.append(f"## Swim {start_kst:%Y-%m-%d}" if start_kst else "## Swim")
    lines.append(f"- files: {len(group)}" +
                 ("  (Stage 0 assembled)" if len(group) > 1 else ""))
    if drop_note:
        lines.append(f"- merge_applied: true  (--drop-lengths {drop_note} — "
                     f"USER GATE 승인분, 거리 = 잔여 active length × "
                     f"{int(sess.get('pool_length') or 0)}m 자동 산출)")
    relabeled = [l for l in laps if l.get("relabeled")]
    if relabeled:
        lines.append("- stroke_relabeled: " + ", ".join(
            f"랩{l['n']} 혼합→{STROKE_KO.get(l['stroke'], l['stroke'])}"
            for l in relabeled) + "  (교정 후 잔존 length 영법 단일)")
    if merge_note:
        lines.append(f"- merge_applied: true  (--merge {merge_note} — "
                     f"USER GATE 승인분, 폴백 경로)")
    lines.append(f"- start_kst: {start_kst:%Y-%m-%d %H:%M}" if start_kst else "")
    if len(wkt_names) > 1:
        lines.append(f"- wkt_name: {', '.join(repr(w) for w in wkt_names)}"
                     f"  ⚠ 다중 프로그램 — routine 매핑은 사용자 확인 필요")
    elif wkt_name:
        lines.append(f"- wkt_name: {wkt_name!r}")
    else:
        lines.append("- wkt_name: None  (free — workout_step 부재)")
    lines.append(f"- pool_length_m: {int(pool) if pool else '?'}  (course={course})")
    if phantoms:
        garmin_dist = sum(f["session"].get("total_distance") or 0 for f in group)
        lines.append(f"- total_distance_m: {int(total_dist)}"
                     f"  (Garmin 표시 {int(garmin_dist)} − 유령랩 "
                     f"{len(phantoms)}건 {int(garmin_dist - total_dist)}m)")
    else:
        lines.append(f"- total_distance_m: {int(total_dist)}")
    lines.append(f"- duration_min: {round(total_time / 60, 1)}  "
                 f"(경과 · 휴식 포함) | active_min: {round(active_time / 60, 1)}")
    lines.append(f"- avg_pace_per_100m: {round(pace100)}  ({fmt_pace(pace100)})"
                 if pace100 else "- avg_pace_per_100m: -")
    sess_swolf = derived_swolf(active_time, total_cycles, act_lengths)
    lines.append(f"- avg_swolf: {round(sess_swolf, 1)}" if sess_swolf
                 else "- avg_swolf: -")
    lines.append(f"- total_cycles: {total_cycles} | num_active_lengths: {act_lengths}")
    lines.append(f"- cycles_per_length: "
                 f"{round(total_cycles / act_lengths, 2) if act_lengths else '-'}")
    lines.append(f"- dps_m: {round(total_dist / total_cycles, 2) if total_cycles else '-'}")
    lines.append(f"- avg_hr: {avg_hr} | peak_hr: {peak_hr}")
    lines.append(f"- hr_efficiency: {hr_efficiency(pace100, avg_hr)}")
    lines.append(f"- water_temp_c: {temp}")
    lines.append(f"- total_calories: {int(kcal) if kcal else '-'}")

    lines.append("")
    lines.append("### laps")
    lines.append("| lap | step | dist | time | pace100 | swolf | cyc | dps | stroke | hr |")
    lines.append("|----:|-----:|-----:|-----:|--------:|------:|----:|----:|--------|---:|")
    for lap in laps:
        if not lap["is_active"]:
            continue
        if lap["phantom"]:
            lines.append(
                f"| {lap['n']} | "
                f"{lap['step_idx'] if lap['step_idx'] is not None else '-'} "
                f"| {int(lap['dist'])} | {lap['secs']:.1f}s | 유령랩 — 집계 제외 "
                f"| - | 0 | - "
                f"| {STROKE_KO.get(lap['stroke'], lap['stroke'] or '?')} "
                f"| {lap['avg_hr'] or '-'} |")
            continue
        lines.append(
            f"| {lap['n']} | {lap['step_idx'] if lap['step_idx'] is not None else '-'} "
            f"| {int(lap['dist'])} | {fmt_dur(lap['secs'])} "
            f"| {fmt_pace(lap['pace100'])} "
            f"| {round(lap['dswolf'], 1) if lap['dswolf'] else '-'} "
            f"| {lap['cycles']} "
            f"| {round(lap['dps'], 2) if lap['dps'] else '-'} "
            f"| {STROKE_KO.get(lap['stroke'], lap['stroke'] or '?')} "
            f"| {lap['avg_hr'] or '-'} |")

    lines.append("")
    lines.append("### blocks")
    lines.append("- " + ", ".join(b["signature"] for b in blocks)
                 if blocks else "- (none)")

    all_lengths = [ln for lap in laps for ln in lap["lengths"]]
    flags = collect_flags(laps, all_lengths, steps, blocks,
                          sess.get("pool_length"))
    lines.append("")
    lines.append("### 검출 플래그")
    if not flags:
        lines.append("- (없음) — F1~F5 무발화")
    for fl in flags:
        loc = []
        if fl["length_idx"] is not None:
            loc.append(f"length#{fl['length_idx']}")
        if fl["lap_n"] is not None:
            loc.append(f"랩{fl['lap_n']}")
        loc = f" [{' / '.join(loc)}]" if loc else ""
        lines.append(f"- **{fl['type']} {fl['title']}**{loc} — {fl['detail']}")
        lines.append(f"  → {fl['suggest']}")
        if fl.get("design"):
            lines.append(f"  → 근거2: {fl['design']}")
        if fl.get("proposal"):
            lines.append(f"  → **제안: `{fl['proposal']}`** "
                         f"(USER GATE 승인 시 재실행 — 거리는 자동 산출)")
    if any(f.get("proposal") for f in flags):
        lines.append("  ※ 자동 적용 금지 — 사용자가 제안을 승인해 "
                     "명시적으로 인자를 넘길 때만 동작한다")

    lines.append("")
    lines.append("### block_metrics (L3 적재용 JSON)")
    # One block per line, compact separators: readable enough to eyeball while
    # keeping the payload near the section A context budget (~450 tokens).
    # `bm1|` prefix is REQUIRED, not cosmetic: the Notion MCP connector
    # pre-parses text property values as JSON and rejects a top-level array
    # outright. The marker breaks that parse so the string is stored verbatim.
    # Consumer: value.split("|", 1)[1] then json.loads.
    # bm1 = payload schema version; bump to bm2 if the block shape changes.
    # (JSONL was rejected: a single-block session yields one line that is
    #  itself a valid JSON object, hitting the same refusal.)
    lines.append("```")
    lines.append(BM_PREFIX + "[")
    for i, b in enumerate(blocks):
        comma = "," if i < len(blocks) - 1 else ""
        lines.append(json.dumps(b, ensure_ascii=False,
                                separators=(",", ":")) + comma)
    lines.append("]")
    lines.append("```")
    return "\n".join(l for l in lines if l is not None)


def parse_drop_arg(spec):
    """`--drop-lengths 35,36,37` -> {35, 36, 37} (length message indices)."""
    out = set()
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            sys.exit(f"--drop-lengths: '{chunk}' 파싱 불가 (형식: 35,36,37)")
    return out


def apply_drops(laps, drop, pool_length):
    """v3.1 주 경로: fold fabricated lengths back into the real one.

    A dropped length's time and strokes are ABSORBED into the nearest
    preceding surviving length, not discarded: the swimmer really did swim
    that water, the watch merely split one length into several. Discarding
    would leave the survivor as a fragment and re-trip F1.

    Distance is a COUNT — it recomputes as (remaining active lengths ×
    pool_length), so no human-entered figure enters the pipeline. Lap
    boundaries are preserved: a lap loses lengths, it does not merge.
    """
    if not pool_length:
        sys.exit("--drop-lengths: pool_length 미상으로 거리 재산출 불가")
    seen = set()
    for lap in laps:
        keep = [ln for ln in lap["lengths"] if ln["idx"] not in drop]
        removed = [ln for ln in lap["lengths"] if ln["idx"] in drop]
        if not removed:
            continue
        seen.update(ln["idx"] for ln in removed)
        # absorb each removed length into the closest earlier survivor
        # (falling back to the first survivor when it led the run)
        for ln in removed:
            host = None
            for cand in keep:
                if cand["idx"] < ln["idx"] and cand["type"] == "active":
                    host = cand if host is None or cand["idx"] > host["idx"] else host
            if host is None:
                host = next((c for c in keep if c["type"] == "active"), None)
            if host is None:
                continue
            host["secs"] = (host["secs"] or 0) + (ln["secs"] or 0)
            host["strokes"] = (host["strokes"] or 0) + (ln["strokes"] or 0)
        act = [ln for ln in keep if ln["type"] == "active"]
        lap["lengths"] = keep
        lap["active_lengths"] = len(act)
        lap["dist"] = len(act) * pool_length
        lap["secs"] = sum(ln["secs"] or 0 for ln in keep)
        lap["cycles"] = sum(ln["strokes"] or 0 for ln in act)
        lap["dropped"] = sorted(ln["idx"] for ln in removed)
        lap["is_active"] = lap["dist"] > 0
        lap["phantom"] = False
        lap["pace100"] = pace_per_100m(lap["secs"], lap["dist"])
        lap["dswolf"] = derived_swolf(lap["secs"], lap["cycles"],
                                      lap["active_lengths"])
        lap["dps"] = (lap["dist"] / lap["cycles"]) if lap["cycles"] else None

        # The lap message's swim_stroke describes the PRE-correction
        # composition. A lap whose ghost length was mis-detected as
        # breaststroke reads "mixed", and it keeps reading "mixed" after the
        # ghost is folded away — which splits one wkt_step across two blocks,
        # since the block key carries stroke. Re-derive it from what is
        # actually left.
        #
        # Corrected laps only. An untouched "mixed" lap is either a real
        # medley or simply undetermined, and re-deriving those is a separate
        # question with a much wider regression surface.
        if lap.get("stroke") == "mixed":
            survivors = {ln["stroke"] for ln in act if ln["stroke"]}
            if len(survivors) == 1:
                lap["stroke"] = survivors.pop()
                lap["relabeled"] = True
    missing = drop - seen
    if missing:
        sys.exit(f"--drop-lengths: length {sorted(missing)} 없음")
    return [l for l in laps if l["is_active"] or not l.get("dropped")]


def parse_merge_arg(spec):
    """`--merge 14+15,20+21` -> [[14,15],[20,21]] (lap numbers as printed)."""
    pairs = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            members = [int(x) for x in chunk.split("+")]
        except ValueError:
            sys.exit(f"--merge: '{chunk}' 파싱 불가 (형식: 14+15,20+21)")
        if len(members) < 2:
            sys.exit(f"--merge: '{chunk}'는 2개 이상의 랩이 필요하다")
        pairs.append(members)
    return pairs


def apply_merges(laps, pairs):
    """Merge approved laps per the Schema 처리 규칙:
    time/strokes summed, HR time-weighted, SWOLF recomputed from merged
    totals. Distance is the summed count — a corrected real distance must be
    supplied by the user downstream, since the parser does not guess.
    """
    by_n = {l["n"]: l for l in laps}
    dropped = set()
    for members in pairs:
        targets = [by_n.get(n) for n in members]
        missing = [n for n, t in zip(members, targets) if t is None]
        if missing:
            sys.exit(f"--merge: 랩 {missing} 없음")
        head = targets[0]
        secs = sum(t["secs"] for t in targets)
        cycles = sum(t["cycles"] for t in targets)
        dist = sum(t["dist"] for t in targets)
        act = sum(t["active_lengths"] for t in targets)
        weighted = [(t["avg_hr"], t["secs"]) for t in targets if t["avg_hr"]]
        hr = (round(sum(h * s for h, s in weighted) / sum(s for _, s in weighted))
              if weighted and sum(s for _, s in weighted) else None)
        head.update({
            "secs": secs, "cycles": cycles, "dist": dist,
            "active_lengths": act, "avg_hr": hr,
            "max_hr": max((t["max_hr"] for t in targets if t["max_hr"]),
                          default=None),
            "lengths": [ln for t in targets for ln in t["lengths"]],
            "merged_from": members,
        })
        head["pace100"] = pace_per_100m(secs, dist)
        head["dswolf"] = derived_swolf(secs, cycles, act)
        head["dps"] = (dist / cycles) if cycles else None
        head["phantom"] = False
        dropped.update(members[1:])
    return [l for l in laps if l["n"] not in dropped]


def main(argv):
    # Output carries Korean text and typographic marks; a Windows console
    # defaults to cp949 and dies on them. Force UTF-8 rather than mangling.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    merge_spec = None
    drop_spec = None
    paths = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--merge", "--drop-lengths"):
            if i + 1 >= len(argv):
                sys.exit(f"{a}: 인자 필요")
            if a == "--merge":
                merge_spec = argv[i + 1]
            else:
                drop_spec = argv[i + 1]
            i += 2
        elif a.startswith("--merge="):
            merge_spec = a.split("=", 1)[1]
            i += 1
        elif a.startswith("--drop-lengths="):
            drop_spec = a.split("=", 1)[1]
            i += 1
        else:
            paths.append(a)
            i += 1
    if not paths:
        sys.exit(__doc__)

    merges = parse_merge_arg(merge_spec) if merge_spec else None
    drops = parse_drop_arg(drop_spec) if drop_spec else None
    files = [read_fit(p) for p in paths]
    groups = group_sessions(files)
    if (merges or drops) and len(groups) > 1:
        sys.exit("--merge/--drop-lengths는 단일 세션에만 적용 가능하다 "
                 f"(현재 {len(groups)}개 세션 검출)")
    for i, group in enumerate(groups):
        if i:
            print("\n" + "=" * 72 + "\n")
        print(render(group, merges, drops))


def _cli():
    """console_scripts entry point."""
    main(sys.argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
