#!/usr/bin/env python3
"""v3.6 — 다파일 세션 교정 인터페이스 `F:IDX` + 블록 파일 경계 (Q-014).

실세션(10-11)으로는 T3~T8을 이미 대조했지만 .FIT은 저장소 밖(.gitignore)
이라 여기서 다시 돌릴 수 없다. 그래서 같은 분기를 합성 픽스처로 고정한다
— 2파일 세션에서 같은 length 번호가 양쪽에 있는 상황이 이번 결함의 형태다.

검사가 지키는 것은 세 가지다.
① **조용한 오적용 금지** — 여러 파일에 있는 번호를 파일 없이 쓰면 거부
② **지정한 파일에만 작용** — `F:IDX`는 그 파일의 length만 건드린다
③ **블록·회복 구간은 파일 경계를 넘지 않는다**

pytest를 쓰지 않는 것은 test_v34.py와 같은 이유다(테스트 의존성 미도입).

    python tests/test_v36.py
"""

import os
import sys

try:                                  # the console defaults to cp949
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from swim_fit_parse import (active_follows_in_file, apply_drops,  # noqa: E402
                            apply_strokes_unreliable, build_block_metrics,
                            collect_flags, derived_swolf, fmt_length_refs,
                            pace_per_100m, parse_drop_arg, parse_merge_arg,
                            parse_unreliable_arg, rest_after, resolve_lengths,
                            resolve_merges)

FAILURES = []
POOL = 50.0


def check(name, cond, extra=""):
    if cond:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s %s" % (name, extra))
        FAILURES.append(name)


def exits(name, fn, *args):
    try:
        fn(*args)
        check(name, False, "SystemExit가 발생하지 않았다")
        return None
    except SystemExit as e:
        check(name, True)
        return str(e)


# --------------------------------------------------------------------------
# fixture — 2파일 세션. 두 파일 모두 length #0·#1을 갖고, #2는 F2에만 있다
# --------------------------------------------------------------------------

def length(f, idx, strokes, secs=30.0, type_="active", step_idx=None):
    return {"idx": idx, "secs": secs, "strokes": strokes,
            "stroke": "freestyle", "type": type_, "step_idx": step_idx,
            "src": "f%d.fit" % f, "f": f, "lap_n": None}


def lap(n, f, lengths, step_idx=None, active=True):
    act = [x for x in lengths if x["type"] == "active"] if active else []
    for x in lengths:
        x["lap_n"] = n
    secs = sum(x["secs"] or 0 for x in lengths)
    cycles = sum(x["strokes"] or 0 for x in act)
    dist = len(act) * POOL
    return {"n": n, "f": f, "src": "f%d.fit" % f, "lengths": lengths,
            "stroke": "freestyle", "active_lengths": len(act), "dist": dist,
            "secs": secs, "cycles": cycles, "is_active": dist > 0,
            "phantom": False, "step_idx": step_idx, "start": n,
            "avg_hr": 150, "max_hr": 160, "garmin_swolf": None,
            "pace100": pace_per_100m(secs, dist),
            "dswolf": derived_swolf(secs, cycles, len(act)),
            "dps": (dist / cycles) if cycles else None}


def session():
    """F1: 랩1(50 · #0) 랩2(휴식) 랩3(50 · #1) 랩4(휴식 90s)
       F2: 랩5(휴식 40s) 랩6(50 · #0) 랩7(휴식) 랩8(50 · #1) 랩9(100 · #2+#3)"""
    return [
        lap(1, 1, [length(1, 0, 20)]),
        lap(2, 1, [], active=False) | {"secs": 20.0},
        lap(3, 1, [length(1, 1, 21)]),
        lap(4, 1, [], active=False) | {"secs": 90.0},
        lap(5, 2, [], active=False) | {"secs": 40.0},
        lap(6, 2, [length(2, 0, 22)]),
        lap(7, 2, [], active=False) | {"secs": 30.0},
        lap(8, 2, [length(2, 1, 23)]),
        lap(9, 2, [length(2, 2, 4, 10.0), length(2, 3, 18)]),
    ]


GROUP = [
    {"path": "/x/f1.fit", "lengths": [{"message_index": i} for i in (0, 1)]},
    {"path": "/x/f2.fit", "lengths": [{"message_index": i} for i in (0, 1, 2, 3)]},
]
SINGLE = [GROUP[1]]

