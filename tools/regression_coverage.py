#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""회귀 셋 결손 감지 — L3 세션 목록과 .FIT 디렉터리를 대조한다 (Q-011).

**파일을 만들지 않는다.** 결손 .FIT은 사용자만 공급할 수 있으므로 감지와
보고까지가 이 도구의 범위다.

승인 근거는 관측된 손해다 — 08-01·08-22 .FIT이 없어 2026-09-03 P1
수정설계가 요구한 D1 회귀 대조 2건을 최초 보고 시점에 수행하지 못했고,
사용자가 수동으로 공급한 뒤에야 완료됐다. 결손은 조용히 있다가 회귀
대조를 요구받는 순간에 드러난다.

**파일 수를 세션 수로 읽으면 안 된다.** 워치가 한 세션을 전·후로 나눠
저장하므로 파일 29개가 세션 24건이다(06-29·07-18·07-25 각 2파일, 07-04
3파일). 그래서 파서의 group_sessions()를 그대로 태운다 — 조립 규칙을
여기서 다시 구현하면 파서와 어긋난다.

날짜는 KST로 맞춘다. `session.start_time`은 UTC이고 L3 `session_date`는
KST이므로, UTC 날짜로 비교하면 15:00 UTC 이후 세션에서 하루가 어긋난다
(현재 corpus에는 그런 세션이 없어 침묵하는 종류의 결함이다).

사용법
  python tools/regression_coverage.py --fitdir ../fitfiles-swim --l3 <scratch>/l3.json

  # 검출 경로 픽스처 — 임의 .FIT을 숨겨 1건이 잡히는지 본다(파일은 지우지 않는다)
  python tools/regression_coverage.py --fitdir ../fitfiles-swim --l3 <scratch>/l3.json --hide 23974314230

종료 코드 0 = 결손 0건 · 1 = 결손 있음 · 2 = 사용 오류.
"""
import argparse
import glob
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from swim_fit_parse import KST, group_sessions, read_fit   # noqa: E402

# MCP SQL 모드가 돌려주는 날짜 컬럼 별칭. 하네스와 같은 목록을 쓴다.
DATE_KEYS = ("session_date", "date:session_date:start", "d", "date")


def l3_dates(path):
    with io.open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("results", data.get("rows", []))
    if not isinstance(data, list):
        sys.exit(f"L3 JSON 형식 오류 — 배열이 아니다: {path}")
    out = {}
    for row in data:
        for k in DATE_KEYS:
            v = row.get(k)
            if v:
                out[str(v)[:10]] = row
                break
    return out


def fit_sessions(fitdir, hide):
    paths = sorted(glob.glob(os.path.join(fitdir, "*.fit")))
    hidden = [p for p in paths if any(h in os.path.basename(p) for h in hide)]
    if hidden:
        print("=" * 66)
        print(f"!! 픽스처 활성 — 스캔에서 제외: {[os.path.basename(p) for p in hidden]}")
        print("!! 파일을 지우지 않는다. 이 실행의 결손 목록은 실측이 아니다.")
        print("=" * 66)
        paths = [p for p in paths if p not in hidden]
    files = [read_fit(p) for p in paths]
    groups = group_sessions(files)
    out = {}
    for g in groups:
        start = g[0]["session"].get("start_time")
        if not start:
            continue
        day = (start + KST).strftime("%Y-%m-%d")
        out.setdefault(day, []).extend(os.path.basename(f["path"]) for f in g)
    return len(paths), groups, out


def main(argv=None):
    ap = argparse.ArgumentParser(description="회귀 셋 결손 감지 (Q-011)")
    ap.add_argument("--fitdir", required=True, help=".FIT 디렉터리")
    ap.add_argument("--l3", required=True,
                    help="L3 조회 결과 JSON (MCP 응답 그대로 넘겨도 받는다)")
    ap.add_argument("--hide", action="append", default=[],
                    help="검출 경로 확인용 — 파일명에 이 문자열이 들어가면 제외")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.fitdir):
        sys.exit(2)

    n_files, groups, by_day = fit_sessions(a.fitdir, a.hide)
    l3 = l3_dates(a.l3)

    print(f"[.FIT] 파일 {n_files}개 = 세션 그룹 {len(groups)}건 <- {a.fitdir}")
    multi = {d: fs for d, fs in sorted(by_day.items()) if len(fs) > 1}
    print(f"[.FIT] 다파일 세션 {len(multi)}건: "
          + (", ".join(f"{d}({len(fs)}파일)" for d, fs in multi.items()) or "없음"))
    print(f"[L3] 세션 {len(l3)}건 <- {a.l3}")

    missing = sorted(set(l3) - set(by_day))
    extra = sorted(set(by_day) - set(l3))
    print()
    print(f"[결손] L3에 있으나 .FIT 없음 — {len(missing)}건")
    for d in missing:
        row = l3[d]
        detail = " · ".join(str(row.get(k)) for k in
                            ("routine", "total_distance_m", "pool_length_m")
                            if row.get(k) is not None)
        print(f"  - {d}" + (f"  ({detail})" if detail else ""))
    print(f"[잉여] .FIT은 있으나 L3에 없음 — {len(extra)}건")
    for d in extra:
        print(f"  - {d}  ({', '.join(by_day[d])})")
    if extra:
        print("  잉여는 결손이 아니다 — 적재 누락이거나 회귀 셋에만 있는 세션이다."
              " 판정은 P2 소관이므로 보고만 한다.")

    print()
    print("결손 0건" if not missing
          else f"결손 {len(missing)}건 — .FIT 공급은 사용자만 할 수 있다")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
