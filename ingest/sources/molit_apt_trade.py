"""국토교통부 아파트 매매 실거래가.

[확인 필요] 표시는 실제 응답으로 확인하기 전까지 남겨 둡니다.
`python -m ingest capture --source molit_apt_trade` 로 실응답을 받아
tests/fixtures/ 에 저장한 뒤 이 파일의 주석을 갱신하세요.

성격: append형 (새 거래가 계속 신고됨).
      단, 이미 신고된 거래가 '해제(취소)' 될 수 있어 _watch.canceled 로
      그 변화를 changed 로 잡습니다. "신고가가 취소됐다"는 그 자체로 신호입니다.
"""

from __future__ import annotations

import os
import statistics
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import httpx

from ingest import quality, storage
from ingest.base import FetchResult, Source, ValidationResult
from ingest.registry import register

# [확인 필요] 공공데이터포털 문서로 확정할 것.
DEFAULT_BASE_URL = (
    "http://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
)

#: data.go.kr 이 정상으로 취급하는 resultCode 값들 (서비스마다 자릿수가 다릅니다)
OK_RESULT_CODES = {"0", "00", "000", "0000"}


# ---------------------------------------------------------------------------
# 인증키
# ---------------------------------------------------------------------------


def normalize_service_key(raw_key: str) -> str:
    """인증키를 항상 '디코딩된' 형태로 되돌린다.

    여기서 시간을 제일 많이 씁니다. 결론만 적어 둡니다.

    공공데이터포털은 같은 키를 두 벌 발급합니다.
      - Encoding 키: 이미 URL 인코딩된 문자열 (%2B, %3D 등이 보임)
      - Decoding 키: 원본 (+, = 가 그대로 보임)

    httpx 는 params= 로 넘긴 값을 **다시** URL 인코딩합니다.
    Encoding 키를 그대로 넘기면 % 가 %25 로 한 번 더 인코딩돼
    SERVICE_KEY_IS_NOT_REGISTERED_ERROR 가 납니다.

    그래서 이 프로젝트는 **Decoding 키를 쓰는 것으로 통일**합니다.
    사용자가 실수로 Encoding 키를 .env 에 넣어도, % 가 보이면
    한 번 unquote 해서 Decoding 키로 되돌립니다.
    """
    key = (raw_key or "").strip()
    if "%" in key:
        return urllib.parse.unquote(key)
    return key


def read_service_key() -> str:
    storage.load_dotenv()
    key = os.environ.get("DATA_GO_KR_KEY", "")
    if not key:
        raise RuntimeError(
            "DATA_GO_KR_KEY 가 없습니다. .env 에 넣거나 환경변수로 주세요. "
            "(.env.example 참고)"
        )
    return normalize_service_key(key)


# ---------------------------------------------------------------------------
# 응답 파싱
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    """API 가 에러 응답을 준 경우 (HTTP 200 이어도)."""


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_xml_response(body: str) -> tuple[list[dict], int, str, str]:
    """XML 응답 -> (items, total_count, result_code, result_msg).

    item 은 태그명 -> 텍스트 의 평평한 dict 로만 바꿉니다.
    이름을 바꾸거나 타입을 바꾸지 않습니다 (raw/ 에 그대로 저장되므로).
    """
    root = ET.fromstring(body)

    # 게이트웨이 레벨 에러: <OpenAPI_ServiceResponse><cmmMsgHeader>...
    cmm = root.find(".//cmmMsgHeader")
    if cmm is not None:
        code = _text(cmm.find("returnReasonCode"))
        msg = _text(cmm.find("returnAuthMsg")) or _text(cmm.find("errMsg"))
        raise ApiError(f"[{code}] {msg}")

    code = _text(root.find(".//resultCode"))
    msg = _text(root.find(".//resultMsg"))
    if code and code not in OK_RESULT_CODES:
        raise ApiError(f"[{code}] {msg}")

    items: list[dict] = []
    for item in root.findall(".//items/item"):
        items.append({child.tag: (child.text or "").strip() for child in item})

    total_raw = _text(root.find(".//totalCount"))
    total = int(total_raw) if total_raw.isdigit() else len(items)
    return items, total, code, msg


