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

from ingest import db, differ, meta, quality, storage
from ingest.base import QuotaExhausted, Source, ValidationResult

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


def _describe_scope_change(before: dict | None, after: dict | None) -> str:
    """무엇이 어떻게 바뀌었는지 한 줄로.

    "범위가 바뀌었습니다" 만 찍으면 로그를 봐도 뭘 확인해야 할지 모릅니다.
    """
    before, after = before or {}, after or {}
    parts: list[str] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if isinstance(old, list) or isinstance(new, list):
            old_set, new_set = set(old or []), set(new or [])
            added, removed = len(new_set - old_set), len(old_set - new_set)
            parts.append(
                f"{key} {len(old_set)}->{len(new_set)}개 (+{added} -{removed})"
            )
        else:
            parts.append(f"{key} {old!r}->{new!r}")
    return ", ".join(parts) or "내용 동일"


def _push_to_db(
    source: Source,
    *,
    records: list[dict],
    diff: dict,
    series_points: list[dict],
    collected_at: str,
    run_date: str,
    result: RunResult,
    log: Logger,
) -> None:
    """DB 로 밀어 넣는다.

    실패해도 파이프라인을 죽이지 않습니다. 파일은 이미 저장됐고 저장소에
    커밋되므로, DB 는 `ingest sync` 로 나중에 다시 채울 수 있습니다.
    여기서 예외를 던지면 멀쩡히 받은 데이터까지 버리게 됩니다.
    """
    name = source.name
    if not db.enabled():
        log(f"[{name}] {db.why_disabled()}")
        return
    if not source.db_table:
        return

    try:
        rows = source.db_rows(records, collected_at=collected_at)
        n = db.upsert(source.db_table, rows, on_conflict=source.db_conflict_key)
        log(f"[{name}] DB {source.db_table} <- {n}행")

        if source.db_event_table:
            events = source.db_event_rows(
                diff, observed_on=run_date, observed_at=collected_at
            )
            if events:
                db.insert(source.db_event_table, events)
                log(f"[{name}] DB {source.db_event_table} <- {len(events)}건")

        if series_points:
            db.write_series_points(name, series_points)

        db.write_collection_run(
            {
                "source": name,
                "run_date": run_date,
                "collected_at": collected_at,
                "as_of": result.as_of,
                "as_of_precision": source.as_of_precision,
                "status": result.status or "ok",
                "record_count": len(records),
                "quarantined_count": result.quarantined_count,
                "partial": result.partial,
                "added": diff["summary"]["added"],
                "removed": diff["summary"]["removed"],
                "changed": diff["summary"]["changed"],
                "warnings": result.warnings,
                "errors": result.errors,
            }
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"DB 쓰기 실패: {type(exc).__name__}: {exc}"
        log(f"[{name}] {msg} -- 파일은 저장됐습니다. `ingest sync` 로 재시도하세요.")
        result.warnings.append(msg)


def run_source(
    source: Source,
    *,
    run_date: str | None = None,
    dry_run: bool = False,
    accept_scope_change: bool = False,
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

    # 조회 범위가 지난번과 달라졌는지 먼저 봅니다.
    # 범위를 넓히면 레코드 수가 몇 배로 뜁니다. 그걸 'API 가 깨졌다' 로
    # 읽으면 전국 전환 첫날 수집이 통째로 격리됩니다.
    #
    # 다만 **비교할 게 없는 경우**가 있습니다. `scope` 필드는 나중에 생겼고,
    # 그 전에 쓰인 latest 에는 없습니다. 2026-08-21 전국 전환이 정확히 여기서
    # 걸렸습니다 -- 탈출구는 있는데 이전 봉투에 지문이 없어 발동을 못 했고,
    # 103,407건이 통째로 격리됐습니다. '모른다' 를 '같다' 로 읽은 탓입니다.
    scope = source.scope()
    previous_scope = (previous_env or {}).get("scope")
    source.scope_unknown = bool(
        scope is not None and previous_env is not None and previous_scope is None
    )
    source.scope_changed = bool(
        accept_scope_change
        or (scope is not None and previous_scope is not None and scope != previous_scope)
    )
    if accept_scope_change:
        log(f"[{name}] --accept-scope-change: 범위를 직접 바꿨다고 보고 레코드 수 급변을 통과시킵니다")
    elif source.scope_changed:
        log(f"[{name}] 조회 범위가 바뀌었습니다: {_describe_scope_change(previous_scope, scope)}")
    elif source.scope_unknown:
        log(f"[{name}] 이전 수집 기록에 조회 범위가 없습니다 -- 범위 변경 여부를 판단할 수 없습니다")

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
        scope=scope,
    )
    result.record_count = len(records)

    if not dry_run:
        path = storage.write_latest(name, latest_env)
        result.written.append(_rel(path))
        log(f"[{name}] latest 갱신 -> {_rel(path)}")

        # 지역별로도 씁니다. 전국이면 합본이 57MB 라 매일 커밋할 수 없고,
        # 소비자도 한 지역 보려고 전부 내려받게 됩니다.
        if any("region_code" in r for r in records[:1]):
            index_path, region_count = storage.write_latest_shards(name, latest_env)
            result.written.append(_rel(index_path))
            log(f"[{name}] 지역별 latest {region_count}개 -> {_rel(index_path.parent)}/")

        # 한 번 수집해도 기준시점이 여러 개일 수 있습니다 (롤링 윈도우).
        # 점 하나에 몰아넣으면 그래프에 가짜 급등이 생깁니다.
        series_points = [
            {
                **point,
                "collected_at": latest_env["collected_at"],
                "partial": fetched.partial,
                "backfill": False,
            }
            for point in source.series_points(records, as_of)
        ]
        for point in series_points:
            path = storage.append_series(
                name, point, schema_version=source.schema_version
            )
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

        # ---- 9-b. DB ------------------------------------------------------
        _push_to_db(
            source,
            records=records,
            diff=diff,
            series_points=series_points,
            collected_at=latest_env["collected_at"],
            run_date=run_date,
            result=result,
            log=log,
        )

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
            as_of_precision=source.as_of_precision,
            partial=raw_partial,
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
    archive_months: bool = False,
    log: Logger = _noop,
) -> list[RunResult]:
    """과거 채우기.

    일일 수집과 **다른 경로**입니다. latest 를 건드리지 않고 series/ 에만 씁니다.

    archive_months 를 켜면 그 달을 아카이브 파일로도 바로 씁니다.
    5년치를 채울 때 쓰는 길입니다.
    """
    if not source.supports_backfill:
        raise RuntimeError(f"{source.name} 소스는 백필을 지원하지 않습니다.")

    results: list[RunResult] = []
    for period in periods:
        res = RunResult(source=source.name, status="failed", as_of=period)
        log(f"[{source.name}] backfill {period} ...")
        try:
            fetched = source.fetch_period(period)
        except QuotaExhausted as exc:
            # 남은 달을 도는 게 무의미합니다. 헛돌면서 아직 회복되지도 않은
            # 한도를 미리 깎아 먹습니다. 여기까지 받은 건 이미 저장됐습니다.
            res.errors.append(str(exc))
            results.append(res)
            done = [r.as_of for r in results if r.ok]
            log(
                f"[{source.name}] {exc}\n"
                f"  {len(done)}/{len(periods)}개월 완료"
                + (f" ({done[0]} ~ {done[-1]})" if done else "")
                + f"\n  이어서 받으려면: --from {period} --to {periods[-1]}"
            )
            break
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
            for point in source.series_points(records, period):
                storage.append_series(
                    source.name,
                    {
                        **point,
                        "collected_at": raw_env["collected_at"],
                        "partial": fetched.partial,
                        "backfill": True,
                    },
                    schema_version=source.schema_version,
                )

        # 과거 달은 받자마자 '과거' 입니다. Supabase 를 거칠 이유가 없습니다.
        # 5년치 1.5GB 는 무료 한도(500MB)에 안 들어가서, 넣었다 빼려고 해도
        # 백필 도중에 멈춥니다.
        if archive_months and not dry_run:
            from ingest import archive

            rows = source.db_rows(records, collected_at=raw_env["collected_at"])
            written = archive.write_month_from_records(
                source.name, period, rows, log=log
            )
            res.warnings.extend(written.errors)
            if written.written:
                res.written.append(_rel(archive.month_path(source.name, period)))

        res.status = "ok"
        log(f"[{source.name}] {period} -> {len(records)}건")
        results.append(res)

    return results