# --------------------------------------------------------------------------
# 1. 인자 파싱 — F:IDX는 튜플, 맨 번호는 int(파일 미지정 표지)
# --------------------------------------------------------------------------
print("[1] 인자 파싱")
check("F:IDX -> (F, IDX)", parse_drop_arg("1:3,2:25") == {(1, 3), (2, 25)})
check("맨 번호는 int로 남는다(하위 호환)", parse_drop_arg("35,36") == {35, 36})
check("혼용", parse_drop_arg("1:3, 25") == {(1, 3), 25})
check("--strokes-unreliable도 같은 문법",
      parse_unreliable_arg("2:9") == {(2, 9)})
check("--merge 구성원도 같은 문법",
      parse_merge_arg("2:14+2:15,20+21") == [[(2, 14), (2, 15)], [20, 21]])
exits("F=0은 중단", parse_drop_arg, "0:3")
exits("F:비정수는 중단", parse_drop_arg, "1:x")
exits("구분자 과다는 중단", parse_drop_arg, "1:2:3")

# --------------------------------------------------------------------------
# 2. 해석 — 모호 번호 거부 / 유일 번호 수용 / 단일 파일 종전 동작
# --------------------------------------------------------------------------
print("[2] 해석")
msg = exits("두 파일에 있는 맨 번호는 거부", resolve_lengths, {0}, GROUP,
            "--drop-lengths")
check("거부 메시지가 두 파일을 모두 지목",
      msg and "F1(f1.fit)" in msg and "F2(f2.fit)" in msg, msg)
check("한 파일에만 있는 맨 번호는 그 파일로 고정",
      resolve_lengths({2}, GROUP, "--drop-lengths") == {(2, 2)})
check("F:IDX는 그대로",
      resolve_lengths({(1, 0)}, GROUP, "--drop-lengths") == {(1, 0)})
check("단일 파일의 맨 번호는 F1(종전 동작)",
      resolve_lengths({0, 1}, SINGLE, "--drop-lengths") == {(1, 0), (1, 1)})
exits("범위 밖 F는 중단", resolve_lengths, {(3, 0)}, GROUP, "--drop-lengths")
exits("어느 파일에도 없는 맨 번호는 중단", resolve_lengths, {99}, GROUP,
      "--drop-lengths")
check("헤더 표기 — 다파일은 F:IDX",
      fmt_length_refs({(2, 25), (1, 3)}, True) == "1:3,2:25")
check("헤더 표기 — 단일 파일은 v3.5 형식 그대로",
      fmt_length_refs({(1, 36), (1, 35)}, False) == "35,36")

# --------------------------------------------------------------------------
# 3. --drop-lengths F:IDX는 지정 파일에만 작용한다
# --------------------------------------------------------------------------
print("[3] --drop-lengths 지정 파일 한정")
laps = apply_drops(session(), {(1, 0)}, POOL)
by_n = {l["n"]: l for l in laps}
check("F1 랩1의 #0만 제거", 1 not in by_n or not by_n[1]["is_active"])
check("F2 랩6의 #0은 그대로", by_n[6]["dist"] == 50.0 and by_n[6]["cycles"] == 22)
laps = apply_drops(session(), {(2, 2)}, POOL)
by_n = {l["n"]: l for l in laps}
check("F2 랩9가 100 -> 50, 시간·스트로크 흡수",
      (by_n[9]["dist"], by_n[9]["cycles"], by_n[9]["secs"]) == (50.0, 22, 40.0),
      str((by_n[9]["dist"], by_n[9]["cycles"], by_n[9]["secs"])))
check("F1은 무접촉", by_n[1]["dist"] == 50.0 and by_n[3]["dist"] == 50.0)
msg = exits("지정 파일에 없는 length는 중단", apply_drops, session(),
            {(1, 2)}, POOL)
check("중단 메시지가 F:IDX로 지목", msg and "1:2" in msg, msg)
# 대조군 — 구 API(맨 int)는 모든 파일에 작용한다. 이것이 결함의 형태이며,
# CLI 경로는 resolve_lengths가 항상 튜플로 바꾸므로 여기에 닿지 않는다.
laps = apply_drops(session(), {0}, POOL)
check("대조군: 맨 int는 양쪽 #0을 모두 친다",
      sum(1 for l in laps if l["n"] in (1, 6) and l["is_active"]) == 0)

