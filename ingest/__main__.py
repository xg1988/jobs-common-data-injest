"""CLI 진입점.

    python -m ingest run       --source <name> | --all
    python -m ingest backfill  --source <name> --from YYYY-MM --to YYYY-MM
    python -m ingest validate  --source <name>
    python -m ingest diff      --source <name> --from YYYY-MM-DD --to YYYY-MM-DD
    python -m ingest capture   --source <name>   실응답을 fixtures/ 에 저장
    python -m ingest list

공통 옵션: --dry-run (파일 안 씀), --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from ingest import meta, pipeline, registry, storage

EXIT_OK = 0
EXIT_SOFT_FAIL = 1  # 한 소스라도 ok 가 아님
EXIT_ALERT = 2  # 연속 3회 실패 -- Actions 를 실패로 끝내 알림이 가게


def _make_logger(verbose: bool):
    def log(message: str) -> None:
        print(message, flush=True)

    def quiet(message: str) -> None:
        # 조용 모드에서도 상태가 바뀌는 줄은 남깁니다.
        if any(k in message for k in ("실패", "격리", "QUARANTINED", "STALE", "->")):
            print(message, flush=True)

    return log if verbose else quiet


def _months_between(start: str, end: str) -> list[str]:
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    if (sy, sm) > (ey, em):
        raise SystemExit(f"--from({start}) 이 --to({end}) 보다 뒤입니다.")
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------


def cmd_list(_args) -> int:
    names = registry.names()
    if not names:
        print("등록된 소스가 없습니다.")
        return EXIT_OK

    cfg = storage.load_sources_config()
    meta_doc = meta.load().get("sources", {})
    print("{:<22} {:<9} {:<10} {:<10} {}".format("SOURCE", "ENABLED", "KIND", "STATUS", "AS_OF"))
    for name in names:
        cls = registry.get(name)
        entry = meta_doc.get(name, {})
        print(
            "{:<22} {:<9} {:<10} {:<10} {}".format(
                name,
                str(bool(cfg.get(name, {}).get("enabled", True))).lower(),
                getattr(cls, "kind", "?"),
                entry.get("status") or "-",
                entry.get("as_of") or "-",
            )
        )
    return EXIT_OK


def _selected_sources(args) -> list[str]:
    if getattr(args, "all", False):
        cfg = storage.load_sources_config()
        return [n for n in registry.names() if cfg.get(n, {}).get("enabled", True)]
    if not args.source:
        raise SystemExit("--source 또는 --all 중 하나가 필요합니다.")
    return [args.source]


def cmd_run(args) -> int:
    log = _make_logger(args.verbose)
    names = _selected_sources(args)
    if not names:
        print("활성화된 소스가 없습니다.")
        return EXIT_OK

    results = []
    for name in names:
        # 한 소스가 실패해도 나머지는 계속 돕니다.
        try:
            source = registry.create(name)
            results.append(
                pipeline.run_source(source, dry_run=args.dry_run, log=log)
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] 치명적 오류: {type(exc).__name__}: {exc}", flush=True)
            if not args.dry_run:
                meta.update(
                    name, status="failed", error=f"{type(exc).__name__}: {exc}"
                )
            results.append(
                pipeline.RunResult(source=name, status="failed", errors=[str(exc)])
            )

    print()
    print("{:<22} {:<12} {:>8} {:>8}  {}".format("SOURCE", "STATUS", "RECORDS", "QUAR", "DIFF"))
    for r in results:
        d = r.diff_summary or {}
        print(
            "{:<22} {:<12} {:>8} {:>8}  {}".format(
                r.source,
                r.status,
                r.record_count,
                r.quarantined_count,
                "+{} -{} ~{}".format(
                    d.get("added", 0), d.get("removed", 0), d.get("changed", 0)
                )
                if d
                else "-",
            )
        )

    alerting = [] if args.dry_run else meta.alerting_sources()
    if alerting:
        print(
            f"\n연속 {meta.FAILURE_ALERT_THRESHOLD}회 이상 실패: {', '.join(alerting)}",
            file=sys.stderr,
        )
        return EXIT_ALERT
    return EXIT_OK if all(r.ok for r in results) else EXIT_SOFT_FAIL


def cmd_backfill(args) -> int:
    log = _make_logger(args.verbose)
    source = registry.create(args.source)
    periods = _months_between(args.from_, args.to)
    print(f"[{args.source}] 백필 {periods[0]} ~ {periods[-1]} ({len(periods)}개월)")
    results = pipeline.run_backfill(
        source, periods, dry_run=args.dry_run, log=log
    )
    ok = sum(1 for r in results if r.ok)
    total_records = sum(r.record_count for r in results)
    print(f"\n완료: {ok}/{len(results)}개월, 레코드 {total_records}건")
    return EXIT_OK if ok == len(results) else EXIT_SOFT_FAIL


def cmd_validate(args) -> int:
    log = _make_logger(True)
    source = registry.create(args.source)
    result = pipeline.revalidate(source, log=log)
    for line in result.errors:
        print(f"  ERROR   {line}")
    for line in result.warnings:
        print(f"  WARN    {line}")
    if result.quarantine:
        print(f"  격리 대상 {len(result.quarantine)}건")
    return EXIT_OK if result.ok else EXIT_SOFT_FAIL


def cmd_sync(args) -> int:
    """저장된 파일을 DB 로 다시 밀어 넣는다 (API 호출 없음)."""
    from ingest import db

    if not db.enabled():
        raise SystemExit(
            f"{db.why_disabled()}\n"
            f"  서버라면 {storage.ROOT / '.env'} 에 SUPABASE_URL 과\n"
            f"  SUPABASE_SERVICE_ROLE_KEY 를 넣으세요."
        )

    log = _make_logger(True)
    total = {"records": 0, "series": 0, "state": 0}
    for name in _selected_sources(args):
        source = registry.create(name)
        written = pipeline.sync_to_db(source, log=log)
        for k in total:
            total[k] += written[k]
    print(
        f"\n완료: 레코드 {total['records']}행, 시계열 {total['series']}점, "
        f"상태 {total['state']}건"
    )
    return EXIT_OK


def cmd_diff(args) -> int:
    log = _make_logger(True)
    source = registry.create(args.source)
    diff = pipeline.recompute_diff(source, args.from_, args.to, log=log)
    if args.dry_run:
        print(json.dumps(diff["summary"], ensure_ascii=False, indent=2))
        return EXIT_OK
    path = storage.write_diff(args.source, args.to, diff)
    print(f"저장 -> {path}")
    return EXIT_OK


def _report_field_coverage(items: list[dict]) -> None:
    """실응답의 태그명을 FIELD_ALIASES 와 대조해 [확인 필요] 를 해소한다."""
    from ingest.sources import molit_apt_trade as m

    if not items:
        print("       (item 이 0건이라 필드 대조를 건너뜁니다. 다른 월로 다시 시도해 보세요.)")
        return

    tags = sorted({tag for item in items for tag in item})
    print(f"       응답 태그 {len(tags)}개: {', '.join(tags)}")

    matched: list[str] = []
    missing: list[str] = []
    for field, candidates in m.FIELD_ALIASES.items():
        hit = next((c for c in candidates if c in tags), None)
        if hit:
            matched.append(f"{field} <- {hit}")
        else:
            missing.append(f"{field} (후보: {'/'.join(candidates)})")

    print("       ── 매칭됨 ──")
    for line in matched:
        print(f"         {line}")
    if missing:
        print("       ── 매칭 실패 (FIELD_ALIASES 수정 필요) ──")
        for line in missing:
            print(f"         {line}")

    unused = [t for t in tags if not any(t in c for c in m.FIELD_ALIASES.values())]
    if unused:
        print(f"       ── 안 쓰는 태그 ──\n         {', '.join(unused)}")

    sample = items[0]
    print("       ── 첫 레코드 원문 값 ──")
    for field in ("price_manwon", "canceled", "canceled_date", "area_m2", "floor"):
        value = m.pick(sample, field)
        print(f"         {field:<14} = {value!r}")


def cmd_capture(args) -> int:
    """실응답을 tests/fixtures/ 에 저장하고 필드명을 대조한다 (기획서 16장 2단계).

    테스트에는 실제 호출을 넣지 않습니다. 여기서 받은 fixture 로만 돕니다.
    """
    from ingest.sources import molit_apt_trade as m

    if args.source != "molit_apt_trade":
        raise SystemExit("capture 는 아직 molit_apt_trade 만 지원합니다.")

    import httpx

    try:
        m.read_service_key()
    except RuntimeError as exc:
        raise SystemExit(
            f"{exc}\n"
            f"  서버라면:  echo 'DATA_GO_KR_KEY=발급받은키' > {storage.ROOT / '.env'}"
        ) from None

    src = m.MolitAptTrade()
    lawd = args.region or (src.regions[0]["code"] if src.regions else "11680")
    ymd = (args.month or date.today().strftime("%Y-%m")).replace("-", "")
    print(f"대상: LAWD_CD={lawd} DEAL_YMD={ymd} numOfRows={args.rows}")
    print(f"엔드포인트: {src.base_url}\n")

    out_dir = storage.ROOT / "tests" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_supported = False

    for fmt in ("xml", "json"):
        params = {
            "serviceKey": m.read_service_key(),
            "LAWD_CD": lawd,
            "DEAL_YMD": ymd,
            "pageNo": "1",
            "numOfRows": str(args.rows),
        }
        if fmt == "json":
            params["_type"] = "json"
        try:
            resp = httpx.get(
                src.base_url, params=params, timeout=src.timeout, follow_redirects=True
            )
            body = resp.text
            is_json = body.lstrip().startswith("{")
            ext = "json" if is_json else "xml"
            path = out_dir / f"molit_apt_trade_{lawd}_{ymd}_{fmt}.{ext}"
            path.write_text(body, encoding="utf-8")
            print(f"[{fmt} 요청] HTTP {resp.status_code} -> {path.name} ({len(body)} bytes, 실제 {ext})")

            if fmt == "json":
                json_supported = is_json
            try:
                if is_json:
                    items, total, code, msg = m.parse_json_response(resp.json())
                else:
                    items, total, code, msg = m.parse_xml_response(body)
            except m.ApiError as exc:
                print(f"       API 에러: {exc}")
                continue
            print(f"       resultCode={code!r} totalCount={total} items={len(items)}")
            if fmt == "xml" or is_json:
                _report_field_coverage(items)
        except Exception as exc:  # noqa: BLE001
            print(f"[{fmt} 요청] 실패: {type(exc).__name__}: {exc}")
        print()

    print("── 다음 할 일 ──")
    print(f"  1. config/sources.yml 의 response_format 을 "
          f"{'json 으로 바꿔도 됩니다' if json_supported else 'xml 로 둡니다 (JSON 미지원)'}")
    print("  2. 위 '매칭 실패' 목록이 있으면 FIELD_ALIASES 를 고치세요")
    print("  3. tests/fixtures/README.md 의 '⚠️ 합성' 표시를 지우세요")
    print("  4. python -m ingest run --source molit_apt_trade --verbose")
    return EXIT_OK


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ingest", description="공공 API 수집 계층")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않음")
        sp.add_argument("--verbose", "-v", action="store_true")
        return sp

    sp = common(sub.add_parser("run", help="일일 수집"))
    sp.add_argument("--source")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = common(sub.add_parser("backfill", help="과거 채우기"))
    sp.add_argument("--source", required=True)
    sp.add_argument("--from", dest="from_", required=True, metavar="YYYY-MM")
    sp.add_argument("--to", required=True, metavar="YYYY-MM")
    sp.set_defaults(func=cmd_backfill)

    sp = common(sub.add_parser("sync", help="저장된 파일을 DB 로 재동기화"))
    sp.add_argument("--source")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = common(sub.add_parser("validate", help="저장된 latest 재검사"))
    sp.add_argument("--source", required=True)
    sp.set_defaults(func=cmd_validate)

    sp = common(sub.add_parser("diff", help="두 날짜 diff 재계산"))
    sp.add_argument("--source", required=True)
    sp.add_argument("--from", dest="from_", required=True, metavar="YYYY-MM-DD")
    sp.add_argument("--to", required=True, metavar="YYYY-MM-DD")
    sp.set_defaults(func=cmd_diff)

    sp = common(sub.add_parser("capture", help="실응답을 fixtures/ 에 저장"))
    sp.add_argument("--source", required=True)
    sp.add_argument("--region", help="법정동코드 5자리")
    sp.add_argument("--month", help="YYYY-MM")
    sp.add_argument("--rows", type=int, default=10)
    sp.set_defaults(func=cmd_capture)

    sp = common(sub.add_parser("list", help="등록된 소스 목록"))
    sp.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
