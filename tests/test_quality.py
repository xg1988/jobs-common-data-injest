"""품질 검사 규칙과, 그 규칙이 실제로 latest 를 지키는지."""

from __future__ import annotations

from datetime import date

import pytest

from ingest import meta, pipeline, quality, storage
from ingest.base import FetchResult, Source

TODAY = date(2026, 8, 19)
MONTHS = ["2026-06", "2026-07", "2026-08"]


def apt(**over):
    base = {
        "region_code": "11680",
        "dong": "역삼동",
        "apt_name": "개나리래미안",
        "area_m2": 84.97,
        "floor": 12,
        "built_year": 2003,
        "deal_date": "2026-08-12",
        "price_manwon": 180000,
        "canceled": False,
        "canceled_date": None,
        "deal_type": "중개거래",
    }
    base.update(over)
    base["_key"] = "{}|{}".format(base["apt_name"], base["price_manwon"])
    base["_watch"] = {"canceled": base["canceled"]}
    return base


def check(records):
    return quality.check_apt_trade_records(
        records, as_of="2026-08", allowed_months=MONTHS, today=TODAY
    )


# ---------------------------------------------------------------------------
# 실거래가 전용 규칙 — 그 레코드만 격리
# ---------------------------------------------------------------------------


def test_price_out_of_range_quarantines_only_that_record():
    records = [apt(price_manwon=180000 + i) for i in range(30)]
    records.append(apt(price_manwon=50, apt_name="이상한거래"))  # 1000만원 미만

    result = check(records)

    assert result.ok is True  # 1/31 = 3.2% -> 전체 격리 아님
    assert len(result.quarantine) == 1
    assert "price_manwon" in result.quarantine[0]["reason"]
    assert quality.partition(records, result.quarantine) == records[:30]


@pytest.mark.parametrize(
    "over,expect",
    [
        ({"area_m2": 4.0}, "area_m2"),
        ({"area_m2": 900.0}, "area_m2"),
        ({"floor": 200}, "floor"),
        ({"floor": -50}, "floor"),
        ({"price_manwon": 99_999_999}, "price_manwon"),
        ({"deal_date": "2027-01-01"}, "미래 날짜"),
        ({"deal_date": "2026-01-05"}, "조회 월 밖"),
    ],
)
def test_range_rules(over, expect):
    records = [apt(price_manwon=180000 + i) for i in range(30)]
    records.append(apt(apt_name="문제", **over))
    result = check(records)
    assert len(result.quarantine) == 1
    assert expect in result.quarantine[0]["reason"]


def test_quarantine_over_5_percent_rejects_the_whole_batch():
    """파싱 로직이 깨진 신호. 전체를 격리합니다."""
    records = [apt(price_manwon=180000 + i) for i in range(90)]
    records += [apt(price_manwon=1, apt_name=f"깨짐{i}") for i in range(10)]

    result = check(records)

    assert result.ok is False  # 10/100 = 10% > 5%
    assert any("격리 비율" in e for e in result.errors)


# ---------------------------------------------------------------------------
# 공통 규칙
# ---------------------------------------------------------------------------


class _Dummy:
    name = "dummy"


def test_record_count_drop_of_40_percent_fails():
    previous = [apt(price_manwon=180000 + i) for i in range(100)]
    current = [apt(price_manwon=180000 + i) for i in range(60)]

    result = quality.run_common_checks(_Dummy(), current, previous)

    assert result.ok is False
    assert any("레코드 수 급변" in e for e in result.errors)


def test_record_count_change_within_30_percent_passes():
    previous = [apt(price_manwon=180000 + i) for i in range(100)]
    current = [apt(price_manwon=180000 + i) for i in range(120)]
    assert quality.run_common_checks(_Dummy(), current, previous).ok is True


def test_empty_response_fails():
    result = quality.run_common_checks(_Dummy(), [], [apt()])
    assert result.ok is False
    assert any("빈 응답" in e for e in result.errors)


def test_missing_key_is_an_error():
    bad = apt()
    del bad["_key"]
    result = quality.run_common_checks(_Dummy(), [bad], None)
    assert result.ok is False
    assert any("_key 누락" in e for e in result.errors)


def test_as_of_regression_fails():
    assert quality.check_as_of_regression("2026-08", "2026-07").ok is False
    assert quality.check_as_of_regression("2026-08", "2026-08").ok is True
    assert quality.check_as_of_regression(None, "2026-08").ok is True


def test_removed_ratio_warning():
    diff = {"summary": {"added": 0, "removed": 10, "changed": 0}}
    assert quality.check_removed_ratio(diff, 100) != []
    assert quality.check_removed_ratio(diff, 1000) == []


# ---------------------------------------------------------------------------
# 파이프라인 — latest 를 지키는가
# ---------------------------------------------------------------------------