# --------------------------------------------------------------------------
# 4. --strokes-unreliable F:IDX는 지정 파일에만 작용한다
# --------------------------------------------------------------------------
print("[4] --strokes-unreliable 지정 파일 한정")
laps = session()
detail = apply_strokes_unreliable(laps, {(2, 1)}, POOL)
check("F2 랩8만 표시", detail == [(8, [1])], str(detail))
check("F1 랩3(#1)은 미표시", "unreliable" not in laps[2])
exits("지정 파일에 없는 active length는 중단", apply_strokes_unreliable,
      session(), {(1, 3)}, POOL)

# --------------------------------------------------------------------------
# 5. --merge — 랩 번호는 세션 유일, F는 소속 확인
# --------------------------------------------------------------------------
print("[5] --merge 소속 확인")
laps = session()
check("맨 랩 번호는 그대로 통과",
      resolve_merges([[6, 8]], laps, GROUP) == [[6, 8]])
check("F:N은 소속이 맞으면 랩 번호로",
      resolve_merges([[(2, 6), (2, 8)]], laps, GROUP) == [[6, 8]])
msg = exits("다른 파일의 랩을 F로 지정하면 중단", resolve_merges,
            [[(1, 6), (1, 8)]], laps, GROUP)
check("중단 메시지가 실제 소속 파일을 지목", msg and "F2(f2.fit)" in msg, msg)
exits("범위 밖 F는 중단", resolve_merges, [[(5, 6), (5, 8)]], laps, GROUP)

# --------------------------------------------------------------------------
# 6. D2 — 블록은 파일 경계를 넘지 않는다
# --------------------------------------------------------------------------
print("[6] 블록 파일 경계")
blocks = build_block_metrics(session(), True, [], {})
laps_of = [b["laps"] for b in blocks]
check("같은 시그니처라도 파일이 다르면 다른 블록",
      [1, 3] in laps_of and [6, 8] in laps_of, str(laps_of))
check("교차 블록 부재",
      not any(set(b) & {1, 3} and set(b) & {6, 8} for b in laps_of))
one = [lap(1, 1, [length(1, 0, 20)]), lap(2, 1, [], active=False),
       lap(3, 1, [length(1, 1, 21)])]
check("단일 파일은 종전대로 한 블록",
      [b["laps"] for b in build_block_metrics(one, True, [], {})] == [[1, 3]])

# --------------------------------------------------------------------------
# 7. 회복 구간 — 파일의 마지막 본 뒤는 세션 끝과 같다
# --------------------------------------------------------------------------
print("[7] 회복 구간 파일 경계")
laps = session()
check("F1 마지막 본(랩3) 뒤에는 같은 파일의 active 랩이 없다",
      not active_follows_in_file(laps, 2))
check("대조군: rest_after는 다음 파일 휴식까지 합산한다(130s)",
      rest_after(laps, 2) == 130.0, rest_after(laps, 2))
b13 = next(b for b in build_block_metrics(laps, True, [], {})
           if b["laps"] == [1, 3])
check("F1 끝 블록의 hrr_rest_s는 null", b13["hrr_rest_s"] is None,
      b13["hrr_rest_s"])
check("F1 끝 블록의 hrr_60은 null", b13["hrr_60"] is None)
check("파일 안에서는 종전대로 — 랩6 뒤 active 존재",
      active_follows_in_file(laps, 5))

# --------------------------------------------------------------------------
# 8. 제안 인자 — 다파일 세션은 F:IDX로 낸다
# --------------------------------------------------------------------------
print("[8] F1 제안 인자 표기")


def frag_file(f):
    """free 그룹 n=6, 중앙값 20 — #2(6)·#3(10) 모두 후보, 합 16/20 = 0.80
    으로 합산 복원(C2) 확정. 선언 영법이 없으니 선두 #2가 남고 #3을 뺀다."""
    ls = [length(f, i, s) for i, s in enumerate((20, 21, 6, 10, 20, 21))]
    return [lap(10 * f + i, f, [ln]) for i, ln in enumerate(ls)]


laps = frag_file(2)
props = [fl.get("proposal") for fl in collect_flags(
    laps, [ln for l in laps for ln in l["lengths"]], {}, [], POOL, True)
    if fl.get("proposal")]
check("다파일 세션 제안 = F:IDX", props == ["--drop-lengths 2:3"], str(props))
laps = frag_file(2)
props = [fl.get("proposal") for fl in collect_flags(
    laps, [ln for l in laps for ln in l["lengths"]], {}, [], POOL)
    if fl.get("proposal")]
check("단일 파일 제안 = 종전 맨 번호", props == ["--drop-lengths 3"], str(props))

print()
if FAILURES:
    print("FAILED %d건: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("v3.6 전 항목 통과")
