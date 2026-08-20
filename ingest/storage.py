"""파일 읽기 · 쓰기 · 경로 규칙 · 봉투(envelope) 생성.

경로 규칙
    data/raw/{source}/{YYYY-MM-DD}.json.gz      원본 (가공 전, gzip)
    data/latest/{source}.json                   최신 정규화 스냅샷
    data/series/{source}.json                   시점별 요약 누적
    data/diff/{source}/{YYYY-MM-DD}.json        전일 대비 변화
    data/quarantine/{source}/{YYYY-MM-DD}.json  검사 실패분

raw 만 압축합니다. 사람이 재처리할 때만 읽기 때문입니다.
나머지는 소비자가 raw.githubusercontent.com 으로 직접 읽어야 해서 평문입니다.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

#: 저장소 루트. ingest/ 의 부모.
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"

SCHEMA_VERSION_DEFAULT = 1


# ---- 시각 -----------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(dt: datetime | None = None) -> str:
    """ISO8601 UTC. 항상 초 단위 + 'Z'."""
    dt = dt or utcnow()
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str(dt: datetime | None = None) -> str:
    """수집 날짜(UTC 기준 아님 -- KST 기준).

    Actions cron 이 00:10 KST 에 도니 UTC 로 찍으면 전날 날짜가 됩니다.
    파일 이름은 사람이 보는 날짜(KST)를 씁니다.
    """
    dt = dt or utcnow()
    kst = dt.astimezone(UTC).timestamp() + 9 * 3600
    return datetime.fromtimestamp(kst, tz=UTC).strftime("%Y-%m-%d")


# ---- 설정 -----------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_sources_config() -> dict:
    return load_yaml(CONFIG / "sources.yml").get("sources", {}) or {}


def load_source_config(name: str) -> dict:
    return load_sources_config().get(name, {}) or {}


def load_regions_config() -> dict:
    return load_yaml(CONFIG / "regions.yml")


def load_dotenv(path: Path | None = None) -> None:
    """.env 를 환경변수로 올린다. 이미 설정된 값은 덮어쓰지 않는다."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


# ---- 봉투 -----------------------------------------------------------------


def envelope(
    *,
    source: str,
    schema_version: int,
    as_of: str,
    as_of_precision: str,
    records: list[Any],
    collected_at: str | None = None,
    partial: bool = False,
    notes: str | None = None,
    scope: dict | None = None,
) -> dict:
    return {
        "source": source,
        "schema_version": schema_version,
        "collected_at": collected_at or iso_utc(),
        "as_of": as_of,
        "as_of_precision": as_of_precision,
        "record_count": len(records),
        "partial": partial,
        "notes": notes,
        # 이번 수집이 무엇을 조회했는지. 다음 실행이 이걸 보고 "범위가
        # 바뀐 것" 과 "API 가 깨진 것" 을 구분합니다.
        "scope": scope,
        "records": records,
    }