class FakeSource(Source):
    name = "fake"
    as_of_precision = "month"
    schema_version = 1

    def __init__(self, records, *, as_of="2026-08", fail=False, partial=False):
        self._records = records
        self._as_of = as_of
        self._fail = fail
        self._partial = partial

    def fetch(self):
        if self._fail:
            raise RuntimeError("API 죽음")
        return FetchResult(
            raw={"items": self._records, "as_of": self._as_of}, partial=self._partial
        )

    def normalize(self, raw):
        return [dict(r) for r in raw["items"]]

    def as_of(self, raw):
        return raw["as_of"]


def seed_latest(records, as_of="2026-08", collected_at="2026-08-18T00:10:00Z"):
    env = storage.envelope(
        source="fake",
        schema_version=1,
        as_of=as_of,
        as_of_precision="month",
        records=records,
        collected_at=collected_at,
    )
    storage.write_latest("fake", env)
    return env


def test_latest_is_not_overwritten_when_fetch_fails(tmp_storage):
    good = [apt(price_manwon=180000 + i) for i in range(10)]
    seed_latest(good)

    result = pipeline.run_source(FakeSource([], fail=True), run_date="2026-08-19")

    assert result.status == "failed"
    assert storage.read_latest("fake")["records"] == good
    assert meta.get_source("fake")["status"] == "failed"
    assert meta.get_source("fake")["consecutive_failures"] == 1


def test_latest_is_not_overwritten_on_empty_response(tmp_storage):
    good = [apt(price_manwon=180000 + i) for i in range(10)]
    seed_latest(good)

    result = pipeline.run_source(FakeSource([]), run_date="2026-08-19")

    assert result.status == "stale"
    assert result.partial is True
    assert storage.read_latest("fake")["records"] == good
    raw = storage.read_raw("fake", "2026-08-19")
    assert raw["partial"] is True and raw["record_count"] == 0


def test_latest_is_not_overwritten_when_records_drop_40_percent(tmp_storage):
    previous = [apt(price_manwon=180000 + i) for i in range(100)]
    seed_latest(previous)

    result = pipeline.run_source(
        FakeSource([apt(price_manwon=180000 + i) for i in range(60)]),
        run_date="2026-08-19",
    )

    assert result.status == "quarantined"
    assert storage.read_latest("fake")["records"] == previous
    assert meta.get_source("fake")["status"] == "quarantined"
    # raw 는 남아 있어야 합니다 -- 나중에 다시 돌릴 수 있도록.
    assert storage.raw_path("fake", "2026-08-19").exists()


def test_as_of_regression_keeps_latest(tmp_storage):
    good = [apt(price_manwon=180000 + i) for i in range(10)]
    seed_latest(good, as_of="2026-08")

    result = pipeline.run_source(
        FakeSource([apt(price_manwon=180000 + i) for i in range(10)], as_of="2026-07"),
        run_date="2026-08-19",
    )

    assert result.status == "quarantined"
    assert storage.read_latest("fake")["as_of"] == "2026-08"


def test_two_consecutive_runs_produce_a_diff(tmp_storage, monkeypatch):
    # collected_at 을 고정해 diff 의 from/to 라벨이 벽시계에 흔들리지 않게 합니다.
    monkeypatch.setattr(storage, "iso_utc", lambda dt=None: "2026-08-18T00:10:00Z")
    day1 = [apt(price_manwon=180000 + i) for i in range(20)]
    r1 = pipeline.run_source(FakeSource(day1), run_date="2026-08-18")
    assert r1.status == "ok"
    assert storage.latest_path("fake").exists()
    assert not storage.diff_path("fake", "2026-08-18").exists()  # 첫 실행

    monkeypatch.setattr(storage, "iso_utc", lambda dt=None: "2026-08-19T00:10:00Z")
    day2 = [apt(price_manwon=180000 + i) for i in range(21)]
    day2[0]["canceled"] = True
    day2[0]["_watch"] = {"canceled": True}
    r2 = pipeline.run_source(FakeSource(day2), run_date="2026-08-19")

    assert r2.status == "ok"
    diff = storage.read_json(storage.diff_path("fake", "2026-08-19"))
    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 1}
    assert diff["changed"][0]["field"] == "canceled"
    assert diff["changed"][0]["after"] is True
    assert diff["from"] == "2026-08-18"
    assert diff["to"] == "2026-08-19"


def test_successful_run_updates_meta_and_series(tmp_storage):
    records = [apt(price_manwon=180000 + i) for i in range(20)]
    pipeline.run_source(FakeSource(records), run_date="2026-08-19")

    entry = meta.get_source("fake")
    assert entry["status"] == "ok"
    assert entry["record_count"] == 20
    assert entry["as_of"] == "2026-08"
    assert entry["consecutive_failures"] == 0

    series = storage.read_json(storage.series_path("fake"))
    assert len(series["points"]) == 1
    assert series["points"][0]["record_count"] == 20


def test_dry_run_writes_nothing(tmp_storage):
    records = [apt(price_manwon=180000 + i) for i in range(20)]
    result = pipeline.run_source(
        FakeSource(records), run_date="2026-08-19", dry_run=True
    )
    assert result.status == "ok"
    assert not storage.latest_path("fake").exists()
    assert not storage.meta_path().exists()


