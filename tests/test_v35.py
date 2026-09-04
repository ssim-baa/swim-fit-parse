#!/usr/bin/env python3
"""v3.5 — `--strokes-unreliable` 커버리지 (Q-004).

이 경로는 실세션 corpus로는 한 번도 실행되지 않는다. 사용자가 인자를 주기
전까지 파서가 스스로 표시를 세우지 않기 때문이며, 그것이 이 기능의 설계
전제다(판정 주체 = 사용자). 미도달 분기는 합성 픽스처로 검증한다.

검사가 지키는 것은 두 가지다.
① **값은 고치지 않는다** — 랩의 거리·시간·스트로크 원값이 그대로 남는가
② **스트로크 파생 4종만 제외 모집단으로 산출되는가**

pytest를 쓰지 않는 것은 test_v34.py와 같은 이유다(테스트 의존성 미도입).

    python tests/test_v35.py
"""

import os
import sys

try:                                  # the console defaults to cp949
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from swim_fit_parse import (apply_strokes_unreliable,  # noqa: E402
                            build_block_metrics, derived_swolf,
                            pace_per_100m, parse_unreliable_arg)

FAILURES = []
POOL = 50.0


def check(name, cond, extra=""):
    if cond:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s %s" % (name, extra))
        FAILURES.append(name)


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------

def length(idx, strokes, secs=30.0, type_="active", stroke="freestyle"):
    return {"idx": idx, "secs": secs, "strokes": strokes, "stroke": stroke,
            "type": type_, "step_idx": 0, "src": "fix.fit", "lap_n": None}


def lap(n, lengths, step_idx=0, avg_hr=150, max_hr=160):
    act = [x for x in lengths if x["type"] == "active"]
    for x in lengths:
        x["lap_n"] = n
    secs = sum(x["secs"] or 0 for x in lengths)
    cycles = sum(x["strokes"] or 0 for x in act)
    dist = len(act) * POOL
    return {"n": n, "lengths": lengths, "stroke": "freestyle",
            "active_lengths": len(act), "dist": dist, "secs": secs,
            "cycles": cycles, "is_active": len(act) > 0, "phantom": False,
            "step_idx": step_idx, "start": n, "avg_hr": avg_hr,
            "max_hr": max_hr, "garmin_swolf": None,
            "pace100": pace_per_100m(secs, dist),
            "dswolf": derived_swolf(secs, cycles, len(act)),
            "dps": (dist / cycles) if cycles else None}


def two_rep_block():
    """같은 step의 50m 2본. 각 랩은 length 1개다."""
    return [lap(1, [length(0, 20)]), lap(2, [length(1, 24)])]


# --------------------------------------------------------------------------
# 1. 값은 고치지 않는다
# --------------------------------------------------------------------------
print("[1] 값 미변경 — 표시만 세운다")
laps = two_rep_block()
before = [(l["dist"], l["secs"], l["cycles"], l["active_lengths"]) for l in laps]
detail = apply_strokes_unreliable(laps, {1}, POOL)
after = [(l["dist"], l["secs"], l["cycles"], l["active_lengths"]) for l in laps]
check("랩 원값(거리·시간·스트로크·length 수) 불변", before == after,
      "%s -> %s" % (before, after))
check("반환값이 랩·length를 지목한다", detail == [(2, [1])], str(detail))
check("표시된 랩에만 unreliable 키", "unreliable" not in laps[0]
      and laps[1]["unreliable"] == [1])

# --------------------------------------------------------------------------
# 2. 스트로크 파생만 제외 모집단으로 산출
# --------------------------------------------------------------------------
print("[2] 제외 산출 — 파생 4종만")
laps = [lap(1, [length(0, 20), length(1, 4), length(2, 22), length(3, 21)])]
apply_strokes_unreliable(laps, {1}, POOL)
l = laps[0]
check("sc_cycles = 원 스트로크 − 제외분", l["sc_cycles"] == 63, l.get("sc_cycles"))
check("sc_lengths = 원 length 수 − 1", l["sc_lengths"] == 3, l.get("sc_lengths"))
check("sc_dist = 원 거리 − 제외 length 거리", l["sc_dist"] == 150.0,
      l.get("sc_dist"))
check("sc_secs = 원 시간 − 제외 length 시간", l["sc_secs"] == 90.0,
      l.get("sc_secs"))
