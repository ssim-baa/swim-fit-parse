#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyPI 배포 — 자격 증명은 `.env`, 버전 정합은 이 스크립트가 본다.

**배포 전 점검이 이 도구의 본체다.** 업로드 자체는 twine 한 줄이지만,
PyPI는 같은 버전을 두 번 올릴 수 없어 **버전이 어긋난 채 올라가면 되돌릴
수 없다.** 이 프로젝트에서 그 위험이 특히 큰 이유는 파서 계약 버전이
세 곳(`pyproject.toml` · `PARSER_TAG` · git 태그)에 나뉘어 있고, P1 세션의
스탬프 GATE가 **PyPI 설치본의 출력**과 Notion 미러 문자열을 대조하기
때문이다. 셋 중 하나만 어긋나도 배포 후에 드러난다.

사용법

    python tools/release.py                # 점검 + 빌드 (업로드 안 함)
    python tools/release.py --check        # 점검만
    python tools/release.py --upload       # 점검 + 빌드 + PyPI 업로드
    python tools/release.py --from-tag     # 태그 커밋에서 빌드(작업 트리 무시)
    python tools/release.py --upload --repository testpypi

**`--from-tag`가 태그와 배포본을 어긋나지 않게 하는 정공법이다.** 태그 이후
`tools/`나 README를 손대도 배포본은 태그 그대로가 된다 — 임시 worktree에
태그를 꺼내 거기서 빌드하고, 산출물만 `dist/`로 받는다. 태그를 옮기는(force
push) 선택지를 쓰지 않아도 되는 이유다.

자격 증명은 `.env`에서 읽는다(`env.example` 참조). **토큰 값은 어디에도
출력하지 않는다** — 존재 여부와 형식만 확인한다.

