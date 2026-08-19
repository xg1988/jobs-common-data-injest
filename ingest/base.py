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
