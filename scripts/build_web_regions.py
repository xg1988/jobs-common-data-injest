"""config/regions.yml -> web/regions.json

조회 화면의 시도/시군구 드롭다운이 읽습니다.

왜 별도 파일인가
    브라우저에서 YAML 을 파싱하려면 라이브러리를 하나 더 실어야 합니다.
    이 프로젝트의 웹 페이지는 의존성 없이 도는 게 원칙이라, 빌드 때
    JSON 으로 한 번 바꿔 둡니다.

왜 손으로 안 적는가
    지역 목록은 행정구역 개편 때마다 바뀝니다. 두 벌을 손으로 맞추면
    언젠가 어긋나고, 어긋난 쪽이 화면이면 "그 지역은 데이터가 없나 보다"
    로 보입니다. 한 곳(regions.yml)만 고치게 만듭니다.

    regions.yml 을 고쳤으면 이걸 돌리세요:
        python scripts/build_web_regions.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest import storage  # noqa: E402

#: 시군구 이름 앞에 붙은 시도 약칭 -> 화면에 쓸 이름.
#: regions.yml 의 name 은 "서울 강남구" 처럼 한 덩어리라 앞쪽을 떼어 씁니다.
SIDO_ORDER = [
    "서울", "경기", "인천", "부산", "대구", "광주", "대전", "울산", "세종",
    "강원", "충북", "충남", "전북", "전남광주", "경북", "경남", "제주",
]


#: 시도 하나가 시군구 하나인 곳. 앞에 붙일 시도명이 없습니다.
SINGLE_TIER = {"세종시": "세종"}


def split_name(name: str) -> tuple[str, str]:
    """'서울 강남구' -> ('서울', '강남구'). 못 나누면 통째로 둡니다."""
    if name in SINGLE_TIER:
        return SINGLE_TIER[name], name
    for sido in sorted(SIDO_ORDER, key=len, reverse=True):
        if name.startswith(sido + " "):
            return sido, name[len(sido) + 1 :]
    if " " in name:
        head, tail = name.split(" ", 1)
        return head, tail
    return "기타", name


def main() -> int:
    cfg = storage.load_regions_config()
    regions = cfg.get("regions") or []
    if not regions:
        print("config/regions.yml 에 regions 가 없습니다.", file=sys.stderr)
        return 1

    grouped: dict[str, list[dict]] = {}
    for entry in regions:
        code = str(entry["code"])
        sido, gu = split_name(str(entry.get("name") or code))
        grouped.setdefault(sido, []).append({"code": code, "name": gu})

    # 화면 순서를 고정합니다. dict 순서에 기대면 목록을 고칠 때마다
    # 드롭다운 순서가 널뛰어서, 쓰는 사람이 매번 다시 찾게 됩니다.
    ordered = {}
    for sido in SIDO_ORDER:
        if sido in grouped:
            ordered[sido] = sorted(grouped.pop(sido), key=lambda r: r["code"])
    for sido in sorted(grouped):  # SIDO_ORDER 에 없던 것들
        ordered[sido] = sorted(grouped[sido], key=lambda r: r["code"])

    out = {
        "generated_from": "config/regions.yml",
        "region_count": len(regions),
        "rolling_months": cfg.get("rolling_months"),
        "sido": ordered,
    }

    path = ROOT / "web" / "regions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"{len(regions)}개 시군구 / {len(ordered)}개 시도 -> {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