종료 코드 0 = 통과 · 1 = 점검 실패 또는 업로드 실패 · 2 = 사용 오류.
"""

import argparse
import io
import os
import re
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")

FAILURES = []


def check(name, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def run(args, **kw):
    """git·python 호출 공통. 실패해도 예외 대신 (rc, out)을 준다."""
    p = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def load_env(path):
    """`.env` -> dict. python-dotenv를 쓰지 않는 이유 = 이 패키지는 런타임
    의존성이 fitparse 하나뿐이고, 배포 스크립트 때문에 개발 의존성을
    늘리지 않는다. 형식은 `KEY=value` 한 줄, `#` 주석, 따옴표 없음이다."""
    out = {}
    if not os.path.exists(path):
        return out
    with io.open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def declared_versions():
    """(pyproject version, PARSER_TAG, SCHEMA_VERSION)"""
    pyproject = io.open(os.path.join(REPO, "pyproject.toml"),
                        encoding="utf-8").read()
    src = io.open(os.path.join(REPO, "src", "swim_fit_parse", "__init__.py"),
                  encoding="utf-8").read()
    ver = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    tag = re.search(r'^PARSER_TAG = "([^"]+)"', src, re.M)
    schema = re.search(r'^SCHEMA_VERSION = "([^"]+)"', src, re.M)
    return (ver.group(1) if ver else None,
            tag.group(1) if tag else None,
            schema.group(1) if schema else None)


def preflight(env, run_tests=True, from_tag=False, need_token=True):
    version, parser_tag, schema = declared_versions()
    print(f"[버전] pyproject {version} · PARSER_TAG {parser_tag} · "
          f"schema_version {schema}")

    check("세 버전 문자열이 일치한다",
          bool(version) and parser_tag == "v" + str(version)
          and schema == version,
          f"{version} / {parser_tag} / {schema}")

    rc, out = run(["git", "status", "--porcelain"])
    if from_tag and out.strip():
        print("  info  작업 트리에 변경이 있으나 --from-tag이므로 빌드에 들어가지 "
              "않는다 — " + ", ".join(out.split()[-3:]))
    else:
        check("작업 트리가 깨끗하다", rc == 0 and not out.strip(),
              out.strip().splitlines()[:3])

    tag = "v" + str(version)
    rc, head = run(["git", "rev-parse", "HEAD"])
    rc2, tagged = run(["git", "rev-list", "-n", "1", tag])
    check(f"태그 {tag}가 존재한다", rc2 == 0, tagged.strip()[:60])
    # HEAD가 태그를 지나쳐도 되지만, **패키지에 들어가는 내용**은 태그와
    # 같아야 한다. 올라간 배포본이 태그와 다르면 "그 태그를 받아 재현한다"가
    # 성립하지 않는다. `tools/`처럼 wheel에 없는 경로는 달라도 무방하다 —
    # 그래서 커밋 해시가 아니라 경로별 diff로 본다.
    if rc2 == 0:
        packaged = ["src", "pyproject.toml", "README.md", "LICENSE"]
        rcd, outd = run(["git", "diff", "--name-only", tag, "HEAD", "--"]
                        + packaged)
        same = head.strip() == tagged.strip()
        if from_tag and outd.strip():
            # --from-tag는 태그를 꺼내 빌드하므로 작업 트리와 달라도 무방하다.
            # 다만 침묵하지는 않는다 — 무엇이 배포본에 빠지는지 이름을 찍는다.
            print("  info  태그 이후 " + ", ".join(outd.split())
                  + " 가 바뀌었다 — --from-tag이므로 배포본에는 반영되지 않는다")
        else:
            check(f"패키지 내용이 태그 {tag}와 동일하다",
                  rcd == 0 and not outd.strip(),
                  ("HEAD가 태그를 지나쳤고 " + ", ".join(outd.split())
                   + " 가 바뀌었다 — 태그를 옮기거나 --from-tag로 빌드하라")
                  if outd.strip() else ("HEAD == 태그" if same else "태그 이후 변경 없음"))

    rc, out = run(["git", "ls-remote", "--tags", "origin", tag])
    # 원격 조회는 네트워크가 필요하다. 실패는 경고로만 남긴다 — 태그 push는
    # 배포의 전제이지만 오프라인에서 빌드까지 막을 이유는 없다.
    if rc != 0:
        print(f"  warn  원격 태그 확인 불가(네트워크?) — {out.strip()[:60]}")
    else:
        check(f"원격에 태그 {tag}가 있다", tag in out,
              "push 후 배포하라: git push origin " + tag)

    token = env.get("TWINE_PASSWORD", "")
    if need_token:
        check(".env에 TWINE_PASSWORD가 있다", bool(token),
              "env.example을 복사해 .env를 만들고 토큰을 넣어라")
    elif not token:
        # 업로드하지 않는 실행에서 토큰 부재는 결함이 아니다. 빌드까지는
        # 자격 증명이 필요 없으므로 막지 않되, 상태는 알려 준다.
        print("  info  .env에 토큰이 없다 — 빌드까지는 필요 없다"
              " (업로드하려면 env.example 참조)")
    if token:
        # 값은 절대 찍지 않는다. 형식만 본다.
        check("토큰이 PyPI API 토큰 형식이다", token.startswith("pypi-"),
              "`pypi-`로 시작해야 한다 (계정 비밀번호는 더 이상 쓰이지 않는다)")
    check(".env가 git에 추적되지 않는다",
          run(["git", "ls-files", "--error-unmatch", ".env"])[0] != 0,
          "커밋 이력에 들어갔다면 토큰을 즉시 폐기하라")

    if run_tests:
        for name in ("test_v34.py", "test_v35.py"):
            path = os.path.join("tests", name)
            if not os.path.exists(os.path.join(REPO, path)):
                continue
            rc, out = run([sys.executable, path])
            check(f"{name} 통과", rc == 0, out.strip().splitlines()[-1:])
    return version


def build(version, from_tag=False):
    dist = os.path.join(REPO, "dist")
    if not os.path.exists(dist):
        os.makedirs(dist)
    rc, out = run([sys.executable, "-c", "import build"])
    if rc != 0:
        print("  FAIL  `build` 패키지가 없다 — pip install build")
        FAILURES.append("build 패키지")
        return []
    if from_tag:
        tag = "v" + str(version)
        work = os.path.join(tempfile.gettempdir(),
                            f"swim-fit-parse-{tag}-build")
        run(["git", "worktree", "remove", "--force", work])
        rc, out = run(["git", "worktree", "add", "--detach", work, tag])
        if rc != 0:
            print(out[-1000:])
            FAILURES.append("worktree")
            return []
        print(f"[빌드] {tag} 태그 worktree — {work}")
        p = subprocess.run([sys.executable, "-m", "build", "--outdir", dist],
                           cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        run(["git", "worktree", "remove", "--force", work])
        if rc != 0:
            print(out[-2000:])
            FAILURES.append("빌드")
            return []
    else:
        print("[빌드] python -m build")
        rc, out = run([sys.executable, "-m", "build"])
        if rc != 0:
            print(out[-2000:])
            FAILURES.append("빌드")
            return []
    made = [f for f in sorted(os.listdir(os.path.join(REPO, "dist")))
            if version in f]
    for f in made:
        print(f"  ok    dist/{f}")
    return made


def upload(files, env, repository=None):
    """twine 호출. 토큰은 환경변수로만 넘긴다 — 명령줄에 실으면 프로세스
    목록과 셸 이력에 남는다."""
    if not files:
        print("  FAIL  업로드할 산출물이 없다")
        return 1
    child = dict(os.environ)
    child["TWINE_USERNAME"] = env.get("TWINE_USERNAME", "__token__")
    child["TWINE_PASSWORD"] = env["TWINE_PASSWORD"]
    if env.get("TWINE_REPOSITORY_URL"):
        child["TWINE_REPOSITORY_URL"] = env["TWINE_REPOSITORY_URL"]
    args = [sys.executable, "-m", "twine", "upload"]
    if repository:
        args += ["--repository", repository]
    args += [os.path.join("dist", f) for f in files]
    print("[업로드] " + " ".join(a for a in args if not a.startswith("pypi-")))
    p = subprocess.run(args, cwd=REPO, env=child)
    return p.returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description="PyPI 배포 (.env 자격 증명)")
    ap.add_argument("--check", action="store_true", help="점검만 한다")
    ap.add_argument("--upload", action="store_true", help="PyPI에 올린다")
    ap.add_argument("--repository", help="twine --repository (예: testpypi)")
    ap.add_argument("--skip-tests", action="store_true",
                    help="점검에서 테스트 실행을 뺀다")
    ap.add_argument("--from-tag", action="store_true",
                    help="작업 트리가 아니라 태그 커밋에서 빌드한다")
    a = ap.parse_args(argv)

    env = load_env(ENV_PATH)
    print(f"[.env] {'읽음' if env else '없음'} — {ENV_PATH}")
    version = preflight(env, run_tests=not a.skip_tests,
                        from_tag=a.from_tag, need_token=a.upload)
    if FAILURES:
        print()
        print(f"점검 실패 {len(FAILURES)}건: {', '.join(FAILURES)}")
        return 1
    print("  점검 전 항목 통과")

    if a.check:
        return 0
    made = build(version, from_tag=a.from_tag)
    if FAILURES:
        return 1
    if not a.upload:
        print()
        print("빌드까지 끝났다. 올리려면 --upload 를 준다.")
        print("**PyPI는 같은 버전을 다시 올릴 수 없다** — 버전 표를 한 번 더 본다.")
        return 0
    return upload(made, env, a.repository)


if __name__ == "__main__":
    sys.exit(main())
