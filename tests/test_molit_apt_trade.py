"""소스 ①: 국토교통부 아파트 매매 실거래가 — 정규화 · 페이징 · 인증키."""

from __future__ import annotations

import httpx
import pytest
import respx
from conftest import fixture

from ingest.sources import molit_apt_trade as m


def make_source(monkeypatch, **overrides):
    monkeypatch.setenv("DATA_GO_KR_KEY", "test-decoded-key")
    src = m.MolitAptTrade(
        {
            "base_url": "http://example.test/RTMSDataSvcAptTrade",
            "num_of_rows": 2,
            "request_delay": 0,
            "backfill_delay": 0,
            "max_retries": 1,
            **overrides,
        }
    )
    src.regions = [{"code": "11680", "name": "서울 강남구"}]
    src.rolling_months = 1
    return src


def raw_from_items(items: list[dict], months=("2026-08",)) -> dict:
    return {
        "endpoint": "http://example.test",
        "response_format": "xml",
        "fetched_at": "2026-08-19T00:00:00Z",
        "months": list(months),
        "regions": [{"code": "11680", "name": "서울 강남구"}],
        "requests": [
            {
                "region_code": "11680",
                "region_name": "서울 강남구",
                "deal_ymd": "202608",
                "ok": True,
                "error": None,
                "total_count": len(items),
                "pages": 1,
                "items": items,
            }
        ],
    }


# ---------------------------------------------------------------------------
# 인증키
# ---------------------------------------------------------------------------


def test_encoding_key_is_decoded_once():
    """Encoding 키를 넣어도 Decoding 키로 되돌린다 (이중 인코딩 방지)."""
    assert m.normalize_service_key("abc%2Bdef%3D") == "abc+def="


def test_decoding_key_is_left_alone():
    assert m.normalize_service_key("abc+def=") == "abc+def="


# ---------------------------------------------------------------------------
# 파싱
# ---------------------------------------------------------------------------


def test_parse_price_strips_comma_and_space():
    """거래금액은 `" 80,000"` 형태로 온다. 콤마·공백 제거 후 int, 단위 만원."""
    assert m.parse_price_manwon(" 80,000") == 80000
    assert m.parse_price_manwon("250,000") == 250000
    assert m.parse_price_manwon("") is None


def test_parse_canceled():
    assert m.parse_canceled("O") is True
    assert m.parse_canceled(" ") is False
    assert m.parse_canceled("") is False


def test_parse_flexible_date():
    assert m.parse_flexible_date("26.08.14") == "2026-08-14"
    assert m.parse_flexible_date("2026.08.14") == "2026-08-14"
    assert m.parse_flexible_date("20260814") == "2026-08-14"
    assert m.parse_flexible_date("") is None
    assert m.parse_flexible_date("이상한값") is None


def test_auth_failure_is_an_api_error():
    with pytest.raises(m.ApiError) as exc:
        m.parse_xml_response(fixture("molit_apt_trade_authfail.xml"))
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(exc.value)


def test_empty_response_parses_to_zero_items():
    items, total, code, _ = m.parse_xml_response(fixture("molit_apt_trade_empty.xml"))
    assert items == []
    assert total == 0
    assert code == "000"


# ---------------------------------------------------------------------------
# 정규화
# ---------------------------------------------------------------------------


def test_normalize_matches_expected_schema(monkeypatch):
    src = make_source(monkeypatch)
    items, _, _, _ = m.parse_xml_response(fixture("molit_apt_trade_page1.xml"))
    records = src.normalize(raw_from_items(items))

    assert len(records) == 2
    first = records[0]
    assert first == {
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
        "_key": "11680|역삼동|개나리래미안|84.97|12|20260812|180000",
        "_watch": {"canceled": False},
    }


def test_canceled_trade_is_kept_not_deleted(monkeypatch):
    """해제 거래를 삭제하지 말고 플래그만 붙여 보관한다."""
    src = make_source(monkeypatch)
    items, _, _, _ = m.parse_xml_response(fixture("molit_apt_trade_page1.xml"))
    records = src.normalize(raw_from_items(items))

    canceled = [r for r in records if r["canceled"]]
    assert len(canceled) == 1
    assert canceled[0]["apt_name"] == "타워팰리스"
    assert canceled[0]["canceled_date"] == "2026-08-14"
    assert canceled[0]["_watch"] == {"canceled": True}
    # 취소분도 레코드로 남아 있어야 합니다.
    assert len(records) == 2


def test_missing_built_year_becomes_none(monkeypatch):
    src = make_source(monkeypatch)
    items, _, _, _ = m.parse_xml_response(fixture("molit_apt_trade_page2.xml"))
    records = src.normalize(raw_from_items(items))
    assert records[0]["built_year"] is None
    assert records[0]["deal_type"] is None


