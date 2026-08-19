"""meta.json 갱신.

소비자는 데이터를 쓰기 전에 이 파일부터 읽습니다.
status != "ok" 이면 그 소스를 쓰지 않습니다.
"""

from __future__ import annotations

from typing import Any, Literal

from ingest import storage

Status = Literal["ok", "stale", "quarantined", "failed"]

#: 연속 실패가 이 횟수에 도달하면 Actions 를 실패로 끝내 알림이 가게 합니다.
FAILURE_ALERT_THRESHOLD = 3


def load() -> dict:
    doc = storage.read_json(storage.meta_path())
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("updated_at", None)
    doc.setdefault("sources", {})
    return doc


def get_source(name: str) -> dict:
    return load().get("sources", {}).get(name, {})


def _blank(name: str) -> dict:
    return {
        "status": "stale",
        "last_success": None,
        "last_attempt": None,
        "consecutive_failures": 0,
        "record_count": 0,
        "as_of": None,
        "schema_version": None,
        "quarantined_count": 0,
        "error": None,
    }


def update(
    name: str,
    *,
    status: Status,
    record_count: int | None = None,
    as_of: str | None = None,
    as_of_precision: str | None = None,
    schema_version: int | None = None,
    quarantined_count: int = 0,
    partial: bool = False,
    error: str | None = None,
    attempted_at: str | None = None,
) -> dict:
    """소스 하나의 상태를 기록하고 meta.json 을 저장한다."""
    doc = load()
    sources: dict[str, Any] = doc.setdefault("sources", {})
    entry = sources.get(name) or _blank(name)

    now = attempted_at or storage.iso_utc()
    entry["status"] = status
    entry["last_attempt"] = now
    entry["quarantined_count"] = quarantined_count
    entry["error"] = error

    if status == "ok":
        entry["last_success"] = now
        entry["consecutive_failures"] = 0
        if record_count is not None:
            entry["record_count"] = record_count
        if as_of is not None:
            entry["as_of"] = as_of
    else:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
        # 실패 시 record_count / as_of 는 마지막 성공값을 남겨 둡니다.
        # 소비자가 "언제 기준 데이터가 latest 에 있는지" 알아야 하기 때문입니다.

    if schema_version is not None:
        entry["schema_version"] = schema_version

    sources[name] = entry
    doc["updated_at"] = storage.iso_utc()
    storage.write_json(storage.meta_path(), doc)
    _mirror_to_db(name, entry, as_of_precision=as_of_precision, partial=partial)
    return entry


def _mirror_to_db(
    name: str, entry: dict, *, as_of_precision: str | None, partial: bool
) -> None:
    """상태를 DB 에도 남긴다.

    실패·격리 상태까지 전부 여기를 지나므로, 소비자는 파일이든 DB든
    같은 계약(status != 'ok' 이면 쓰지 않는다)으로 판단할 수 있습니다.

    DB 가 안 되더라도 meta.json 은 이미 저장됐으니 조용히 넘어갑니다.
    """
    from ingest import db

    if not db.enabled():
        return
    try:
        db.write_source_state(
            name,
            status=entry["status"],
            last_success=entry["last_success"],
            last_attempt=entry["last_attempt"],
            consecutive_failures=entry["consecutive_failures"],
            record_count=entry["record_count"],
            as_of=entry["as_of"],
            as_of_precision=as_of_precision or "month",
            schema_version=entry["schema_version"],
            quarantined_count=entry["quarantined_count"],
            partial=partial,
            error=entry["error"],
        )
    except Exception:  # noqa: BLE001 -- meta.json 이 이미 진실을 담고 있습니다
        pass


def alerting_sources() -> list[str]:
    """연속 실패가 임계값에 도달한 소스."""
    doc = load()
    return [
        name
        for name, entry in doc.get("sources", {}).items()
        if int(entry.get("consecutive_failures") or 0) >= FAILURE_ALERT_THRESHOLD
    ]