def parse_json_response(payload: dict) -> tuple[list[dict], int, str, str]:
    """JSON 응답 -> (items, total_count, result_code, result_msg).

    ✅ 확인됨 (2026-08-19): `_type=json` 을 붙이면 JSON 으로 응답합니다.
    게이트웨이 레벨 에러도 JSON 으로 오는데, 이때는 봉투가 통째로 다릅니다.

        {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
            "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
            "returnAuthMsg": "등록되지 않은 서비스키",
            "returnReasonCode": "30"}}}

    이 모양을 먼저 잡지 않으면 인증 실패가 '빈 응답' 으로 둔갑합니다.
    """
    # 게이트웨이 레벨 에러 (XML 의 <cmmMsgHeader> 와 같은 것)
    gateway = payload.get("OpenAPI_ServiceResponse")
    if isinstance(gateway, dict):
        header = gateway.get("cmmMsgHeader", {}) or {}
        code = str(header.get("returnReasonCode", "")).strip()
        msg = str(header.get("returnAuthMsg") or header.get("errMsg") or "").strip()
        raise ApiError(f"[{code}] {msg}")

    resp = payload.get("response", payload)
    header = resp.get("header", {}) or {}
    code = str(header.get("resultCode", "")).strip()
    msg = str(header.get("resultMsg", "")).strip()
    if code and code not in OK_RESULT_CODES:
        raise ApiError(f"[{code}] {msg}")

    body = resp.get("body", {}) or {}
    raw_items = body.get("items") or {}
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("item") or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    items = [
        {k: ("" if v is None else str(v).strip()) for k, v in it.items()}
        for it in raw_items
    ]
    total = int(body.get("totalCount") or len(items))
    return items, total, code, msg


# ---------------------------------------------------------------------------
# 필드 매핑
# ---------------------------------------------------------------------------

#: 정규화 필드 -> 원본 태그 후보. 앞에 있는 것이 우선.
#:
#: ✅ 확정됨 (2026-08-19, LAWD_CD=11680 DEAL_YMD=202606 실응답):
#:    응답 태그는 **영문 20개**입니다.
#:      aptDong aptNm buildYear buyerGbn cdealDay cdealType dealAmount
#:      dealDay dealMonth dealYear dealingGbn estateAgentSggNm excluUseAr
#:      floor jibun landLeaseholdGbn rgstDate sggCd slerGbn umdNm
#:    `roadNm`(도로명)은 **없습니다** -- 기획서 추정과 달라 제거했습니다.
#:
#: 한글 태그는 구 서비스(RTMSOBJSvc)의 것으로, 폴백으로만 남겨 둡니다.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "region_code": ("sggCd", "지역코드"),
    "dong": ("umdNm", "법정동"),
    "apt_name": ("aptNm", "아파트"),
    "area_m2": ("excluUseAr", "전용면적"),
    "floor": ("floor", "층"),
    "built_year": ("buildYear", "건축년도"),
    "deal_year": ("dealYear", "년"),
    "deal_month": ("dealMonth", "월"),
    "deal_day": ("dealDay", "일"),
    "price_manwon": ("dealAmount", "거래금액"),
    "canceled": ("cdealType", "해제여부"),
    "canceled_date": ("cdealDay", "해제사유발생일"),
    "deal_type": ("dealingGbn", "거래유형"),
    "jibun": ("jibun", "지번"),
}

#: 응답에는 있지만 1단계에서 정규화하지 않는 태그.
#: (aptDong 은 소유권 이전 등기 완료 건만 채워지고, 나머지는 당장 쓸 데가 없습니다.)
UNUSED_TAGS = (
    "aptDong",
    "buyerGbn",
    "estateAgentSggNm",
    "landLeaseholdGbn",
    "rgstDate",
    "slerGbn",
)