def test_key_collision_gets_serial_number_not_an_error(monkeypatch):
    """같은 날 같은 단지 같은 면적·층·가격 거래가 둘 이상. 에러 아님."""
    src = make_source(monkeypatch)
    items, _, _, _ = m.parse_xml_response(fixture("molit_apt_trade_page1.xml"))
    duplicated = [items[0], dict(items[0]), dict(items[0])]
    records = src.normalize(raw_from_items(duplicated))

    keys = [r["_key"] for r in records]
    assert len(set(keys)) == 3
    assert keys[0].endswith("|180000")
    assert keys[1].endswith("#2")
    assert keys[2].endswith("#3")
    assert src.last_key_collisions == 2

    result = src.validate(records, None)
    assert result.errors == []  # 충돌은 경고일 뿐
    assert any("_key 충돌 2건" in w for w in result.warnings)


def test_failed_request_items_are_skipped(monkeypatch):
    src = make_source(monkeypatch)
    raw = raw_from_items([])
    raw["requests"][0]["ok"] = False
    raw["requests"][0]["error"] = "ConnectTimeout"
    assert src.normalize(raw) == []


def test_as_of_is_the_latest_month_queried(monkeypatch):
    src = make_source(monkeypatch)
    raw = raw_from_items([], months=("2026-06", "2026-07", "2026-08"))
    assert src.as_of(raw) == "2026-08"


# ---------------------------------------------------------------------------
# 페이징 (HTTP 모킹)
# ---------------------------------------------------------------------------


@respx.mock
def test_pagination_collects_every_page(monkeypatch):
    """totalCount 를 보고 끝까지 돈다. numOfRows=2, totalCount=3 -> 2페이지."""
    src = make_source(monkeypatch)
    route = respx.get("http://example.test/RTMSDataSvcAptTrade").mock(
        side_effect=[
            httpx.Response(200, text=fixture("molit_apt_trade_page1.xml")),
            httpx.Response(200, text=fixture("molit_apt_trade_page2.xml")),
        ]
    )

    fetched = src.fetch()

    assert route.call_count == 2
    assert fetched.partial is False
    entry = fetched.raw["requests"][0]
    assert entry["pages"] == 2
    assert len(entry["items"]) == 3
    assert src.raw_record_count(fetched.raw) == 3

    # 요청 파라미터도 확인 -- serviceKey 는 디코딩된 값이어야 합니다.
    q = route.calls[1].request.url.params
    assert q["pageNo"] == "2"
    assert q["LAWD_CD"] == "11680"
    assert q["serviceKey"] == "test-decoded-key"


@respx.mock
def test_fetch_marks_partial_when_some_requests_fail(monkeypatch):
    src = make_source(monkeypatch)
    src.rolling_months = 2  # 두 달 조회 -> 하나만 실패시킵니다
    respx.get("http://example.test/RTMSDataSvcAptTrade").mock(
        side_effect=[
            httpx.Response(200, text=fixture("molit_apt_trade_empty.xml")),
            httpx.ConnectTimeout("boom"),
        ]
    )

    fetched = src.fetch()

    assert fetched.partial is True
    assert "서울 강남구" in (fetched.notes or "")
    assert [r["ok"] for r in fetched.raw["requests"]] == [True, False]


@respx.mock
def test_fetch_raises_when_every_request_fails(monkeypatch):
    """전부 실패는 '부분 수집'이 아니라 '실패'. meta 가 failed 로 기록돼야 합니다."""
    src = make_source(monkeypatch)
    respx.get("http://example.test/RTMSDataSvcAptTrade").mock(
        side_effect=httpx.ConnectTimeout("boom")
    )

    with pytest.raises(RuntimeError, match="전부 실패"):
        src.fetch()


def test_fetch_fails_fast_without_a_key(monkeypatch):
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    monkeypatch.setattr(m.storage, "load_dotenv", lambda path=None: None)
    src = m.MolitAptTrade({"base_url": "http://example.test/x", "request_delay": 0})
    with pytest.raises(RuntimeError, match="DATA_GO_KR_KEY"):
        src.fetch()


@respx.mock
def test_empty_response_yields_no_records(monkeypatch):
    src = make_source(monkeypatch)
    respx.get("http://example.test/RTMSDataSvcAptTrade").mock(
        return_value=httpx.Response(200, text=fixture("molit_apt_trade_empty.xml"))
    )

    fetched = src.fetch()
    assert src.normalize(fetched.raw) == []
    assert src.raw_record_count(fetched.raw) == 0


# ---------------------------------------------------------------------------
# series 요약
# ---------------------------------------------------------------------------


def test_series_metrics_exclude_canceled(monkeypatch):
    """취소된 신고가가 '역대 최고가'로 잡히면 안 됩니다."""
    src = make_source(monkeypatch)
    records = [
        {"region_code": "11680", "price_manwon": 100000, "area_m2": 84.9, "canceled": False},
        {"region_code": "11680", "price_manwon": 999999, "area_m2": 84.9, "canceled": True},
    ]
    metrics = src.series_metrics(records)
    assert metrics["deal_count"] == 1
    assert metrics["canceled_count"] == 1
    assert metrics["overall"]["price_manwon_max"] == 100000
