"""Supabase(PostgREST) 로 쓰기.

왜 REST 인가
    psycopg 를 쓰면 의존성이 늘고 Actions 에서 DB 포트로 나가야 합니다.
    PostgREST 는 HTTPS 라 어디서든 열리고, httpx 하나면 됩니다.

권한
    쓰기는 `SUPABASE_SERVICE_ROLE_KEY` 로만 합니다 (RLS 우회).
    읽기는 publishable 키로 누구나 가능합니다 -- 소비자가 쓰는 경로입니다.

설정이 없으면 조용히 꺼집니다. 파일 저장만으로도 파이프라인은 그대로 돕니다.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import httpx

from ingest import storage

#: PostgREST 한 번에 보낼 행 수. 너무 크면 요청이 거부됩니다.
BATCH_SIZE = 500

TIMEOUT = 60.0


class DbError(RuntimeError):
    pass


def _settings() -> tuple[str, str] | None:
    storage.load_dotenv()
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        return None
    return url, key


def enabled() -> bool:
    return _settings() is not None


def why_disabled() -> str:
    return (
        "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 가 없어 DB 쓰기를 건너뜁니다. "
        "(.env.example 참고)"
    )


def _client(url: str, key: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{url}/rest/v1",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # 응답 본문을 안 받아 트래픽을 줄입니다.
            "Prefer": "return=minimal",
        },
        timeout=TIMEOUT,
    )


def _chunks(rows: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def upsert(table: str, rows: list[dict], *, on_conflict: str) -> int:
    """행을 밀어 넣는다. 같은 키가 있으면 갱신.

    on_conflict 는 대상 테이블의 유니크 키 컬럼들(콤마 구분)입니다.
    """
    if not rows:
        return 0
    settings = _settings()
    if settings is None:
        raise DbError(why_disabled())
    url, key = settings

    written = 0
    with _client(url, key) as client:
        for chunk in _chunks(rows, BATCH_SIZE):
            resp = client.post(
                f"/{table}",
                params={"on_conflict": on_conflict},
                headers={"Prefer": "return=minimal,resolution=merge-duplicates"},
                json=chunk,
            )
            if resp.status_code >= 400:
                raise DbError(
                    f"{table} upsert 실패 [{resp.status_code}] {resp.text[:400]}"
                )
            written += len(chunk)
    return written


def insert(table: str, rows: list[dict]) -> int:
    """append 전용 테이블에 넣는다 (이벤트 로그 등)."""
    if not rows:
        return 0
    settings = _settings()
    if settings is None:
        raise DbError(why_disabled())
    url, key = settings

    written = 0
    with _client(url, key) as client:
        for chunk in _chunks(rows, BATCH_SIZE):
            resp = client.post(f"/{table}", json=chunk)
            if resp.status_code >= 400:
                raise DbError(
                    f"{table} insert 실패 [{resp.status_code}] {resp.text[:400]}"
                )
            written += len(chunk)
    return written


def count(table: str, **filters: str) -> int:
    """행 수. 검증용."""
    settings = _settings()
    if settings is None:
        raise DbError(why_disabled())
    url, key = settings
    with _client(url, key) as client:
        resp = client.get(
            f"/{table}",
            params={"select": "*", **filters},
            headers={"Prefer": "count=exact", "Range": "0-0"},
        )
        if resp.status_code >= 400:
            raise DbError(f"{table} count 실패 [{resp.status_code}] {resp.text[:200]}")
        content_range = resp.headers.get("content-range", "*/0")
        return int(content_range.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# 이 프로젝트의 테이블
# ---------------------------------------------------------------------------

SOURCE_STATE = "mkt_source_state"
COLLECTION_RUN = "mkt_collection_run"
SERIES_POINT = "mkt_series_point"


def write_source_state(
    source: str,
    *,
    status: str,
    last_success: str | None,
    last_attempt: str,
    consecutive_failures: int,
    record_count: int,
    as_of: str | None,
    as_of_precision: str,
    schema_version: int | None,
    quarantined_count: int,
    partial: bool,
    error: str | None,
) -> None:
    upsert(
        SOURCE_STATE,
        [
            {
                "source": source,
                "status": status,
                "last_success": last_success,
                "last_attempt": last_attempt,
                "consecutive_failures": consecutive_failures,
                "record_count": record_count,
                "as_of": as_of,
                "as_of_precision": as_of_precision,
                "schema_version": schema_version,
                "quarantined_count": quarantined_count,
                "partial": partial,
                "error": error,
                "updated_at": last_attempt,
            }
        ],
        on_conflict="source",
    )


def write_series_points(source: str, points: list[dict]) -> int:
    rows = [
        {
            "source": source,
            "as_of": p["as_of"],
            "collected_date": (p.get("collected_at") or "")[:10],
            "collected_at": p["collected_at"],
            "record_count": p["record_count"],
            "partial": bool(p.get("partial")),
            "backfill": bool(p.get("backfill")),
            "metrics": p.get("metrics") or {},
        }
        for p in points
    ]
    return upsert(SERIES_POINT, rows, on_conflict="source,as_of,collected_date")


def write_collection_run(row: dict[str, Any]) -> None:
    insert(COLLECTION_RUN, [row])
