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


def cmd_capture(args) -> int:
    """실응답을 tests/fixtures/ 에 저장한다 (기획서 16장 2단계).

    테스트에는 실제 호출을 넣지 않습니다. 여기서 받은 fixture 로만 돕니다.
    """
    from ingest.sources import molit_apt_trade as m

    if args.source != "molit_apt_trade":
        raise SystemExit("capture 는 아직 molit_apt_trade 만 지원합니다.")

    import httpx

    src = m.MolitAptTrade()
    lawd = args.region or (src.regions[0]["code"] if src.regions else "11680")
    ymd = (args.month or date.today().strftime("%Y-%m")).replace("-", "")

    out_dir = storage.ROOT / "tests" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)

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
            ext = "json" if body.lstrip().startswith("{") else "xml"
            path = out_dir / f"molit_apt_trade_{lawd}_{ymd}_{fmt}.{ext}"
            path.write_text(body, encoding="utf-8")
            print(f"[{fmt}] HTTP {resp.status_code} -> {path} ({len(body)} bytes)")
            print(f"       머리 200자: {body[:200].strip()!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{fmt}] 실패: {type(exc).__name__}: {exc}")
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
