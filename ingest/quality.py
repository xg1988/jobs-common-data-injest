"""품질 검사 규칙 (공통 + 소스별).

이 모듈의 목적은 하나입니다: **오래된 데이터가 틀린 데이터보다 낫다.**
검사에 걸리면 latest 를 덮어쓰지 않고 quarantine/ 으로 보냅니다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ingest.base import ValidationResult

# ---- 공통 임계값 ----------------------------------------------------------

#: 전일 대비 레코드 수 변동 허용폭
RECORD_COUNT_CHANGE_LIMIT = 0.30
#: 레코드 단위 격리가 이 비율을 넘으면 파싱이 깨진 것으로 보고 전체 격리
QUARANTINE_RATIO_LIMIT = 0.05
#: removed 가 전체의 이 비율을 넘으면 경고 (조회 범위가 어긋난 신호)
REMOVED_RATIO_WARN = 0.05

# ---- 실거래가 전용 범위 ---------------------------------------------------

PRICE_MANWON_MIN = 1_000          # 1천만원
PRICE_MANWON_MAX = 10_000_000     # 1000억원
AREA_M2_MIN = 10.0
AREA_M2_MAX = 500.0
FLOOR_MIN = -5
FLOOR_MAX = 100


def _q(record: dict, reason: str) -> dict:
    return {"reason": reason, "record": record}


# ---- 공통 규칙 ------------------------------------------------------------


def run_common_checks(
    source: Any,
    records: list[dict],
    previous: list[dict] | None,
) -> ValidationResult:
    """모든 소스에 적용되는 검사.

    빈 응답은 '에러'가 아니라 '쓰지 말 것'입니다. ok=False 로 두고
    pipeline 이 latest 를 유지하도록 합니다.
    """
    errors: list[str] = []
    warnings: list[str] = []
    quarantine: list[dict] = []

    # 1) _key 누락 -- 하나라도 없으면 diff 가 성립하지 않으므로 에러
    missing_key = [r for r in records if not r.get("_key")]
    if missing_key:
        errors.append(f"_key 누락 레코드 {len(missing_key)}건")
        quarantine.extend(_q(r, "missing_key") for r in missing_key[:100])

    # 2) 빈 응답
    if len(records) == 0:
        errors.append("빈 응답 (record_count == 0)")
        return ValidationResult(
            ok=False, errors=errors, warnings=warnings, quarantine=quarantine
        )

    # 3) 레코드 수 급변
    if previous:
        prev_n = len(previous)
        curr_n = len(records)
        if prev_n > 0:
            delta = (curr_n - prev_n) / prev_n
            if abs(delta) > RECORD_COUNT_CHANGE_LIMIT:
                errors.append(
                    f"레코드 수 급변: {prev_n} -> {curr_n} ({delta:+.1%}, "
                    f"허용 ±{RECORD_COUNT_CHANGE_LIMIT:.0%})"
                )

    return ValidationResult(
        ok=not errors, errors=errors, warnings=warnings, quarantine=quarantine
    )


def check_as_of_regression(previous_as_of: str | None, new_as_of: str) -> ValidationResult:
    """기준 시점이 과거로 가면 격리.

    문자열 비교로 충분합니다 -- as_of 는 'YYYY-MM' / 'YYYY-MM-DD' /
    'YYYY-Qn' 처럼 사전순 = 시간순인 형식만 씁니다.
    """
    if previous_as_of and new_as_of < previous_as_of:
        return ValidationResult(
            ok=False,
            errors=[f"as_of 역행: {previous_as_of} -> {new_as_of}"],
        )
    return ValidationResult(ok=True)


def apply_quarantine_ratio_rule(result: ValidationResult, total: int) -> ValidationResult:
    """레코드 단위 격리가 5% 를 넘으면 전체를 격리한다."""
    if total <= 0 or not result.quarantine:
        return result
    ratio = len(result.quarantine) / total
    if ratio > QUARANTINE_RATIO_LIMIT:
        result.ok = False
        result.errors.append(
            f"격리 비율 {ratio:.1%} > {QUARANTINE_RATIO_LIMIT:.0%} "
            f"({len(result.quarantine)}/{total}) -- 파싱 로직이 깨진 신호"
        )
    return result


def check_removed_ratio(diff: dict, total: int) -> list[str]:
    """removed 급증 경고. 실거래가에서 removed 는 거의 안 나와야 정상."""
    removed = diff.get("summary", {}).get("removed", 0)
    if total > 0 and removed / total > REMOVED_RATIO_WARN:
        return [
            f"removed 급증: {removed}/{total} = {removed / total:.1%} "
            f"> {REMOVED_RATIO_WARN:.0%}"
        ]
    return []


# ---- 실거래가 전용 규칙 ---------------------------------------------------


def check_apt_trade_records(
    records: list[dict],
    *,
    as_of: str,
    allowed_months: list[str] | None = None,
    today: date | None = None,
) -> ValidationResult:
    """범위를 벗어난 레코드만 격리하고 나머지는 통과시킨다.

    `allowed_months` 는 이번 수집에서 실제로 조회한 계약년월 목록(YYYY-MM)입니다.
    기획서 11-2 는 "거래일이 as_of 월과 일치"라고 썼지만, 실제 수집은
    롤링 윈도우(최근 3개월)라 한 달에 고정할 수 없습니다. 그래서
    '조회한 월 집합 안에 있을 것' 으로 완화했습니다.
    """
    today = today or datetime.now().date()
    passed: list[dict] = []
    quarantine: list[dict] = []
    warnings: list[str] = []

    for rec in records:
        reasons: list[str] = []

        price = rec.get("price_manwon")
        if not isinstance(price, int) or not (
            PRICE_MANWON_MIN <= price <= PRICE_MANWON_MAX
        ):
            reasons.append(f"price_manwon 범위 이탈: {price!r}")

        area = rec.get("area_m2")
        if not isinstance(area, (int, float)) or not (
            AREA_M2_MIN <= float(area) <= AREA_M2_MAX
        ):
            reasons.append(f"area_m2 범위 이탈: {area!r}")

        floor = rec.get("floor")
        if not isinstance(floor, int) or not (FLOOR_MIN <= floor <= FLOOR_MAX):
            reasons.append(f"floor 범위 이탈: {floor!r}")

        deal_date = rec.get("deal_date")
        if not isinstance(deal_date, str) or len(deal_date) != 10:
            reasons.append(f"deal_date 형식 오류: {deal_date!r}")
        else:
            try:
                parsed = date.fromisoformat(deal_date)
            except ValueError:
                reasons.append(f"deal_date 파싱 실패: {deal_date!r}")
            else:
                if parsed > today:
                    reasons.append(f"deal_date 미래 날짜: {deal_date}")
                elif allowed_months and deal_date[:7] not in allowed_months:
                    reasons.append(
                        f"deal_date 가 조회 월 밖: {deal_date[:7]} "
                        f"(조회: {', '.join(allowed_months)})"
                    )

        if reasons:
            quarantine.append(_q(rec, "; ".join(reasons)))
        else:
            passed.append(rec)

    result = ValidationResult(
        ok=True, errors=[], warnings=warnings, quarantine=quarantine
    )
    if quarantine:
        result.warnings.append(
            f"범위 이탈 레코드 {len(quarantine)}건 격리 (as_of={as_of})"
        )
    return apply_quarantine_ratio_rule(result, len(records))


def partition(records: list[dict], quarantine: list[dict]) -> list[dict]:
    """격리된 레코드를 제외한 나머지를 돌려준다."""
    bad_ids = {id(entry["record"]) for entry in quarantine}
    return [r for r in records if id(r) not in bad_ids]