# ---- 저수준 IO ------------------------------------------------------------


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    tmp.replace(path)  # 부분 기록된 파일이 남지 않도록 원자적 교체
    return path


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_json_gz(path: Path, payload: Any) -> Path:
    """gzip 으로 압축해 저장한다. raw/ 전용.

    실측: 하루치 원본 1,916 KB -> 91 KB (약 21배). 지역을 전국으로 늘리면
    압축 없이는 저장소가 감당하지 못합니다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with tmp.open("wb") as raw_fh:
        # filename="" + mtime=0 -- 내용이 같으면 바이트도 같게 만들어
        # 헛커밋(내용은 그대로인데 gzip 헤더만 달라지는 것)을 막습니다.
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_fh, mtime=0) as fh:
            fh.write(text.encode("utf-8"))
    tmp.replace(path)
    return path


def read_json_gz(path: Path) -> Any | None:
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


# ---- 경로 -----------------------------------------------------------------


def raw_path(source: str, date: str) -> Path:
    """raw 는 gzip 으로 저장합니다 (사람이 재처리할 때만 읽으므로).

    latest/series/diff 는 소비자가 raw.githubusercontent.com 으로 직접 읽어야
    해서 압축하지 않습니다.
    """
    return DATA / "raw" / source / f"{date}.json.gz"


def latest_path(source: str) -> Path:
    return DATA / "latest" / f"{source}.json"


def series_path(source: str) -> Path:
    return DATA / "series" / f"{source}.json"


def diff_path(source: str, date: str) -> Path:
    return DATA / "diff" / source / f"{date}.json"


def quarantine_path(source: str, date: str) -> Path:
    return DATA / "quarantine" / source / f"{date}.json"


def meta_path() -> Path:
    return ROOT / "meta.json"


# ---- 도메인 IO ------------------------------------------------------------


def write_raw(source: str, date: str, envelope_payload: dict) -> Path:
    return write_json_gz(raw_path(source, date), envelope_payload)


def read_raw(source: str, date: str) -> dict | None:
    gz = raw_path(source, date)
    if gz.exists():
        return read_json_gz(gz)
    # 압축 도입 이전에 쌓인 파일도 계속 읽을 수 있게 둡니다.
    return read_json(gz.with_suffix("").with_suffix(".json"))


def list_raw_dates(source: str) -> list[str]:
    d = DATA / "raw" / source
    if not d.exists():
        return []
    dates = {p.name.split(".")[0] for p in d.glob("*.json.gz")}
    dates |= {p.stem for p in d.glob("*.json")}
    return sorted(dates)


#: 합본 latest 를 통째로 쓰는 상한. 넘으면 지역별 파일만 씁니다.
#:
#: 전국이면 합본이 약 57MB 입니다. 파일 하나로는 받을 수 있지만, **매일**
#: 커밋하면 git 히스토리에 1년이면 20GB 가 쌓여 저장소를 못 쓰게 됩니다.
#: 소비자도 강남구 하나 보려고 57MB 를 내려받게 됩니다.
LATEST_INLINE_LIMIT = 40_000


def latest_shard_dir(source: str) -> Path:
    return DATA / "latest" / source


def latest_shard_path(source: str, region_code: str) -> Path:
    return latest_shard_dir(source) / f"{region_code}.json"


def latest_index_path(source: str) -> Path:
    return latest_shard_dir(source) / "index.json"


def write_latest(source: str, envelope_payload: dict) -> Path:
    """합본 latest 를 쓴다. 너무 크면 안 씁니다 (지역별 파일이 대신합니다).

    작은 설정(서울 몇 개 구)에서는 지금까지처럼 파일 하나가 나옵니다.
    소비자가 쓰던 경로가 그대로 살아 있어야 하기 때문입니다.
    """
    path = latest_path(source)
    if envelope_payload.get("record_count", 0) > LATEST_INLINE_LIMIT:
        # 낡은 합본이 남아 있으면 소비자가 옛 데이터를 최신인 줄 알고 읽습니다.
        # 지우는 대신 records 를 비우고 어디로 가야 하는지 적어 둡니다.
        return write_json(path, {
            **{k: v for k, v in envelope_payload.items() if k != "records"},
            "records": [],
            "sharded": True,
            "notes": (
                f"레코드가 {envelope_payload['record_count']:,}건이라 합본을 만들지 않습니다. "
                f"data/latest/{source}/index.json 을 읽고 필요한 지역 파일만 받으세요."
            ),
        })
    return write_json(path, envelope_payload)


def write_latest_shards(source: str, envelope_payload: dict) -> tuple[Path, int]:
    """지역별 latest 파일 + 인덱스를 쓴다. (인덱스 경로, 지역 수)

    소비자는 index.json 을 먼저 읽고 필요한 지역만 가져갑니다.
    """
    records = envelope_payload.get("records", [])
    head = {k: v for k, v in envelope_payload.items() if k != "records"}

    by_region: dict[str, list] = {}
    for record in records:
        by_region.setdefault(str(record.get("region_code", "unknown")), []).append(record)

    d = latest_shard_dir(source)
    d.mkdir(parents=True, exist_ok=True)

    index_regions: dict[str, dict] = {}
    for code, rows in sorted(by_region.items()):
        path = write_json(latest_shard_path(source, code), {
            **head, "region_code": code, "record_count": len(rows), "records": rows,
        })
        index_regions[code] = {"record_count": len(rows), "file": path.name}

    # 이번에 0건인 지역의 낡은 파일이 남아 있으면 소비자가 옛 데이터를
    # 최신으로 읽습니다. 인덱스에 없는 파일은 치웁니다.
    for stale in d.glob("*.json"):
        if stale.name != "index.json" and stale.stem not in index_regions:
            stale.unlink()

    return write_json(latest_index_path(source), {
        **head, "regions": index_regions, "region_count": len(index_regions),
    }), len(index_regions)


def read_latest(source: str) -> dict | None:
    """직전 latest 를 읽는다. 지역별로 쪼개져 있으면 도로 합칩니다.

    합본이 없다고 빈 레코드를 돌려주면 diff 가 매일 "전부 새로 생김" 으로
    나옵니다. 어제와 오늘을 비교하는 게 이 파이프라인의 전부라, 여기서
    합치지 않으면 나머지가 전부 무의미해집니다.
    """
    doc = read_json(latest_path(source))
    if doc is None or not doc.get("sharded"):
        return doc

    index = read_json(latest_index_path(source))
    if index is None:
        return doc  # 조각이 없으면 빈 채로 -- 첫 실행처럼 다뤄집니다

    records: list = []
    for code in sorted(index.get("regions", {})):
        shard = read_json(latest_shard_path(source, code))
        if shard:
            records.extend(shard.get("records", []))
    return {**doc, "records": records, "record_count": len(records)}


def write_diff(source: str, date: str, payload: dict) -> Path:
    return write_json(diff_path(source, date), payload)


def write_quarantine(source: str, date: str, payload: dict) -> Path:
    return write_json(quarantine_path(source, date), payload)


def append_series(source: str, point: dict, *, schema_version: int) -> Path:
    """series/ 에 시점 하나를 추가한다.

    같은 (as_of, collected_date) 짝이 이미 있으면 덮어씁니다 -- 하루에 두 번
    돌려도 점이 두 개 생기지 않게.
    """
    path = series_path(source)
    doc = read_json(path) or {
        "source": source,
        "schema_version": schema_version,
        "updated_at": None,
        "points": [],
    }
    points: list[dict] = doc.get("points", [])

    key = (point.get("as_of"), (point.get("collected_at") or "")[:10])
    points = [
        p for p in points
        if (p.get("as_of"), (p.get("collected_at") or "")[:10]) != key
    ]
    points.append(point)
    points.sort(key=lambda p: (p.get("as_of") or "", p.get("collected_at") or ""))

    doc["points"] = points
    doc["schema_version"] = schema_version
    doc["updated_at"] = iso_utc()
    return write_json(path, doc)


def raw_envelope(
    *,
    source: str,
    schema_version: int,
    as_of: str,
    as_of_precision: str,
    raw: Any,
    record_count: int,
    collected_at: str | None = None,
    partial: bool = False,
    notes: str | None = None,
) -> dict:
    """raw/ 용 봉투.

    latest/ 봉투와 같은 껍데기지만 `records` 대신 `raw` 를 담습니다.
    (기획서 7-1 은 둘 다 같은 구조라고 썼지만, raw 는 정의상 '정규화 전'이라
     레코드 리스트가 없습니다. 껍데기 필드는 전부 동일하게 유지하고
     본문 키만 raw 로 둡니다. normalize() 는 이 raw 를 그대로 받습니다.)
    """
    return {
        "source": source,
        "schema_version": schema_version,
        "collected_at": collected_at or iso_utc(),
        "as_of": as_of,
        "as_of_precision": as_of_precision,
        "record_count": record_count,
        "partial": partial,
        "notes": notes,
        "raw": raw,
    }