def test_raw_is_stored_gzipped_and_reproducibly(tmp_storage):
    """raw 는 gzip. 같은 내용이면 바이트도 같아야 헛커밋이 안 납니다."""
    records = [apt(price_manwon=180000 + i) for i in range(20)]
    pipeline.run_source(FakeSource(records), run_date="2026-08-19")

    path = storage.raw_path("fake", "2026-08-19")
    assert path.name.endswith(".json.gz")
    assert path.exists()
    first = path.read_bytes()
    assert first[:2] == b"\x1f\x8b"  # gzip magic

    # 읽어오면 원본 그대로
    env = storage.read_raw("fake", "2026-08-19")
    assert env["raw"]["items"] == records
    assert "2026-08-19" in storage.list_raw_dates("fake")

    # 같은 내용 재저장 -> 바이트 동일 (gzip 헤더에 시각이 안 박힘)
    storage.write_raw("fake", "2026-08-19", env)
    assert path.read_bytes() == first


def test_series_gets_one_point_per_as_of(tmp_storage):
    """소스가 여러 시점을 돌려주면 점도 그만큼 찍힙니다."""

    class MultiPeriod(FakeSource):
        def series_points(self, records, as_of):
            return [
                {"as_of": "2026-07", "record_count": 1, "metrics": {}},
                {"as_of": "2026-08", "record_count": 2, "metrics": {}},
            ]

    pipeline.run_source(
        MultiPeriod([apt(price_manwon=180000 + i) for i in range(20)]),
        run_date="2026-08-19",
    )

    series = storage.read_json(storage.series_path("fake"))
    assert [p["as_of"] for p in series["points"]] == ["2026-07", "2026-08"]
    assert [p["record_count"] for p in series["points"]] == [1, 2]
    assert all(p["backfill"] is False for p in series["points"])


# ---------------------------------------------------------------------------
# 조회 범위 변경 — '범위를 넓힌 것' 과 'API 가 깨진 것' 은 다릅니다
# ---------------------------------------------------------------------------


class _Src:
    """run_common_checks 가 보는 최소한의 소스."""

    def __init__(self, scope_changed: bool = False):
        self.scope_changed = scope_changed


def _records(n: int) -> list[dict]:
    return [{"_key": f"k{i}"} for i in range(n)]


def test_record_count_spike_is_an_error_by_default():
    """조용한 급증은 API 가 깨진 신호입니다. 잡아야 합니다."""
    result = quality.run_common_checks(_Src(), _records(131_000), _records(2_850))

    assert not result.ok
    assert any("레코드 수 급변" in e for e in result.errors)


def test_record_count_spike_is_fine_when_the_scope_changed():
    """서울 8개 구 -> 전국 254개는 +4,500% 입니다. 정상입니다.

    구분하지 않으면 전국 전환 첫날 수집이 통째로 격리되고, 로그에는
    원인을 전혀 알려 주지 않는 "레코드 수 급변" 만 남습니다.
    """
    result = quality.run_common_checks(
        _Src(scope_changed=True), _records(131_000), _records(2_850)
    )

    assert result.ok
    assert result.errors == []
    assert any("조회 범위가 바뀌었으니 정상" in w for w in result.warnings)


def test_scope_change_does_not_excuse_an_empty_response():
    """범위를 바꿨어도 0건이면 쓰면 안 됩니다."""
    result = quality.run_common_checks(_Src(scope_changed=True), [], _records(2_850))

    assert not result.ok
    assert any("빈 응답" in e for e in result.errors)


def test_backfill_stops_when_the_quota_runs_out(tmp_storage, monkeypatch):
    """한도를 다 썼으면 남은 달을 도는 게 무의미합니다.

    계속 돌면 헛돌면서, 아직 회복되지도 않은 한도를 미리 깎아 먹습니다.
    어디까지 받았고 어디서부터 이어야 하는지도 알려 줘야 합니다.
    """
    from ingest.base import QuotaExhausted

    calls: list[str] = []

    class Flaky(Source):
        name = "flaky"
        as_of_precision = "month"
        supports_backfill = True

        def fetch(self):
            raise NotImplementedError

        def normalize(self, raw):
            return []

        def as_of(self, raw):
            return raw["period"]

        def fetch_period(self, period):
            calls.append(period)
            if len(calls) >= 3:
                raise QuotaExhausted("하루 요청 한도를 다 썼습니다")
            return FetchResult(raw={"period": period, "requests": []})

    lines: list[str] = []
    results = pipeline.run_backfill(
        Flaky(), ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"],
        log=lines.append,
    )

    assert calls == ["2025-01", "2025-02", "2025-03"]   # 4·5월은 아예 안 부름
    assert len(results) == 3
    assert not results[-1].ok
    assert any("--from 2025-03 --to 2025-05" in ln for ln in lines)