def pick(item: dict, field: str) -> str:
    for tag in FIELD_ALIASES.get(field, ()):
        if tag in item:
            return (item[tag] or "").strip()
    return ""


def parse_price_manwon(value: str) -> int | None:
    """거래금액은 문자열로 옵니다. 예: 콤마와 앞쪽 공백이 붙은 " 80,000".

    콤마와 공백을 제거하고 int 로 바꿉니다. 단위는 만원입니다.
    """
    cleaned = (value or "").replace(",", "").replace(" ", "").strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_int(value: str) -> int | None:
    cleaned = (value or "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def parse_float(value: str) -> float | None:
    cleaned = (value or "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_flexible_date(value: str) -> str | None:
    """해제사유발생일 등. 표기가 흔들려서 [확인 필요] -- 여러 형태를 받습니다.

    26.08.12 / 2026.08.12 / 20260812 / 2026-08-12  ->  2026-08-12
    """
    raw = (value or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    try:
        if len(digits) == 8:
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
        if len(digits) == 6:
            # YY.MM.DD 로 가정 (20xx 년대)
            return date(
                2000 + int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
            ).isoformat()
    except ValueError:
        return None
    return None


def parse_canceled(value: str) -> bool:
    """해제여부: "O" -> true. 그 외(빈 문자열 포함) -> false."""
    return (value or "").strip().upper() == "O"


# ---------------------------------------------------------------------------
# 소스
# ---------------------------------------------------------------------------


def month_window(months: int, today: date | None = None) -> list[str]:
    """오늘 기준 최근 N개월. 예: 2026-08-19, 3 -> [2026-06, 2026-07, 2026-08]"""
    today = today or date.today()
    out: list[str] = []
    y, m = today.year, today.month
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(out)


@register
class MolitAptTrade(Source):
    name = "molit_apt_trade"
    as_of_precision = "month"
    schema_version = 1
    kind = "append"
    supports_backfill = True

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        if not cfg:
            cfg = storage.load_source_config(self.name)
        self.config = cfg

        regions_cfg = storage.load_regions_config()
        self.regions: list[dict] = regions_cfg.get("regions", []) or []
        self.rolling_months: int = int(regions_cfg.get("rolling_months", 3))

        self.base_url: str = cfg.get("base_url") or DEFAULT_BASE_URL
        self.response_format: str = (cfg.get("response_format") or "xml").lower()
        self.num_of_rows: int = int(cfg.get("num_of_rows", 1000))
        self.timeout: float = float(cfg.get("timeout_seconds", 20))
        self.max_retries: int = int(cfg.get("max_retries", 3))
        self.request_delay: float = float(cfg.get("request_delay", 0.2))
        self.backfill_delay: float = float(cfg.get("backfill_delay", 1.0))

        #: normalize() 가 채우는 통계. validate() 에서 읽습니다.
        self.last_key_collisions: int = 0
        self.last_parse_failures: list[str] = []
        #: _fetch_months() 시작 때 한 번만 읽습니다.
        self._service_key: str | None = None

    # -- HTTP ---------------------------------------------------------------

    def _request_once(
        self, client: httpx.Client, lawd_cd: str, deal_ymd: str, page: int
    ) -> tuple[list[dict], int]:
        params = {
            "serviceKey": self._service_key or read_service_key(),
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "pageNo": str(page),
            "numOfRows": str(self.num_of_rows),
        }
        if self.response_format == "json":
            params["_type"] = "json"

        resp = client.get(self.base_url, params=params, timeout=self.timeout)
        resp.raise_for_status()

        if resp.text.lstrip().startswith("{"):
            items, total, _, _ = parse_json_response(resp.json())
        else:
            items, total, _, _ = parse_xml_response(resp.text)
        return items, total

    def _fetch_one(
        self, client: httpx.Client, lawd_cd: str, deal_ymd: str, delay: float
    ) -> dict:
        """(지역 x 계약년월) 하나를 페이징 끝까지 조회한다."""
        items: list[dict] = []
        total = 0
        page = 1
        pages = 0

        while True:
            last_error: Exception | None = None
            for attempt in range(self.max_retries):
                try:
                    page_items, total = self._request_once(client, lawd_cd, deal_ymd, page)
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 -- 재시도 대상 전부
                    last_error = exc
                    if attempt < self.max_retries - 1:
                        time.sleep(2**attempt)  # 지수 백오프: 1s, 2s, 4s
            if last_error is not None:
                return {
                    "region_code": lawd_cd,
                    "deal_ymd": deal_ymd,
                    "ok": False,
                    "error": f"{type(last_error).__name__}: {last_error}",
                    "total_count": None,
                    "pages": pages,
                    "items": items,
                }

            items.extend(page_items)
            pages += 1

            # totalCount 를 보고 끝까지 돈다. numOfRows 를 크게 잡아도
            # 지역/월에 따라 넘칠 수 있습니다.
            if len(items) >= total or not page_items:
                break
            page += 1
            if delay:
                time.sleep(delay)

        return {
            "region_code": lawd_cd,
            "deal_ymd": deal_ymd,
            "ok": True,
            "error": None,
            "total_count": total,
            "pages": pages,
            "items": items,
        }

    def _fetch_months(self, months: list[str], *, delay: float) -> FetchResult:
        if not self.regions:
            raise RuntimeError("config/regions.yml 에 regions 가 비어 있습니다.")

        # 인증키는 여기서 한 번만 읽습니다. 없으면 24번 헛돌기 전에 바로 터집니다.
        self._service_key = read_service_key()

        requests_log: list[dict] = []
        failures: list[str] = []

        with httpx.Client(follow_redirects=True) as client:
            for region in self.regions:
                code = str(region["code"])
                name = region.get("name", code)
                for month in months:
                    entry = self._fetch_one(client, code, month.replace("-", ""), delay)
                    entry["region_name"] = name
                    requests_log.append(entry)
                    if not entry["ok"]:
                        failures.append(f"{name} {month} 조회 실패")
                    if delay:
                        time.sleep(delay)

        # 전부 실패했다면 '부분 수집'이 아니라 '실패'입니다.
        # (인증키 만료, 서비스 점검, 엔드포인트 변경 같은 것들.)
        # 여기서 예외를 던져야 meta 가 stale 이 아니라 failed 로 기록되고,
        # 원인이 "빈 응답" 뒤에 숨지 않습니다.
        if requests_log and len(failures) == len(requests_log):
            first_error = next(
                (r["error"] for r in requests_log if r.get("error")), "알 수 없음"
            )
            raise RuntimeError(
                f"{len(failures)}개 요청이 전부 실패했습니다. 첫 오류: {first_error}"
            )

        raw = {
            "endpoint": self.base_url,
            "response_format": self.response_format,
            "fetched_at": storage.iso_utc(),
            "months": months,
            "regions": [
                {"code": str(r["code"]), "name": r.get("name", str(r["code"]))}
                for r in self.regions
            ],
            "requests": requests_log,
        }
        return FetchResult(
            raw=raw,
            partial=bool(failures),
            notes="; ".join(failures) if failures else None,
        )

    # -- Source 인터페이스 ---------------------------------------------------

    def fetch(self) -> FetchResult:
        return self._fetch_months(month_window(self.rolling_months), delay=self.request_delay)

    def fetch_period(self, period: str) -> FetchResult:
        """백필: 계약년월 하나만. 요청 사이 지연을 크게 둡니다."""
        return self._fetch_months([period], delay=self.backfill_delay)

    def as_of(self, raw: dict) -> str:
        months = raw.get("months") or []
        if months:
            return max(months)
        return datetime.now().strftime("%Y-%m")

    def normalize(self, raw: dict) -> list[dict]:
        self.last_key_collisions = 0
        self.last_parse_failures = []

        records: list[dict] = []
        for req in raw.get("requests", []):
            if not req.get("ok"):
                continue
            fallback_region = str(req.get("region_code") or "")
            for item in req.get("items", []):
                rec = self._normalize_item(item, fallback_region)
                if rec is not None:
                    records.append(rec)

        self._resolve_key_collisions(records)
        return records

    def _resolve_key_collisions(self, records: list[dict]) -> None:
        """같은 `_key` 를 가진 레코드에 일련번호를 붙인다. 에러가 아닙니다.

        ★ 붙이는 순서가 응답 순서에 의존하면 안 됩니다.

        실데이터에서 확인된 경우
        (11680 수서동 까치마을 2026-06-20 34.44㎡ 6층 145,000):
        같은 거래가 '해제분' 과 '정상분' 으로 두 번 옵니다. 기획서의 `_key` 구성
        요소만으로는 둘이 완전히 같아서 충돌합니다.

        응답에 나온 순서대로 #2 를 붙이면, 다음 날 API 가 순서만 바꿔 줘도
        `canceled` 가 true<->false 로 뒤집힌 것처럼 보이는 **가짜 diff** 가 납니다.
        그러면 "신고가 취소" 알림이 매일 잘못 나갑니다.

        그래서 충돌 그룹 안에서는 레코드 내용 자체를 정렬 기준으로 씁니다.
        같은 응답이면 순서와 무관하게 항상 같은 `_key` 가 나옵니다.
        """
        groups: dict[str, list[dict]] = {}
        for rec in records:
            groups.setdefault(rec["_key"], []).append(rec)

        for base_key, group in groups.items():
            if len(group) == 1:
                continue
            for offset, rec in enumerate(sorted(group, key=_collision_sort_key)):
                if offset:
                    rec["_key"] = f"{base_key}#{offset + 1}"
                    self.last_key_collisions += 1

    def _normalize_item(self, item: dict, fallback_region: str) -> dict | None:
        year = parse_int(pick(item, "deal_year"))
        month = parse_int(pick(item, "deal_month"))
        day = parse_int(pick(item, "deal_day"))
        price = parse_price_manwon(pick(item, "price_manwon"))
        area = parse_float(pick(item, "area_m2"))
        floor = parse_int(pick(item, "floor"))

        if year is None or month is None or day is None:
            self.last_parse_failures.append(f"거래일 누락: {item!r}")
            return None
        try:
            deal_date = date(year, month, day).isoformat()
        except ValueError:
            self.last_parse_failures.append(f"거래일 파싱 실패: {item!r}")
            return None

        region_code = (pick(item, "region_code") or fallback_region)[:5]
        dong = pick(item, "dong")
        apt_name = pick(item, "apt_name")
        canceled = parse_canceled(pick(item, "canceled"))

        rec = {
            "region_code": region_code,
            "dong": dong,
            "apt_name": apt_name,
            "area_m2": area,
            "floor": floor,
            "built_year": parse_int(pick(item, "built_year")),
            "deal_date": deal_date,
            "price_manwon": price,
            # 해제 거래는 삭제하지 않고 플래그만 붙여 보관합니다.
            # 집계할 때 기본적으로 제외하는 건 소비자 몫입니다.
            "canceled": canceled,
            "canceled_date": parse_flexible_date(pick(item, "canceled_date")),
            "deal_type": pick(item, "deal_type") or None,
        }
        rec["_key"] = "|".join(
            [
                region_code,
                dong,
                apt_name,
                f"{area}",
                f"{floor}",
                deal_date.replace("-", ""),
                f"{price}",
            ]
        )
        rec["_watch"] = {"canceled": canceled}
        return rec

    # -- 품질 ---------------------------------------------------------------

    def validate(
        self, records: list[dict], previous: list[dict] | None
    ) -> ValidationResult:
        result = quality.run_common_checks(self, records, previous)
        if not records:
            return result

        allowed = month_window(self.rolling_months)
        result = result.merge(
            quality.check_apt_trade_records(
                records, as_of=max(allowed), allowed_months=allowed
            )
        )

        if self.last_key_collisions:
            # 에러가 아닙니다. 카운트만 기록합니다.
            result.warnings.append(
                f"_key 충돌 {self.last_key_collisions}건 (일련번호 부여)"
            )
        if self.last_parse_failures:
            result.warnings.append(
                f"거래일 파싱 실패로 버린 레코드 {len(self.last_parse_failures)}건"
            )
        return result

    # -- series -------------------------------------------------------------

    # -- DB 매핑 -------------------------------------------------------------

    db_table = "mkt_apt_trade"
    db_conflict_key = "key"
    db_event_table = "mkt_apt_trade_event"

    def db_rows(self, records: list[dict], *, collected_at: str) -> list[dict]:
        return [
            {
                "key": r["_key"],
                "region_code": r["region_code"],
                "dong": r["dong"],
                "apt_name": r["apt_name"],
                "area_m2": r["area_m2"],
                "floor": r["floor"],
                "built_year": r["built_year"],
                "deal_date": r["deal_date"],
                "price_manwon": r["price_manwon"],
                "canceled": r["canceled"],
                "canceled_date": r["canceled_date"],
                "deal_type": r["deal_type"],
                # 갱신될 때 더 이른 값이 지켜집니다 (DB 트리거).
                "first_seen_at": collected_at,
                "last_seen_at": collected_at,
            }
            for r in records
        ]

    def db_event_rows(
        self, diff: dict, *, observed_on: str, observed_at: str
    ) -> list[dict]:
        base = {"observed_on": observed_on, "observed_at": observed_at}
        rows: list[dict] = []
        for entry in diff.get("added", []):
            rows.append({**base, "event": "added", "key": entry["_key"], "record": entry["record"]})
        for entry in diff.get("removed", []):
            # 실거래가에서 removed 는 거의 안 나와야 정상입니다.
            # 행을 지우지는 않습니다 -- 사라진 이유를 모르는 채 지우면 복구가 안 됩니다.
            rows.append({**base, "event": "removed", "key": entry["_key"], "record": entry["record"]})
        for entry in diff.get("changed", []):
            rows.append(
                {
                    **base,
                    "event": "changed",
                    "key": entry["_key"],
                    "field": entry["field"],
                    "before_value": entry["before"],
                    "after_value": entry["after"],
                }
            )
        return rows

    def series_points(self, records: list[dict], as_of: str) -> list[dict]:
        """계약년월별로 점을 나눈다.

        일일 수집은 최근 3개월을 한 번에 받습니다. 이걸 점 하나에 넣으면
        그 점만 3개월치가 돼서 그래프에 가짜 급등이 생깁니다.
        (백필은 한 달씩 넣으므로 나란히 놓으면 바로 어긋납니다.)

        대신 계약년월로 쪼개면, 같은 달을 여러 시점에 잰 기록이 쌓입니다.
        "2026-06 을 7월에 쟀을 때 1,394건 / 8월에 쟀을 때 1,412건" 처럼요.
        신고 지연이 얼마나 메워지는지가 그대로 보입니다 -- 이 프로젝트가
        원래 만들고 싶었던 시계열이 이쪽입니다.
        """
        by_month: dict[str, list[dict]] = {}
        for rec in records:
            deal_date = rec.get("deal_date") or ""
            if len(deal_date) >= 7:
                by_month.setdefault(deal_date[:7], []).append(rec)

        if not by_month:
            return super().series_points(records, as_of)

        return [
            {
                "as_of": month,
                "record_count": len(group),
                "metrics": self.series_metrics(group),
            }
            for month, group in sorted(by_month.items())
        ]

    def series_metrics(self, records: list[dict]) -> dict[str, Any]:
        """시점별 요약. 취소된 거래는 집계에서 제외합니다.

        (제외하지 않으면 취소된 신고가가 "역대 최고가"로 잡힙니다.)
        """
        live = [r for r in records if not r.get("canceled")]
        by_region: dict[str, list[dict]] = {}
        for rec in live:
            by_region.setdefault(rec.get("region_code", "?"), []).append(rec)

        return {
            "deal_count": len(live),
            "canceled_count": len(records) - len(live),
            "overall": _price_stats(live),
            "by_region": {
                code: dict({"deal_count": len(rs)}, **_price_stats(rs))
                for code, rs in sorted(by_region.items())
            },
        }


def _collision_sort_key(record: dict) -> tuple:
    """`_key` 충돌 그룹 안의 정렬 기준. **해제된 것이 먼저**입니다.

    ★ 순서를 왜 이렇게 잡았는지가 중요합니다.

    실데이터 (11530 신도림동 우성2 2026-07-08 84.82㎡ 5층 122,000):
        8/19  해제된 신고 1건
        8/20  해제된 신고 1건 + 새로 들어온 정상 신고 1건   <- 재신고

    사실은 "재신고가 들어왔다" 인데, 정상분을 먼저 정렬하면 기준 `_key` 가
    어제의 해제분에서 오늘의 정상분으로 옮겨 갑니다. 그러면 diff 가
    `canceled: true -> false` 로 나와 **"취소가 풀렸다"** 로 읽힙니다.
    알림 봇이 정반대로 알리게 됩니다.

    해제분을 먼저 두면:
        기준 `_key`  해제분 그대로 -> 변화 없음     (맞음)
        `#2`         정상분 -> added               (맞음: 재신고)

    해제는 되돌아가지 않는 상태라, 먼저 붙잡아 두는 쪽이 안정적입니다.
    (거래 하나가 나중에 해제되는 보통의 경우는 그룹이 1건이라 영향 없습니다 --
     `canceled: false -> true` 로 정상 기록됩니다.)
    """
    return (
        0 if record.get("canceled") else 1,
        record.get("canceled_date") or "",
        record.get("deal_type") or "",
        record.get("built_year") or 0,
    )


def _price_stats(records: Iterable[dict]) -> dict[str, Any]:
    records = list(records)
    prices = [
        r["price_manwon"] for r in records if isinstance(r.get("price_manwon"), int)
    ]
    areas = [
        float(r["area_m2"])
        for r in records
        if isinstance(r.get("area_m2"), (int, float))
    ]
    if not prices:
        return {
            "price_manwon_median": None,
            "price_manwon_mean": None,
            "price_manwon_min": None,
            "price_manwon_max": None,
            "area_m2_median": None,
        }
    return {
        "price_manwon_median": int(statistics.median(prices)),
        "price_manwon_mean": int(statistics.fmean(prices)),
        "price_manwon_min": min(prices),
        "price_manwon_max": max(prices),
        "area_m2_median": round(statistics.median(areas), 2) if areas else None,
    }


def _molit_validate_backfill(
    self: MolitAptTrade, records: list[dict], period: str
) -> ValidationResult:
    """백필은 조회한 월이 롤링 윈도우 밖이라 allowed_months 를 그 달로 고정합니다."""
    result = quality.run_common_checks(self, records, None)
    if not records:
        return result
    result = result.merge(
        quality.check_apt_trade_records(records, as_of=period, allowed_months=[period])
    )
    if self.last_key_collisions:
        result.warnings.append(
            f"_key 충돌 {self.last_key_collisions}건 (일련번호 부여)"
        )
    return result


MolitAptTrade.validate_backfill = _molit_validate_backfill  # type: ignore[assignment]
