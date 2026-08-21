"""PostgREST 로 쓰기. 어디에 떠 있든 상관하지 않습니다.

왜 REST 인가
    psycopg 를 쓰면 의존성이 늘고 Actions 에서 DB 포트로 나가야 합니다.
    PostgREST 는 HTTPS 라 어디서든 열리고, httpx 하나면 됩니다.

    이 덕분에 DB 를 Supabase 에서 VPS 로 옮겨도 이 파일은 안 바뀝니다.
    주소와 키만 갈아 끼우면 됩니다 (docs/VPS_DB.md).

어디를 보나
    DB_API_URL / DB_API_KEY 를 먼저 봅니다. 없으면 예전 이름
    (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY) 으로 물러섭니다 --
    이미 돌고 있는 VPS 의 .env 를 안 건드려도 되게.

권한
    쓰기 키는 RLS 를 우회하는 관리자 키입니다.
    읽기는 공개 키로 누구나 가능합니다 -- 소비자가 쓰는 경로입니다.

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


#: (새 이름, 예전 이름). 예전 이름은 Supabase 를 쓰던 시절의 것입니다.
URL_ENV = ("DB_API_URL", "SUPABASE_URL")
KEY_ENV = ("DB_API_KEY", "SUPABASE_SERVICE_ROLE_KEY")


def _first_env(names: tuple[str, ...]) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _settings() -> tuple[str, str] | None:
    storage.load_dotenv()
    url = _first_env(URL_ENV).rstrip("/")
    key = _first_env(KEY_ENV)
    if not url or not key:
        return None
    return url, key


def enabled() -> bool:
    return _settings() is not None


def why_disabled() -> str:
    return (
        f"{URL_ENV[0]} / {KEY_ENV[0]} 가 없어 DB 쓰기를 건너뜁니다. "
        f"(예전 이름 {URL_ENV[1]} / {KEY_ENV[1]} 도 봅니다. .env.example 참고)"
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


def select_all(table: str, *, columns: str = "*", order: str, page_size: int = 1000, **filters: str) -> list[dict]:
    """조건에 맞는 행을 전부 읽는다. 페이지로 나눠 받습니다.

    PostgREST 는 한 번에 돌려주는 행 수에 상한이 있어(기본 1000),
    그냥 GET 하면 **조용히 잘린 결과**를 받습니다. 아카이브에서 이건
    치명적입니다 -- 잘린 줄 모르고 원본을 지우게 됩니다.

    order 를 반드시 받는 이유도 같습니다. 정렬이 없으면 페이지 사이에
    같은 행이 두 번 오거나 아예 빠질 수 있습니다.
    """
    settings = _settings()
    if settings is None:
        raise DbError(why_disabled())
    url, key = settings

    rows: list[dict] = []
    with _client(url, key) as client:
        offset = 0
        while True:
            resp = client.get(
                f"/{table}",
                params={"select": columns, "order": order, **filters},
                headers={
                    "Range-Unit": "items",
                    "Range": f"{offset}-{offset + page_size - 1}",
                    "Prefer": "count=exact",
                },
            )
            if resp.status_code >= 400:
                raise DbError(f"{table} select 실패 [{resp.status_code}] {resp.text[:300]}")
            page = resp.json()
            rows.extend(page)
            total = int(resp.headers.get("content-range", "*/0").rsplit("/", 1)[-1])
            offset += len(page)
            if not page or offset >= total:
                break

    return rows


def delete(table: str, **filters: str) -> int:
    """조건에 맞는 행을 지운다. 지운 행 수를 돌려줍니다.

    filters 가 비면 **거부합니다**. 실수로 테이블을 통째로 비우는
    사고가 이 한 줄에서 갈립니다.
    """
    if not filters:
        raise DbError("delete 에 조건이 없습니다. 전체 삭제는 막혀 있습니다.")
    settings = _settings()
    if settings is None:
        raise DbError(why_disabled())
    url, key = settings
    with _client(url, key) as client:
        resp = client.delete(
            f"/{table}",
            params=dict(filters),
            headers={"Prefer": "return=representation"},
        )
        if resp.status_code >= 400:
            raise DbError(f"{table} delete 실패 [{resp.status_code}] {resp.text[:300]}")
        return len(resp.json())


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
