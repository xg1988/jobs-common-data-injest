"""오래된 달을 DB -> 파일로 옮기는 아카이브.

여기서 테스트하는 건 사실상 하나입니다: **원본을 잃지 않는가.**
아카이브는 잘 돌 때보다 잘못 돌 때가 훨씬 비쌉니다. 지운 데이터는
안 돌아옵니다.
"""

from __future__ import annotations

import gzip
from datetime import date

import pytest

from ingest import archive, db, query


def rows_for(month: str, n: int, region: str = "11680") -> list[dict]:
    return [
        {
            "key": f"{region}-{month}-{i:04d}",
            "region_code": region,
            "dong": "역삼동",
            "apt_name": "테스트아파트",
            "area_m2": 84.0,
            "floor": 5,
            "built_year": 2005,
            "deal_date": f"{month}-15",
            "price_manwon": 100000 + i,
            "canceled": False,
            "canceled_date": None,
            "deal_type": "중개거래",
            "first_seen_at": "2026-01-01T00:00:00Z",
            "last_seen_at": "2026-01-01T00:00:00Z",
        }
        for i in range(n)
    ]


class FakeDb:
    """PostgREST 대신. 달별로 행을 들고 있습니다."""

    def __init__(self, data: dict[str, list[dict]]):
        self.data = {m: list(rows) for m, rows in data.items()}
        self.deleted: list[str] = []

    def _month_of(self, where: dict) -> str:
        # where = {"and": "(deal_date.gte.2025-03-01,deal_date.lt.2025-04-01)"}
        return where["and"].split("gte.")[1][:7]

    def select_all(self, table, *, columns="*", order=None, page_size=1000, **where):
        if "and" not in where:  # db_months() 가 컬럼만 훑는 경우
            out = [r for rows in self.data.values() for r in rows]
            return [{columns: r[columns]} for r in out] if columns != "*" else out
        return list(self.data.get(self._month_of(where), []))

    def count(self, table, **where):
        return len(self.data.get(self._month_of(where), []))

    def delete(self, table, **where):
        if not where:
            raise db.DbError("조건 없는 삭제")
        month = self._month_of(where)
        gone = self.data.pop(month, [])
        self.deleted.append(month)
        return len(gone)

    def upsert(self, table, rows, *, on_conflict):
        for row in rows:
            self.data.setdefault(str(row["deal_date"])[:7], []).append(row)
        return len(rows)


@pytest.fixture
def fake_db(monkeypatch, tmp_storage):
    fake = FakeDb({"2025-03": rows_for("2025-03", 40), "2026-07": rows_for("2026-07", 12)})
    monkeypatch.setattr(db, "enabled", lambda: True)
    for name in ("select_all", "count", "delete", "upsert"):
        monkeypatch.setattr(db, name, getattr(fake, name))
    return fake


# ---------------------------------------------------------------------------
# 기간 계산
# ---------------------------------------------------------------------------


def test_cutoff_keeps_exactly_hot_months():
    """12개월 보관, 오늘이 2026-08 이면 2025-09 부터 DB 에 남습니다."""
    assert archive.cutoff_month(12, date(2026, 8, 20)) == "2025-09"
    assert archive.cutoff_month(1, date(2026, 1, 5)) == "2026-01"    # 이번 달만 남김
    assert archive.cutoff_month(3, date(2026, 1, 5)) == "2025-11"    # 해를 넘어감


def test_month_bounds_excludes_the_next_month():
    """끝을 포함하면 다음 달 1일 거래가 딸려 들어와 두 달에 중복 저장됩니다."""
    assert archive.month_bounds("2025-12") == ("2025-12-01", "2026-01-01")


# ---------------------------------------------------------------------------
# 내보내기
# ---------------------------------------------------------------------------


def test_archive_writes_file_and_leaves_db_alone_without_evict(fake_db):
    """--evict 없이는 **절대** DB 를 건드리지 않습니다."""
    r = archive.archive_month("molit_apt_trade", "2025-03", log=lambda m: None)

    assert r.ok and r.rows == 40
    assert archive.month_path("molit_apt_trade", "2025-03").exists()
    assert fake_db.deleted == []
    assert len(fake_db.data["2025-03"]) == 40


def test_archived_file_round_trips(fake_db):
    archive.archive_month("molit_apt_trade", "2025-03", log=lambda m: None)
    back = archive.read_month("molit_apt_trade", "2025-03")

    assert len(back) == 40
    assert back[0]["price_manwon"] == 100000
    assert back[0]["dong"] == "역삼동"      # 한글이 깨지지 않아야 합니다


def test_same_rows_produce_identical_bytes(fake_db):
    """같은 입력 -> 같은 바이트.

    gzip 은 기본으로 타임스탬프를 넣어서, 아무것도 안 바뀌어도 매번
    다른 파일이 나옵니다. 그러면 sha256 검증이 무의미해지고 저장소에
    쓸데없는 diff 가 쌓입니다.
    """
    first = archive._serialize(rows_for("2025-03", 5))
    second = archive._serialize(rows_for("2025-03", 5))
    assert first == second
    assert gzip.decompress(first).decode().count("\n") == 5


# ---------------------------------------------------------------------------
# 비우기 — 여기가 위험한 부분입니다
# ---------------------------------------------------------------------------


