"""소스 어댑터 인터페이스와 공통 타입.

새 소스는 Source 를 상속해 fetch / normalize / as_of 세 개만 구현하면 붙습니다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FetchResult:
    """원본 응답. 가공 금지."""

    raw: dict
    partial: bool = False
    notes: str | None = None


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)

    def merge(self, other: ValidationResult) -> ValidationResult:
        return ValidationResult(
            ok=self.ok and other.ok,
            errors=[*self.errors, *other.errors],
            warnings=[*self.warnings, *other.warnings],
            quarantine=[*self.quarantine, *other.quarantine],
        )


class Source(ABC):
    """수집 대상 하나."""

    name: str
    as_of_precision: str  # "day" | "month" | "quarter"
    schema_version: int = 1

    #: "append" (새 레코드가 계속 쌓임) | "update" (같은 레코드의 값이 바뀜)
    #: diff 엔진 자체는 둘 다 같은 방식으로 처리하지만, 품질 검사와
    #: 리포팅에서 기대치가 다릅니다.
    kind: str = "append"

    @abstractmethod
    def fetch(self) -> FetchResult:
        """원본을 그대로 반환한다. 가공하지 않는다."""

    @abstractmethod
    def normalize(self, raw: dict) -> list[dict]:
        """레코드 리스트로 변환. 각 레코드에 _key, _watch 포함."""

    @abstractmethod
    def as_of(self, raw: dict) -> str:
        """데이터 기준 시점을 추출한다."""

    #: pipeline 이 실행 중에 채웁니다. 조회 범위(지역 목록 등)가 지난번과
    #: 달라졌다는 뜻입니다. 이때는 레코드 수가 크게 변하는 게 정상입니다.
    scope_changed: bool = False

    def scope(self) -> dict | None:
        """이번 수집이 **무엇을** 조회했는지. 기본은 없음.

        여기에 조회 범위를 적어 두면, 다음 실행 때 '범위가 바뀐 것' 과
        'API 가 깨진 것' 을 구분할 수 있습니다. 둘 다 레코드 수를 크게
        바꾸지만 대응은 정반대입니다 -- 하나는 정상, 하나는 사고입니다.
        """
        return None

    def validate(
        self, records: list[dict], previous: list[dict] | None
    ) -> ValidationResult:
        """소스별 품질 검사. 기본 구현은 공통 규칙만 적용."""
        from ingest import quality

        return quality.run_common_checks(self, records, previous)

    def series_metrics(self, records: list[dict]) -> dict[str, Any]:
        """series/ 에 남길 요약 지표. 기본은 없음.

        series 에 레코드 전체를 쌓으면 파일이 금방 못 쓸 크기가 됩니다.
        시점별 요약만 남기고, 레코드 원본이 필요하면 raw/ 를 봅니다.
        """
        return {}

    def series_points(self, records: list[dict], as_of: str) -> list[dict]:
        """이번 수집을 series 의 점 몇 개로 나눌지 정한다.

        ★ 한 점 = 한 기준시점(as_of) 이어야 합니다.

        실거래가처럼 롤링 윈도우로 여러 달을 한 번에 받는 소스는, 받은 걸
        통째로 한 점에 넣으면 안 됩니다. 그러면 그 점만 3개월치가 돼서
        그래프에 가짜 급등이 생깁니다. (백필은 한 달씩 넣으므로 더 어긋납니다.)

        기본 구현은 소스가 한 시점만 받는다고 보고 점 하나를 만듭니다.
        여러 시점을 한 번에 받는 소스는 이걸 오버라이드하세요.
        """
        return [
            {
                "as_of": as_of,
                "record_count": len(records),
                "metrics": self.series_metrics(records),
            }
        ]

    # ---- DB 매핑 (선택 구현) ----------------------------------------------
    #
    # 파일 저장은 모든 소스가 공통이지만, DB 테이블은 소스마다 컬럼이 다릅니다.
    # 아래 셋을 채우면 pipeline 이 알아서 밀어 넣습니다. 안 채우면 파일만 씁니다.

    #: 정규화 레코드가 들어갈 테이블 (latest 에 해당)
    db_table: str | None = None
    #: 위 테이블의 유니크 키 컬럼 (upsert 기준)
    db_conflict_key: str = "key"
    #: diff 를 쌓을 이벤트 테이블 (선택)
    db_event_table: str | None = None

    def db_rows(self, records: list[dict], *, collected_at: str) -> list[dict]:
        """레코드를 db_table 의 행으로 바꾼다."""
        return []

    def db_event_rows(
        self, diff: dict, *, observed_on: str, observed_at: str
    ) -> list[dict]:
        """diff 를 db_event_table 의 행으로 바꾼다."""
        return []

    # ---- 백필 (선택 구현) -------------------------------------------------

    supports_backfill: bool = False

    def fetch_period(self, period: str) -> FetchResult:
        """백필용. 단일 기준 시점(예: "2024-03")만 조회한다."""
        raise NotImplementedError(
            f"{self.name} 소스는 백필을 지원하지 않습니다."
        )

    # ---- 저장 보조 --------------------------------------------------------

    def raw_record_count(self, raw: dict) -> int:
        """raw 봉투의 record_count. 원본 항목이 몇 개 들어왔는지.

        정규화 후 개수와 다를 수 있습니다 (파싱 실패분이 빠지므로).
        기본 구현은 흔한 두 모양을 처리합니다.
        """
        if isinstance(raw.get("requests"), list):
            return sum(len(r.get("items") or []) for r in raw["requests"])
        if isinstance(raw.get("items"), list):
            return len(raw["items"])
        return 0

    def validate_backfill(self, records: list[dict], period: str) -> ValidationResult:
        """백필용 검사. 이전 스냅샷이 없으므로 전일 대비 검사는 건너뜁니다."""
        return self.validate(records, None)
