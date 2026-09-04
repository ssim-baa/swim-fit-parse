#!/usr/bin/env python3
"""v3.4 branch coverage — the paths the 26-file corpus never reaches.

Real sessions exercise the happy paths; the guards do not fire in any of them.
Per CLAUDE.md, unreached branches are verified with synthetic fixtures rather
than by waiting for a session that happens to trip them.

No pytest: the package has no test dependency and this is not the place to
introduce one.

    python tests/test_v34.py
"""

import os
import sys

try:                                  # the console defaults to cp949
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from swim_fit_parse import (apply_drops, collect_flags,  # noqa: E402
                            declared_stroke)

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s %s" % (name, extra))
        FAILURES.append(name)


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------

def length(idx, strokes, stroke, secs=30.0, type_="active", step_idx=0):
    return {"idx": idx, "secs": secs, "strokes": strokes, "stroke": stroke,
            "type": type_, "step_idx": step_idx, "src": "fix.fit",
            "lap_n": None}


def lap(n, lengths, stroke, pool=50.0, step_idx=0):
    act = [x for x in lengths if x["type"] == "active"]
    for x in lengths:
        x["lap_n"] = n
    return {"n": n, "lengths": lengths, "stroke": stroke,
            "active_lengths": len(act), "dist": len(act) * pool,
            "secs": sum(x["secs"] or 0 for x in lengths),
            "cycles": sum(x["strokes"] or 0 for x in act),
            "is_active": len(act) > 0, "phantom": False,
            "step_idx": step_idx, "start": n}


def swim_step(idx, dist, stroke="freestyle"):
    return {"message_index": idx, "duration_distance": float(dist),
            "duration_type": "distance", "target_type": "swim_stroke",
            "target_stroke_type": stroke}


def repeat_step(idx, target, count):
    return {"message_index": idx, "duration_type": "repeat_until_steps_cmplt",
            "duration_step": target, "repeat_steps": count}


def details(flags, ftype):
    return [f["detail"] for f in flags if f["type"] == ftype]


def proposals(flags):
    return [f["proposal"] for f in flags if f.get("proposal")]


# --------------------------------------------------------------------------
# D1a — relabel after correction
# --------------------------------------------------------------------------
print("D1a  apply_drops 라벨 재산출")

# survivors all one stroke -> relabel
laps = [lap(1, [length(0, 8, "breaststroke"), length(1, 6, "freestyle"),
                length(2, 6, "breaststroke")], "mixed")]
out = apply_drops(laps, {0, 2}, 50.0)
check("잔존 영법 단일 -> 교체", out[0]["stroke"] == "freestyle"
      and out[0].get("relabeled") is True, out[0]["stroke"])
check("흡수는 전량 보존", out[0]["cycles"] == 20, out[0]["cycles"])

# survivors still mixed -> keep "mixed"
laps = [lap(1, [length(0, 8, "breaststroke"), length(1, 6, "freestyle"),
                length(2, 6, "backstroke")], "mixed")]
out = apply_drops(laps, {0}, 50.0)
check("잔존 영법 혼재 -> mixed 유지", out[0]["stroke"] == "mixed"
      and "relabeled" not in out[0], out[0]["stroke"])

# survivors carry no stroke at all -> keep "mixed"
laps = [lap(1, [length(0, 8, "breaststroke"), length(1, 6, None),
                length(2, 6, None)], "mixed")]
out = apply_drops(laps, {0}, 50.0)
check("잔존 영법 전부 None -> mixed 유지", out[0]["stroke"] == "mixed",
      out[0]["stroke"])

# label is not "mixed" -> untouched
laps = [lap(1, [length(0, 8, "breaststroke"), length(1, 6, "freestyle"),
                length(2, 6, "freestyle")], "breaststroke")]
out = apply_drops(laps, {0}, 50.0)
check("라벨이 mixed가 아니면 무변화", out[0]["stroke"] == "breaststroke"
      and "relabeled" not in out[0], out[0]["stroke"])

# drill survives as drill -> the signature becomes 50드릴, not 50자유
laps = [lap(1, [length(0, 8, "breaststroke"), length(1, 6, "drill"),
                length(2, 6, "drill")], "mixed")]
out = apply_drops(laps, {0}, 50.0)
check("잔존 영법 drill -> drill 교체", out[0]["stroke"] == "drill",
      out[0]["stroke"])


# --------------------------------------------------------------------------
# D2a — baseline selection
# --------------------------------------------------------------------------
print("D2a  기준선 선택")

# 09-03 step6 shape: 7 observed against 4 designed -> anchor 85/4 = 21.25
S6 = [21, 8, 6, 6, 23, 11, 10]
lengths = [length(i, s, "freestyle") for i, s in enumerate(S6)]
laps = [lap(1, lengths[:1], "freestyle"), lap(2, lengths[1:4], "mixed"),
        lap(3, lengths[4:5], "freestyle"), lap(4, lengths[5:], "freestyle")]