def test_evict_removes_db_rows_after_archive(fake_db):
    r = archive.archive_month("molit_apt_trade", "2025-03", evict=True, log=lambda m: None)

    assert r.evicted == 40
    assert "2025-03" not in fake_db.data          # DB 에서는 사라졌고
    assert len(archive.read_month("molit_apt_trade", "2025-03")) == 40  # 파일엔 남았고
    assert archive.load_index("molit_apt_trade")["months"]["2025-03"]["evicted"] is True


def test_evict_refuses_when_there_is_no_archive(fake_db):
    """아카이브가 없으면 DB 가 유일한 사본입니다. 지우면 안 됩니다."""
    with pytest.raises(archive.ArchiveError, match="아카이브 기록이 없습니다"):
        archive.evict_month("molit_apt_trade", "2025-03", log=lambda m: None)
    assert len(fake_db.data["2025-03"]) == 40


def test_evict_refuses_when_the_file_was_tampered_with(fake_db):
    """파일이 기록된 sha256 과 다르면 손상됐다고 봅니다."""
    archive.archive_month("molit_apt_trade", "2025-03", log=lambda m: None)
    path = archive.month_path("molit_apt_trade", "2025-03")
    path.write_bytes(gzip.compress(b'{"key":"other"}\n', mtime=0))

    with pytest.raises(archive.ArchiveError, match="sha256"):
        archive.evict_month("molit_apt_trade", "2025-03", log=lambda m: None)
    assert len(fake_db.data["2025-03"]) == 40


def test_evict_refuses_when_db_changed_after_archiving(fake_db):
    """아카이브한 뒤에 정정 신고가 들어오면 파일이 낡은 것입니다.

    그대로 지우면 그 사이 들어온 거래를 잃습니다.
    """
    archive.archive_month("molit_apt_trade", "2025-03", log=lambda m: None)
    fake_db.data["2025-03"].extend(rows_for("2025-03", 3, region="11650"))

    with pytest.raises(archive.ArchiveError, match="다시 아카이브"):
        archive.evict_month("molit_apt_trade", "2025-03", log=lambda m: None)
    assert len(fake_db.data["2025-03"]) == 43


def test_delete_without_a_filter_is_refused():
    """조건 없는 DELETE 는 테이블을 통째로 비웁니다. 막아 둡니다."""
    with pytest.raises(db.DbError, match="조건이 없습니다"):
        db.delete("mkt_apt_trade")


# ---------------------------------------------------------------------------
# 조회 — 아카이브가 조회를 망치면 안 됩니다
# ---------------------------------------------------------------------------


def test_query_reads_across_db_and_archive(fake_db):
    """비워진 달은 파일에서, 남아 있는 달은 DB 에서. 부르는 쪽은 몰라도 됩니다."""
    archive.archive_month("molit_apt_trade", "2025-03", evict=True, log=lambda m: None)

    result = query.fetch("molit_apt_trade", start="2025-03", end="2026-07")

    assert len(result) == 52                       # 40 + 12
    assert result.sources["2025-03"] == "archive"
    assert result.sources["2026-07"] == "db"
    assert result.missing == []


def test_query_separates_empty_months_from_unreadable_ones(fake_db):
    """0건인 달과 못 읽은 달은 다릅니다.

    뭉뚱그리면 "파일이 사라졌다" 가 "그 달엔 거래가 없었다" 로 보입니다.
    """
    result = query.fetch("molit_apt_trade", start="2025-01", end="2025-03")

    assert result.empty == ["2025-01", "2025-02"]   # DB 가 0건을 돌려줌
    assert result.missing == []                      # 읽기 자체는 성공
    assert len(result) == 40


def test_query_reports_a_month_whose_archive_file_vanished(fake_db):
    """비운 뒤 파일이 사라지면 **반드시** 티가 나야 합니다."""
    archive.archive_month("molit_apt_trade", "2025-03", evict=True, log=lambda m: None)
    archive.month_path("molit_apt_trade", "2025-03").unlink()

    result = query.fetch("molit_apt_trade", start="2025-03", end="2025-03")

    assert result.missing == ["2025-03"]
    assert len(result) == 0


def test_query_excludes_canceled_by_default(fake_db):
    """취소 거래를 섞으면 취소된 신고가가 역대 최고가로 잡힙니다."""
    fake_db.data["2026-07"][0]["canceled"] = True

    assert len(query.fetch("molit_apt_trade", start="2026-07", end="2026-07")) == 11
    assert len(query.fetch("molit_apt_trade", start="2026-07", end="2026-07",
                           include_canceled=True)) == 12


def test_restore_puts_an_archived_month_back(fake_db):
    archive.archive_month("molit_apt_trade", "2025-03", evict=True, log=lambda m: None)
    assert "2025-03" not in fake_db.data

    written = query.restore("molit_apt_trade", "2025-03", log=lambda m: None)

    assert written == 40
    assert len(fake_db.data["2025-03"]) == 40
    # 파일은 그대로 둡니다 -- 되돌린 뒤 다시 비울 수 있어야 합니다.
    assert archive.month_path("molit_apt_trade", "2025-03").exists()
    assert archive.load_index("molit_apt_trade")["months"]["2025-03"]["evicted"] is False
