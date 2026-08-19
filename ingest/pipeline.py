"""fetch -> raw 저장 -> normalize -> validate -> diff -> write.

실행 순서 (기획서 10장)

    1. fetch()                    실패 시 재시도 3회 (어댑터 안에서)
    2. raw/ 에 원본 저장           실패해도 여기까지는 남긴다
    3. normalize()
    4. validate()                 이전 latest 와 비교
    5. 검사 실패?
         yes -> quarantine/ 에 저장, meta(status=quarantined),
                ★ latest 를 덮어쓰지 않고 종료
         no  -> 계속
    6. diff 계산 (이전 latest vs 새 records)
    7. latest/ 덮어쓰기
    8. series/ 에 append
    9. diff/ 에 저장
   10. meta.json 갱신 (status=ok)
   11. git commit & push          <- Actions 워크플로가 담당

5번이 이 설계의 핵심입니다. 오래된 데이터가 틀린 데이터보다 낫습니다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ingest import differ, meta, quality, storage
from ingest.base import Source, ValidationResult

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class RunResult:
    source: str
    status: str  # ok | stale | quarantined | failed
    record_count: int = 0
    quarantined_count: int = 0
    as_of: str | None = None
    partial: bool = False
    diff_summary: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _rel(path) -> str:
    try:
        return str(path.relative_to(storage.ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def run_source(
    source: Source,
    *,
    run_date: str | None = None,
    dry_run: bool = False,
    log: Logger = _noop,
) -> RunResult:
    name = source.name
    run_date = run_date or storage.today_str()
    result = RunResult(source=name, status="failed")

    source_cfg = storage.load_source_config(name)
    # partial 수집을 소비 금지(stale)로 볼지. 기획서 7-1: "partial true면 쓰면 안 됨".
    partial_marks_stale = bool(source_cfg.get("partial_marks_stale", True))

    # ---- 1. fetch --------------------------------------------------------
    log(f"[{name}] fetch...")
    try:
        fetched = source.fetch()
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        log(f"[{name}] fetch 실패 -- {msg}")
        result.errors.append(msg)
        if not dry_run:
            meta.update(
                name,
                status="failed",
                schema_version=source.schema_version,
                error=msg,
            )
        return result

    as_of = source.as_of(fetched.raw)
    result.as_of = as_of
    result.partial = fetched.partial

    # ---- 2. raw 저장 (실패해도 여기까지는 남긴다) --------------------------
    raw_count = source.raw_record_count(fetched.raw)
    # 빈 응답도 partial 로 표시합니다 (기획서 11-1). 소비자는 partial=true 를
    # 보면 쓰지 않습니다.
    raw_partial = fetched.partial or raw_count == 0
    result.partial = raw_partial
    raw_env = storage.raw_envelope(
        source=name,
        schema_version=source.schema_version,
        as_of=as_of,
        as_of_precision=source.as_of_precision,
        raw=fetched.raw,
        record_count=raw_count,
        partial=raw_partial,
        notes=fetched.notes or ("빈 응답" if raw_count == 0 else None),
    )
    if not dry_run:
        path = storage.write_raw(name, run_date, raw_env)
        result.written.append(_rel(path))
        log(f"[{name}] raw 저장 -> {_rel(path)} ({raw_env['record_count']}건)")

    # ---- 3. normalize ----------------------------------------------------
    try:
        records = source.normalize(fetched.raw)
    except Exception as exc:  # noqa: BLE001
        msg = f"normalize 실패: {type(exc).__name__}: {exc}"
        result.errors.append(msg)
        if not dry_run:
            meta.update(
                name, status="failed", schema_version=source.schema_version, error=msg
            )
        return result

    log(f"[{name}] normalize -> {len(records)}건")

    # ---- 4. validate -----------------------------------------------------
    previous_env = storage.read_latest(name)
    previous_records = (previous_env or {}).get("records") or None
    previous_as_of = (previous_env or {}).get("as_of")

    validation = source.validate(records, previous_records)
    validation = validation.merge(
        quality.check_as_of_regression(previous_as_of, as_of)
    )
    result.warnings.extend(validation.warnings)
    result.errors.extend(validation.errors)
    result.quarantined_count = len(validation.quarantine)

    if validation.quarantine and not dry_run:
        path = storage.write_quarantine(
            name,
            run_date,
            {
                "source": name,
                "collected_at": raw_env["collected_at"],
                "as_of": as_of,
                "batch_rejected": not validation.ok,
                "errors": validation.errors,
                "warnings": validation.warnings,
                "count": len(validation.quarantine),
                "entries": validation.quarantine,
            },
        )
        result.written.append(_rel(path))
        log(f"[{name}] 격리 {len(validation.quarantine)}건 -> {_rel(path)}")

    # ---- 5. 검사 실패 -> latest 를 덮어쓰지 않고 종료 -----------------------
    if not validation.ok:
        empty_only = len(records) == 0
        status = "stale" if empty_only else "quarantined"
        for line in validation.errors:
            log(f"[{name}] {status.upper()}: {line}")
        log(f"[{name}] latest 를 덮어쓰지 않습니다.")
        result.status = status
        if not dry_run:
            meta.update(
                name,
                status=status,
                schema_version=source.schema_version,
                quarantined_count=len(validation.quarantine),
                error="; ".join(validation.errors) or None,
            )
        return result

    # 레코드 단위 격리분은 빼고 진행합니다.
    if validation.quarantine:
        records = quality.partition(records, validation.quarantine)
        log(f"[{name}] 격리분 제외 후 {len(records)}건")

    # ---- 6. diff ---------------------------------------------------------
    from_label = (previous_env or {}).get("collected_at", "")[:10] or "(none)"
    diff = differ.diff_records(
        previous_records,
        records,
        source=name,
        from_label=from_label,
        to_label=run_date,
    )
    result.diff_summary = diff["summary"]
    result.warnings.extend(quality.check_removed_ratio(diff, len(records)))

    # ---- 7~9. latest / series / diff -------------------------------------
    latest_env = storage.envelope(
        source=name,
        schema_version=source.schema_version,
        as_of=as_of,
        as_of_precision=source.as_of_precision,
        records=records,
        collected_at=raw_env["collected_at"],
        partial=fetched.partial,
        notes=fetched.notes,
    )
    result.record_count = len(records)

    if not dry_run:
        path = storage.write_latest(name, latest_env)
        result.written.append(_rel(path))
        log(f"[{name}] latest 갱신 -> {_rel(path)}")

        point = {
            "collected_at": latest_env["collected_at"],
            "as_of": as_of,
            "record_count": len(records),
            "partial": fetched.partial,
            "backfill": False,
            "metrics": source.series_metrics(records),
        }
        path = storage.append_series(name, point, schema_version=source.schema_version)
        result.written.append(_rel(path))

        if previous_env is not None and not differ.is_empty(diff):
            path = storage.write_diff(name, run_date, diff)
            result.written.append(_rel(path))
            log(
                "[{}] diff -> {} (+{} -{} ~{})".format(
                    name,
                    _rel(path),
                    diff["summary"]["added"],
                    diff["summary"]["removed"],
                    diff["summary"]["changed"],
                )
            )
        elif previous_env is None:
            log(f"[{name}] 이전 latest 가 없어 diff 를 건너뜁니다 (첫 실행).")

    # ---- 10. meta --------------------------------------------------------
    status = "stale" if (fetched.partial and partial_marks_stale) else "ok"
    if fetched.partial:
        log(f"[{name}] 부분 수집: {fetched.notes}")
    result.status = status
    if not dry_run:
        meta.update(
            name,
            status=status,
            record_count=len(records),
            as_of=as_of,
            schema_version=source.schema_version,
            quarantined_count=len(validation.quarantine),
            error=fetched.notes if fetched.partial else None,
        )
    return result


# ---------------------------------------------------------------------------
# 백필
# ---------------------------------------------------------------------------


def run_backfill(
    source: Source,
    periods: list[str],
    *,
    dry_run: bool = False,
    log: Logger = _noop,
) -> list[RunResult]:
    """과거 채우기.

    일일 수집과 **다른 경로**입니다. latest 를 건드리지 않고 series/ 에만 씁니다.
    """
    if not source.supports_backfill:
        raise RuntimeError(f"{source.name} 소스는 백필을 지원하지 않습니다.")

    results: list[RunResult] = []
    for period in periods:
        res = RunResult(source=source.name, status="failed", as_of=period)
        log(f"[{source.name}] backfill {period} ...")
        try:
            fetched = source.fetch_period(period)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            log(f"[{source.name}] {period} 실패 -- {msg}")
            res.errors.append(msg)
            results.append(res)
            continue

        raw_env = storage.raw_envelope(
            source=source.name,
            schema_version=source.schema_version,
            as_of=period,
            as_of_precision=source.as_of_precision,
            raw=fetched.raw,
            record_count=source.raw_record_count(fetched.raw),
            partial=fetched.partial,
            notes=fetched.notes,
        )
        if not dry_run:
            path = storage.write_raw(source.name, f"backfill-{period}", raw_env)
            res.written.append(_rel(path))

        records = source.normalize(fetched.raw)
        validation = source.validate_backfill(records, period)
        res.record_count = len(records)
        res.quarantined_count = len(validation.quarantine)
        res.warnings.extend(validation.warnings)
        res.errors.extend(validation.errors)

        if validation.quarantine:
            records = quality.partition(records, validation.quarantine)
            if not dry_run:
                storage.write_quarantine(
                    source.name,
                    f"backfill-{period}",
                    {
                        "source": source.name,
                        "as_of": period,
                        "batch_rejected": not validation.ok,
                        "errors": validation.errors,
                        "count": len(validation.quarantine),
                        "entries": validation.quarantine,
                    },
                )

        if not validation.ok:
            res.status = "quarantined"
            log(f"[{source.name}] {period} 격리: {'; '.join(validation.errors)}")
            results.append(res)
            continue

        if not dry_run:
            point = {
                "collected_at": raw_env["collected_at"],
                "as_of": period,
                "record_count": len(records),
                "partial": fetched.partial,
                "backfill": True,
                "metrics": source.series_metrics(records),
            }
            storage.append_series(
                source.name, point, schema_version=source.schema_version
            )

        res.status = "ok"
        log(f"[{source.name}] {period} -> {len(records)}건")
        results.append(res)

    return results


# ---------------------------------------------------------------------------
# 재계산 (저장된 파일만 사용, API 호출 없음)
# ---------------------------------------------------------------------------


def revalidate(source: Source, *, log: Logger = _noop) -> ValidationResult:
    """저장된 latest 를 다시 검사한다."""
    env = storage.read_latest(source.name)
    if env is None:
        raise FileNotFoundError(f"latest 가 없습니다: {storage.latest_path(source.name)}")
    records = env.get("records") or []
    result = source.validate(records, None)
    log(f"[{source.name}] latest {len(records)}건 재검사 -> ok={result.ok}")
    return result


def recompute_diff(
    source: Source, from_date: str, to_date: str, *, log: Logger = _noop
) -> dict:
    """두 날짜의 raw/ 를 읽어 diff 를 다시 계산한다."""
    out = []
    for d in (from_date, to_date):
        env = storage.read_raw(source.name, d)
        if env is None:
            raise FileNotFoundError(f"raw 가 없습니다: {storage.raw_path(source.name, d)}")
        out.append(source.normalize(env["raw"]))

    diff = differ.diff_records(
        out[0], out[1], source=source.name, from_label=from_date, to_label=to_date
    )
    log(
        "[{}] {} -> {}: +{} -{} ~{}".format(
            source.name,
            from_date,
            to_date,
            diff["summary"]["added"],
            diff["summary"]["removed"],
            diff["summary"]["changed"],
        )
    )
    return diff
