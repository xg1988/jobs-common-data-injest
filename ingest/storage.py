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


def write_latest(source: str, envelope_payload: dict) -> Path:
    return write_json(latest_path(source), envelope_payload)


def read_latest(source: str) -> dict | None:
    return read_json(latest_path(source))


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