allen = [x for l in laps for x in l["lengths"]]
steps = {0: swim_step(0, 50), 1: repeat_step(1, 0, 4)}
flags = collect_flags(laps, allen, steps, [], 50.0)
d = details(flags, "F1")
check("E>0 -> 설계앵커", any("설계앵커 85÷4" in x and "E=+3" in x for x in d),
      d)
check("앵커 하에서 11·10이 F1로 잡힌다",
      any("11+10=21" in x for x in d), d)
check("F2 오발화 없음", not details(flags, "F2"), details(flags, "F2"))
check("F3 대체 발화 없음", not details(flags, "F3"), details(flags, "F3"))
f5 = details(flags, "F5")
check("F5가 같은 E를 보고한다", any("E=+3" in x for x in f5), f5)

# same population, but the program calls for 7 -> E == 0 -> median
steps_e0 = {0: swim_step(0, 50), 1: repeat_step(1, 0, 7)}
flags = collect_flags(laps, allen, steps_e0, [], 50.0)
d = details(flags, "F1")
check("E=0 -> 중앙값 유지", any("중앙값" in x for x in d)
      and not any("설계앵커" in x for x in d), d)
check("E=0이면 F5 침묵", not details(flags, "F5"), details(flags, "F5"))

# free session: no step index anywhere -> median, no design to anchor to
free_lengths = [length(i, s, "freestyle", step_idx=None) for i, s in enumerate(S6)]
free_laps = [lap(1, free_lengths[:1], "freestyle"),
             lap(2, free_lengths[1:4], "mixed"),
             lap(3, free_lengths[4:5], "freestyle"),
             lap(4, free_lengths[5:], "freestyle")]
for l in free_laps:
    l["step_idx"] = None
free_all = [x for l in free_laps for x in l["lengths"]]
flags = collect_flags(free_laps, free_all, {}, [], 50.0)
d = details(flags, "F1")
check("free 세션 -> 중앙값", d and all("중앙값" in x for x in d), d)
check("free 세션 -> F5 없음", not details(flags, "F5"))

# pool_length unknown -> no anchor, no crash
flags = collect_flags(laps, allen, steps, [], None)
check("pool_length 없음 -> 중앙값 폴백",
      all("설계앵커" not in x for x in details(flags, "F1")))


# --------------------------------------------------------------------------
# D2b — a phantom lap must not ground a C1 proposal
# --------------------------------------------------------------------------
print("D2b  유령 랩 가드")

ph_lengths = [length(i, s, "freestyle") for i, s in enumerate(S6)]
ph_laps = [lap(1, ph_lengths[:1], "freestyle"), lap(2, ph_lengths[1:4], "mixed"),
           lap(3, ph_lengths[4:5], "freestyle"), lap(4, ph_lengths[5:], "freestyle")]
ph_laps[1]["phantom"] = True
ph_all = [x for l in ph_laps for x in l["lengths"]]
flags = collect_flags(ph_laps, ph_all, steps, [], 50.0)
check("유령 랩은 C1 확정을 만들지 않는다",
      not any("[C1 확정]" in f["title"] and f["lap_n"] == 2
              for f in flags if f["type"] == "F1"))


# --------------------------------------------------------------------------
# D1b — host selection
# --------------------------------------------------------------------------
print("D1b  host 선택")

check("declared_stroke: 선언 있음",
      declared_stroke({0: swim_step(0, 50, "backstroke")}, 0) == "backstroke")
check("declared_stroke: step_idx None", declared_stroke({}, None) is None)
check("declared_stroke: target_type 불일치",
      declared_stroke({0: {"message_index": 0, "target_type": "open"}}, 0) is None)
check("declared_stroke: 미해석 enum은 그대로",
      declared_stroke({0: swim_step(0, 50, 255)}, 0) == 255)

# a C2 run whose second fragment matches the declared stroke -> host = second
mix = [length(0, 21, "freestyle"), length(1, 8, "breaststroke"),
       length(2, 6, "freestyle"), length(3, 6, "breaststroke"),
       length(4, 23, "freestyle"), length(5, 11, "freestyle"),
       length(6, 10, "freestyle")]
mlaps = [lap(1, mix[:1], "freestyle"), lap(2, mix[1:4], "mixed"),
         lap(3, mix[4:5], "freestyle"), lap(4, mix[5:], "freestyle")]
mall = [x for l in mlaps for x in l["lengths"]]
flags = collect_flags(mlaps, mall, steps, [], 50.0)
check("선언 영법과 맞는 조각을 host로",
      "--drop-lengths 1,3" in proposals(flags), proposals(flags))

# no declared stroke on the step -> leading fragment stays the host
steps_nodecl = {0: {"message_index": 0, "duration_distance": 50.0,
                    "duration_type": "distance", "target_type": "open"},
                1: repeat_step(1, 0, 4)}
flags = collect_flags(mlaps, mall, steps_nodecl, [], 50.0)
check("선언 영법 없음 -> run[0] 폴백",
      "--drop-lengths 2,3" in proposals(flags), proposals(flags))


print()
if FAILURES:
    print("FAILED %d: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all passed")
