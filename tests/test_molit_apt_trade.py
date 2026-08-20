"""소스 ①: 국토교통부 아파트 매매 실거래가 — 정규화 · 페이징 · 인증키.

fixture 는 전부 **실응답**입니다 (2026-08-19 캡처).
tests/fixtures/README.md 에 어떤 조건으로 받았는지 적혀 있습니다.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from conftest import fixture

from ingest.sources import molit_apt_trade as m

#: 실응답을 받은 시점의 롤링 윈도우. 벽시계에 흔들리지 않게 고정합니다.
WINDOW = ["2026-06", "2026-07", "2026-08"]


def make_source(monkeypatch, **overrides):
    monkeypatch.setenv("DATA_GO_KR_KEY", "test-decoded-key")
    monkeypatch.setattr(m, "month_window", lambda n, today=None: list(WINDOW[-n:]))
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


def items_of(name: str) -> list[dict]:
    return m.parse_xml_response(fixture(name))[0]


def raw_from_items(items: list[dict], months=("2026-06",)) -> dict:
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
                "deal_ymd": "202606",
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
# 응답 파싱 (전부 실응답)
# ---------------------------------------------------------------------------


def test_response_uses_english_tags():
    """실응답으로 확정: 태그는 영문이고 roadNm 은 없다 (기획서 추정과 다름)."""
    tags = set(items_of("molit_apt_trade_page1.xml")[0])
    assert {"aptNm", "umdNm", "excluUseAr", "dealAmount", "cdealType", "sggCd"} <= tags
    assert "roadNm" not in tags
    assert not tags & {"아파트", "법정동", "거래금액"}


def test_every_normalized_field_has_a_matching_tag():
    """FIELD_ALIASES 에 실응답에 없는 필드가 남아 있으면 실패."""
    tags = set(items_of("molit_apt_trade_page1.xml")[0])
    for field, candidates in m.FIELD_ALIASES.items():
        assert tags & set(candidates), f"{field} 에 대응하는 태그가 실응답에 없음"


def test_auth_failure_xml_is_an_api_error():
    with pytest.raises(m.ApiError) as exc:
        m.parse_xml_response(fixture("molit_apt_trade_authfail.xml"))
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(exc.value)


def test_auth_failure_json_is_an_api_error():
    """JSON 에러 봉투는 모양이 다릅니다. 놓치면 인증 실패가 '빈 응답'이 됩니다."""
    payload = json.loads(fixture("molit_apt_trade_authfail.json"))
    with pytest.raises(m.ApiError) as exc:
        m.parse_json_response(payload)
    assert "등록되지 않은 서비스키" in str(exc.value)
    assert "[30]" in str(exc.value)


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
    records = src.normalize(raw_from_items(items_of("molit_apt_trade_page1.xml")))

    assert len(records) == 2
    assert records[0] == {
        "region_code": "11680",
        "dong": "수서동",
        "apt_name": "까치마을",
        "area_m2": 34.44,
        "floor": 6,
        "built_year": 1993,
        "deal_date": "2026-06-20",
        "price_manwon": 145000,
        "canceled": False,
        "canceled_date": None,
        "deal_type": "중개거래",
        "_key": "11680|수서동|까치마을|34.44|6|20260620|145000",
        "_watch": {"canceled": False},
    }


def test_price_string_is_parsed_to_int(monkeypatch):
    """거래금액은 콤마가 붙은 문자열로 온다. 단위는 만원."""
    assert m.parse_price_manwon(" 80,000") == 80000  # 앞 공백까지 방어
    assert m.parse_price_manwon("") is None

    src = make_source(monkeypatch)
    records = src.normalize(raw_from_items(items_of("molit_apt_trade_page1.xml")))
    assert [r["price_manwon"] for r in records] == [145000, 143000]
    assert all(isinstance(r["price_manwon"], int) for r in records)


def test_canceled_trade_is_kept_not_deleted(monkeypatch):
    """해제 거래를 삭제하지 말고 플래그만 붙여 보관한다."""
    src = make_source(monkeypatch)
    records = src.normalize(raw_from_items(items_of("molit_apt_trade_canceled.xml")))

    assert len(records) == 4  # 하나도 안 버립니다
    canceled = [r for r in records if r["canceled"]]
    assert len(canceled) == 2
    assert canceled[0]["apt_name"] == "신동아"
    # 실응답의 해제일 표기는 YY.MM.DD 입니다.
    assert canceled[0]["canceled_date"] == "2026-07-25"
    assert canceled[0]["_watch"] == {"canceled": True}


def test_flexible_date_covers_the_real_format():
    assert m.parse_flexible_date("26.07.25") == "2026-07-25"  # 실응답 표기
    assert m.parse_flexible_date("2026.07.25") == "2026-07-25"
    assert m.parse_flexible_date("20260725") == "2026-07-25"
    assert m.parse_flexible_date(" ") is None
    assert m.parse_flexible_date("이상한값") is None


def test_blank_padded_tags_become_none(monkeypatch):
    """빈 태그가 공백 한 칸(`<cdealDay> </cdealDay>`)으로 옵니다."""
    src = make_source(monkeypatch)
    records = src.normalize(raw_from_items(items_of("molit_apt_trade_page1.xml")))
    assert records[0]["canceled"] is False
    assert records[0]["canceled_date"] is None


# ---------------------------------------------------------------------------
# _key 충돌 — 실데이터에 실제로 있습니다
# ---------------------------------------------------------------------------


def test_key_collision_gets_serial_number_not_an_error(monkeypatch):
    """같은 거래가 해제분·정상분으로 두 번 오면 `_key` 가 겹칩니다.

    실응답 예: 11680 수서동 까치마을 2026-06-20 34.44㎡ 6층 145,000
    """
    src = make_source(monkeypatch)
    records = src.normalize(raw_from_items(items_of("molit_apt_trade_canceled.xml")))

    keys = [r["_key"] for r in records]
    assert len(set(keys)) == len(keys)  # 전부 고유
    assert src.last_key_collisions == 1
    assert sum(1 for k in keys if "#" in k) == 1

    src.rolling_months = 3  # 실응답이 2026-06 이므로 윈도우를 3개월로
    result = src.validate(records, None)
    assert result.errors == []  # 충돌은 에러가 아닙니다
    assert any("_key 충돌 1건" in w for w in result.warnings)


def test_key_assignment_does_not_depend_on_response_order(monkeypatch):
    """응답 순서가 바뀌어도 같은 레코드에 같은 `_key` 가 붙어야 합니다.

    순서에 의존하면 API 가 순서만 바꿔 줘도 canceled 가 true<->false 로
    뒤집힌 가짜 diff 가 나고, "신고가 취소" 알림이 매일 잘못 나갑니다.
    """
    src = make_source(monkeypatch)
    items = items_of("molit_apt_trade_canceled.xml")

    forward = src.normalize(raw_from_items(items))
    backward = src.normalize(raw_from_items(list(reversed(items))))

    def key_of(records, *, canceled):
        hits = [
            r["_key"]
            for r in records
            if r["apt_name"] == "까치마을" and r["canceled"] is canceled
        ]
        assert len(hits) == 1
        return hits[0]

    assert key_of(forward, canceled=True) == key_of(backward, canceled=True)
    assert key_of(forward, canceled=False) == key_of(backward, canceled=False)
    # 해제분이 기준 키를 갖습니다 (아래 재신고 테스트 참고).
    assert not key_of(forward, canceled=True).endswith("#2")
    assert key_of(forward, canceled=False).endswith("#2")


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
# 페이징 (HTTP 모킹 — 실제 호출은 하지 않습니다)
# ---------------------------------------------------------------------------


@respx.mock
def test_pagination_collects_every_page(monkeypatch):
    """totalCount 를 보고 끝까지 돈다. numOfRows=2, totalCount=4 -> 2페이지."""
    src = make_source(monkeypatch)
    # 실응답의 totalCount(223)를 4로 줄여 2페이지에서 끝나게 합니다.
    def trim(name):
        return fixture(name).replace(
            "<totalCount>223</totalCount>", "<totalCount>4</totalCount>"
        )

    route = respx.get("http://example.test/RTMSDataSvcAptTrade").mock(
        side_effect=[
            httpx.Response(200, text=trim("molit_apt_trade_page1.xml")),
            httpx.Response(200, text=trim("molit_apt_trade_page2.xml")),
        ]
    )

    fetched = src.fetch()

    assert route.call_count == 2
    assert fetched.partial is False
    entry = fetched.raw["requests"][0]
    assert entry["pages"] == 2
    assert len(entry["items"]) == 4
    assert src.raw_record_count(fetched.raw) == 4

    records = src.normalize(fetched.raw)
    assert [r["apt_name"] for r in records] == [
        "까치마을",
        "우민",
        "삼성동롯데아파트",
        "한신(개포)",
    ]

    q = route.calls[1].request.url.params
    assert q["pageNo"] == "2"
    assert q["LAWD_CD"] == "11680"
    assert q["serviceKey"] == "test-decoded-key"  # 디코딩된 값


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


@respx.mock
def test_empty_response_yields_no_records(monkeypatch):
    src = make_source(monkeypatch)
    respx.get("http://example.test/RTMSDataSvcAptTrade").mock(
        return_value=httpx.Response(200, text=fixture("molit_apt_trade_empty.xml"))
    )

    fetched = src.fetch()
    assert src.normalize(fetched.raw) == []
    assert src.raw_record_count(fetched.raw) == 0


def test_fetch_fails_fast_without_a_key(monkeypatch):
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    monkeypatch.setattr(m.storage, "load_dotenv", lambda path=None: None)
    src = m.MolitAptTrade({"base_url": "http://example.test/x", "request_delay": 0})
    with pytest.raises(RuntimeError, match="DATA_GO_KR_KEY"):
        src.fetch()


# ---------------------------------------------------------------------------
# series 요약
# ---------------------------------------------------------------------------


def test_series_metrics_exclude_canceled(monkeypatch):
    """취소된 신고가가 '역대 최고가'로 잡히면 안 됩니다."""
    src = make_source(monkeypatch)
    records = src.normalize(raw_from_items(items_of("molit_apt_trade_canceled.xml")))
    metrics = src.series_metrics(records)

    assert metrics["deal_count"] == 2
    assert metrics["canceled_count"] == 2
    # 취소분(183,500)이 빠지고 정상분 중 최고가만 남습니다.
    assert metrics["overall"]["price_manwon_max"] == 145000


def test_series_is_split_by_deal_month(monkeypatch):
    """롤링 윈도우로 여러 달을 한 번에 받아도 점은 달마다 따로 찍혀야 합니다.

    한 점에 몰아넣으면 그 점만 3개월치가 돼서, 백필 점(한 달치)과 나란히
    놓았을 때 그래프에 가짜 급등이 생깁니다.
    """
    src = make_source(monkeypatch)
    june = items_of("molit_apt_trade_page1.xml")  # 2026-06 2건
    july = [dict(it, dealMonth="7") for it in items_of("molit_apt_trade_page2.xml")]

    points = src.series_points(src.normalize(raw_from_items(june + july)), "2026-07")

    assert [p["as_of"] for p in points] == ["2026-06", "2026-07"]
    assert [p["record_count"] for p in points] == [2, 2]
    assert points[0]["metrics"]["overall"]["price_manwon_max"] == 145000


def test_rereport_after_cancellation_is_added_not_uncanceled(monkeypatch):
    """해제된 신고가 남아 있는 채로 정상 신고가 새로 들어오는 경우.

    실데이터 (11530 신도림동 우성2 2026-07-08 84.82제곱미터 5층 122,000):
        어제  해제분 1건
        오늘  해제분 1건 + 정상분 1건   <- 재신고

    사실은 "재신고 1건 추가" 입니다. 정상분이 기준 키를 가져가면 diff 가
    `canceled: true -> false` 로 나와 "취소가 풀렸다" 로 읽히고,
    알림 봇이 정반대로 알립니다.
    """
    from ingest import differ

    src = make_source(monkeypatch)
    items = items_of("molit_apt_trade_canceled.xml")
    # 같은 거래의 해제분/정상분 짝을 고릅니다 (까치마을).
    canceled_item = next(i for i in items if i["cdealType"].strip() == "O"
                         and i["aptNm"] == "까치마을")
    normal_item = next(i for i in items if i["cdealType"].strip() == ""
                       and i["aptNm"] == "까치마을")

    yesterday = src.normalize(raw_from_items([canceled_item]))
    today = src.normalize(raw_from_items([canceled_item, normal_item]))

    diff = differ.diff_records(
        yesterday, today, source="molit_apt_trade", from_label="d1", to_label="d2"
    )

    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 0}
    assert diff["added"][0]["record"]["canceled"] is False  # 재신고분이 added


# ---------------------------------------------------------------------------
# 조용한 0건 — 행정구역 개편으로 지역코드가 낡으면 이렇게 됩니다
# ---------------------------------------------------------------------------


@respx.mock
def test_stale_region_code_returns_empty_not_an_error(monkeypatch):
    """옛 지역코드는 에러가 아니라 **0건**을 돌려줍니다.

    2026-08 실제 사례: 광주(29)+전남(46)이 전남광주통합특별시(12)로
    통합되면서 옛 코드 25개가 전부 조용히 0건이 됐습니다. 응답은
    resultCode=000, 즉 "정상" 입니다. 이걸 못 잡으면 월 2,630건이
    빠진 채 "전국 수집 완료" 로 보입니다.
    """
    src = make_source(monkeypatch)
    src.regions = [
        {"code": "11680", "name": "서울 강남구"},   # 살아있는 코드
        {"code": "29110", "name": "광주 동구"},     # 폐지된 코드
    ]

    empty = fixture("molit_apt_trade_empty.xml")
    live = fixture("molit_apt_trade_page1.xml")

    def route(request):
        code = request.url.params.get("LAWD_CD")
        return httpx.Response(200, text=empty if code == "29110" else live)

    respx.get(url__startswith="http://example.test").mock(side_effect=route)

    raw = src.fetch()
    assert src.last_empty_regions == ["광주 동구(29110)"]
    # 강남구는 데이터가 왔으니 지목되면 안 됩니다.
    assert not any("강남" in r for r in src.last_empty_regions)

    src.rolling_months = 3
    result = src.validate(src.normalize(raw.raw), None)
    assert result.errors == []          # 0건 지역은 에러가 아닙니다
    assert any("행정구역 개편" in w for w in result.warnings)


@respx.mock
def test_failed_request_is_not_reported_as_empty_region(monkeypatch):
    """조회가 **실패**한 지역은 '0건 지역' 이 아닙니다.

    둘을 섞으면 일시적인 네트워크 오류가 "코드가 폐지됐다" 로 둔갑해
    멀쩡한 지역을 목록에서 지우게 됩니다.
    """
    src = make_source(monkeypatch)
    src.regions = [{"code": "11680", "name": "서울 강남구"}]
    respx.get(url__startswith="http://example.test").mock(
        side_effect=httpx.ConnectError("일시적 오류")
    )

    with pytest.raises(RuntimeError):   # 전부 실패 = 수집 실패
        src.fetch()
    assert src.last_empty_regions == []