check("dswolf가 신뢰 모집단으로 재산출", round(l["dswolf"], 4) == 51.0,
      l["dswolf"])
check("dps도 신뢰 모집단으로 재산출",
      round(l["dps"], 4) == round(150.0 / 63, 4), l["dps"])
check("cycles·dist·secs 원값은 그대로",
      (l["cycles"], l["dist"], l["secs"]) == (67, 200.0, 120.0),
      "%s" % [l["cycles"], l["dist"], l["secs"]])

# --------------------------------------------------------------------------
# 3. 블록 지표 — distance_m은 전량, dps_m·spi·swolf·cpl은 제외 모집단
# --------------------------------------------------------------------------
print("[3] block_metrics 반영")
laps = two_rep_block()
base = build_block_metrics(laps, True, [], {})[0]
laps = two_rep_block()
apply_strokes_unreliable(laps, {1}, POOL)
got = build_block_metrics(laps, True, [], {})[0]
check("distance_m은 전량 유지(거리는 스트로크 파생이 아니다)",
      got["distance_m"] == base["distance_m"] == 100, got["distance_m"])
check("cycles_per_length가 신뢰 length만 반영",
      got["cycles_per_length"] == 20.0, got["cycles_per_length"])
check("dps_m이 신뢰 length만 반영", got["dps_m"] == 2.5, got["dps_m"])
check("spi가 dps 변화를 따라간다", got["spi"] != base["spi"],
      "%s vs %s" % (base["spi"], got["spi"]))
check("pace_median은 불변(시간은 오염되지 않았다)",
      got["pace_median"] == base["pace_median"], got["pace_median"])
check("avg_hr·peak_hr 불변",
      (got["avg_hr"], got["peak_hr"]) == (base["avg_hr"], base["peak_hr"]))

# --------------------------------------------------------------------------
# 4. 전량 제외 = 0이 아니라 미측정(null)
# --------------------------------------------------------------------------
print("[4] 전량 제외는 0이 아니라 null")
laps = [lap(1, [length(0, 20)])]
apply_strokes_unreliable(laps, {0}, POOL)
one = build_block_metrics(laps, True, [], {})[0]
check("cycles 0 -> dps_m null", one["dps_m"] is None, one["dps_m"])
check("cycles 0 -> cycles_per_length null", one["cycles_per_length"] is None)
check("cycles 0 -> swolf null", one["swolf"] is None)
check("cycles 0 -> spi null", one["spi"] is None)
check("distance_m은 남는다", one["distance_m"] == 50, one["distance_m"])

# --------------------------------------------------------------------------
# 5. 무플래그 경로는 v3.4와 동일
# --------------------------------------------------------------------------
print("[5] 무플래그 불변")
laps = two_rep_block()
untouched = build_block_metrics(laps, True, [], {})[0]
check("sc_* 키가 없으면 원값으로 떨어진다",
      (untouched["dps_m"], untouched["cycles_per_length"]) == (2.27, 22.0),
      "%s" % [untouched["dps_m"], untouched["cycles_per_length"]])

# --------------------------------------------------------------------------
# 6. 가드 — 없는 length · idle length · pool_length 미상
# --------------------------------------------------------------------------
print("[6] 가드")
for name, args in (
        ("존재하지 않는 length는 중단", (two_rep_block(), {99}, POOL)),
        ("idle length 지정은 중단",
         ([lap(1, [length(0, 20), length(1, None, 12.0, "idle")])], {1}, POOL)),
        ("pool_length 미상이면 중단", (two_rep_block(), {1}, None))):
    try:
        apply_strokes_unreliable(*args)
        check(name, False, "SystemExit가 발생하지 않았다")
    except SystemExit:
        check(name, True)

# --------------------------------------------------------------------------
# 7. 인자 파싱
# --------------------------------------------------------------------------
print("[7] 인자 파싱")
check("콤마 목록", parse_unreliable_arg("9,12") == {9, 12})
check("공백 허용", parse_unreliable_arg(" 9 , 12 ") == {9, 12})
check("빈 청크 무시", parse_unreliable_arg("9,,12") == {9, 12})
try:
    parse_unreliable_arg("9,x")
    check("비정수는 중단", False, "SystemExit가 발생하지 않았다")
except SystemExit:
    check("비정수는 중단", True)

print()
if FAILURES:
    print("FAILED %d건: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("v3.5 전 항목 통과")
