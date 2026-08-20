"""latest 를 지역별로 쪼개기.

전국이면 합본이 약 57MB 입니다. 파일 하나로 받을 수는 있지만
**매일** 커밋하면 git 히스토리에 1년이면 20GB 가 쌓입니다.
소비자도 한 지역 보려고 전부 내려받게 됩니다.
"""

from __future__ import annotations

import json

from ingest import storage


def env_of(records: list[dict]) -> dict:
    return storage.envelope(
        source="molit_apt_trade",
        schema_version=1,
        as_of="2026-06",
        as_of_precision="month",
        records=records,
        collected_at="2026-08-20T00:00:00Z",
    )


def rec(code: str, i: int) -> dict:
    return {"key": f"{code}-{i}", "region_code": code, "price_manwon": 100000 + i}


def test_shards_split_by_region(tmp_storage):
    env = env_of([rec("11680", 1), rec("11680", 2), rec("11650", 3)])

    index_path, count = storage.write_latest_shards("molit_apt_trade", env)

    assert count == 2
    gangnam = json.loads(storage.latest_shard_path("molit_apt_trade", "11680").read_text("utf-8"))
    assert gangnam["record_count"] == 2
    assert {r["region_code"] for r in gangnam["records"]} == {"11680"}

    index = json.loads(index_path.read_text("utf-8"))
    assert index["regions"]["11650"]["record_count"] == 1
    assert "records" not in index          # 인덱스에 본문이 들어가면 쪼갠 의미가 없습니다
    assert index["as_of"] == "2026-06"     # 소비자가 기준시점을 인덱스만 보고 알아야 합니다


def test_stale_shard_is_removed(tmp_storage):
    """이번에 0건인 지역의 옛 파일이 남으면 소비자가 옛 데이터를 최신으로 읽습니다."""
    storage.write_latest_shards("molit_apt_trade", env_of([rec("11680", 1), rec("11650", 2)]))
    assert storage.latest_shard_path("molit_apt_trade", "11650").exists()

    storage.write_latest_shards("molit_apt_trade", env_of([rec("11680", 1)]))

    assert not storage.latest_shard_path("molit_apt_trade", "11650").exists()
    assert storage.latest_shard_path("molit_apt_trade", "11680").exists()


def test_small_source_still_gets_a_combined_file(tmp_storage):
    """서울 몇 개 구로 쓰던 소비자의 경로가 그대로 살아 있어야 합니다."""
    env = env_of([rec("11680", i) for i in range(10)])

    storage.write_latest("molit_apt_trade", env)

    doc = json.loads(storage.latest_path("molit_apt_trade").read_text("utf-8"))
    assert len(doc["records"]) == 10
    assert not doc.get("sharded")


def test_huge_source_gets_a_pointer_not_a_stale_body(tmp_storage, monkeypatch):
    """합본을 안 쓸 때 옛 파일을 그대로 두면 낡은 데이터가 최신으로 보입니다."""
    monkeypatch.setattr(storage, "LATEST_INLINE_LIMIT", 5)
    storage.write_latest("molit_apt_trade", env_of([rec("11680", i) for i in range(3)]))

    storage.write_latest("molit_apt_trade", env_of([rec("11680", i) for i in range(10)]))

    doc = json.loads(storage.latest_path("molit_apt_trade").read_text("utf-8"))
    assert doc["records"] == []          # 낡은 3건이 남아 있으면 안 됩니다
    assert doc["sharded"] is True
    assert doc["record_count"] == 10     # 실제 건수는 알려 줍니다
    assert "index.json" in doc["notes"]  # 어디로 가야 하는지도


def test_read_latest_reassembles_shards(tmp_storage, monkeypatch):
    """합본이 없으면 조각을 도로 합쳐서 돌려줘야 합니다.

    빈 레코드를 돌려주면 diff 가 매일 "전부 새로 생김" 으로 나옵니다.
    어제와 오늘을 비교하는 게 이 파이프라인의 전부라, 여기가 무너지면
    나머지가 전부 무의미해집니다.
    """
    monkeypatch.setattr(storage, "LATEST_INLINE_LIMIT", 2)
    env = env_of([rec("11680", 1), rec("11680", 2), rec("11650", 3)])

    storage.write_latest("molit_apt_trade", env)
    storage.write_latest_shards("molit_apt_trade", env)

    back = storage.read_latest("molit_apt_trade")

    assert back["record_count"] == 3
    assert {r["key"] for r in back["records"]} == {"11680-1", "11680-2", "11650-3"}
    assert back["as_of"] == "2026-06"


def test_read_latest_of_a_small_source_is_unchanged(tmp_storage):
    storage.write_latest("molit_apt_trade", env_of([rec("11680", 1)]))

    back = storage.read_latest("molit_apt_trade")

    assert len(back["records"]) == 1
    assert not back.get("sharded")
