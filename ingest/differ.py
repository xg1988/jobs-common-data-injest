"""diff 엔진.

append형(실거래가)과 update형(금융상품 금리) 둘 다 같은 방식으로 처리합니다.
  - 짝짓기 기준은 `_key`
  - "변화"로 볼 필드는 각 레코드의 `_watch`

append형은 `_watch` 가 사실상 비어 있어 added 만 나오고,
update형은 `_watch` 가 채워져 changed 가 주로 나옵니다.
실거래가는 append형이지만 `_watch = {"canceled": ...}` 하나 때문에
"신고가 취소" 가 changed 로 잡힙니다.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def index_by_key(records: Iterable[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rec in records:
        key = rec.get("_key")
        if key is None:
            continue
        out[str(key)] = rec
    return out


def diff_records(
    previous: list[dict] | None,
    current: list[dict],
    *,
    source: str,
    from_label: str,
    to_label: str,
) -> dict[str, Any]:
    prev_idx = index_by_key(previous or [])
    curr_idx = index_by_key(current)

    added = [
        {"_key": k, "record": curr_idx[k]}
        for k in curr_idx
        if k not in prev_idx
    ]
    removed = [
        {"_key": k, "record": prev_idx[k]}
        for k in prev_idx
        if k not in curr_idx
    ]

    changed: list[dict] = []
    for key in curr_idx:
        if key not in prev_idx:
            continue
        before_watch = prev_idx[key].get("_watch") or {}
        after_watch = curr_idx[key].get("_watch") or {}
        for field in sorted(set(before_watch) | set(after_watch)):
            before = before_watch.get(field)
            after = after_watch.get(field)
            if before != after:
                changed.append(
                    {
                        "_key": key,
                        "field": field,
                        "before": before,
                        "after": after,
                    }
                )

    added.sort(key=lambda e: e["_key"])
    removed.sort(key=lambda e: e["_key"])
    changed.sort(key=lambda e: (e["_key"], e["field"]))

    return {
        "source": source,
        "from": from_label,
        "to": to_label,
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }


def is_empty(diff: dict) -> bool:
    s = diff.get("summary", {})
    return not (s.get("added") or s.get("removed") or s.get("changed"))