# ---------------------------------------------------------------------------
# 재계산 (저장된 파일만 사용, API 호출 없음)
# ---------------------------------------------------------------------------


def sync_to_db(source: Source, *, log: Logger = _noop) -> dict[str, int]:
    """저장된 파일을 DB 로 다시 밀어 넣는다. API 호출 없음.

    DB 쓰기가 실패한 날 복구하거나, DB 를 처음 붙일 때 씁니다.
    파일이 진실이므로 여러 번 돌려도 결과가 같습니다 (upsert).
    """
    name = source.name
    if not db.enabled():
        raise RuntimeError(db.why_disabled())

    written = {"records": 0, "series": 0, "state": 0}

    env = storage.read_latest(name)
    if env and source.db_table:
        rows = source.db_rows(env.get("records") or [], collected_at=env["collected_at"])
        written["records"] = db.upsert(
            source.db_table, rows, on_conflict=source.db_conflict_key
        )
        log(f"[{name}] DB {source.db_table} <- {written['records']}행")

    series_doc = storage.read_json(storage.series_path(name))
    if series_doc:
        written["series"] = db.write_series_points(name, series_doc.get("points") or [])
        log(f"[{name}] DB 시계열 <- {written['series']}점")

    entry = meta.get_source(name)
    if entry:
        db.write_source_state(
            name,
            status=entry.get("status") or "stale",
            last_success=entry.get("last_success"),
            last_attempt=entry.get("last_attempt") or storage.iso_utc(),
            consecutive_failures=int(entry.get("consecutive_failures") or 0),
            record_count=int(entry.get("record_count") or 0),
            as_of=entry.get("as_of"),
            as_of_precision=source.as_of_precision,
            schema_version=entry.get("schema_version"),
            quarantined_count=int(entry.get("quarantined_count") or 0),
            partial=bool((env or {}).get("partial")),
            error=entry.get("error"),
        )
        written["state"] = 1
        log(f"[{name}] DB 상태 갱신 (status={entry.get('status')})")

    return written


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
