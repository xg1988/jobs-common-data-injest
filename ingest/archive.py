"""오래된 달을 DB 에서 파일로 옮긴다.

왜 필요한가
    Supabase 무료 한도는 500MB 입니다. 전국 거래는 한 달 약 4.4만 건,
    행당 621 바이트라 **19개월이면 꽉 찹니다**. 5년치(1.5GB)는 안 들어갑니다.

    그렇다고 지울 수는 없습니다. 그래서 오래된 달은 파일로 내보내고
    DB 에서만 비웁니다. 데이터는 그대로 남습니다.

    한 달치 파일은 gzip 으로 약 830KB 입니다. 5년치 60개를 다 합쳐도
    50MB 라 저장소에 그냥 들어갑니다.

순서를 지키는 것이 전부입니다
    ① DB 에서 그 달을 전부 읽는다
    ② 파일로 쓴다
    ③ **다시 읽어서** 행 수와 sha256 이 맞는지 본다
    ④ 맞을 때만 DB 에서 지운다

    ③ 을 건너뛰면 언젠가 유일한 사본을 날립니다. 쓰기가 도중에 끊겼는지
    디스크가 찼는지는 다시 읽어 보기 전에는 알 수 없습니다.

파일 형식
    NDJSON + gzip. 한 줄에 한 거래.
    Parquet 이 더 작지만 pyarrow 가 필요하고, 소비자가 `zcat | jq` 로
    바로 못 봅니다. 이 계층의 목적은 **누구나 읽을 수 있는 것** 입니다.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


from ingest import db, storage

#: DB 에 남겨 둘 최근 개월 수. 이보다 오래된 달이 아카이브 대상입니다.
DEFAULT_HOT_MONTHS = 12

#: 아카이브가 다루는 테이블. 소스마다 다릅니다.
TABLES = {
    "molit_apt_trade": {
        "table": db.APT_TRADE if hasattr(db, "APT_TRADE") else "mkt_apt_trade",
        #: 어느 달에 속하는지 판단하는 컬럼
        "month_column": "deal_date",
        #: 페이지 사이에 행이 새거나 겹치지 않게 하는 정렬 키 (유일해야 합니다)
        "order": "key.asc",
    }
}


class ArchiveError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------


def archive_dir(source: str) -> Path:
    return storage.DATA / "archive" / source


def month_path(source: str, month: str) -> Path:
    return archive_dir(source) / f"{month}.ndjson.gz"


def index_path(source: str) -> Path:
    return archive_dir(source) / "index.json"


def load_index(source: str) -> dict:
    path = index_path(source)
    if not path.exists():
        return {"source": source, "format": "ndjson.gz", "months": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_index(source: str, index: dict) -> None:
    path = index_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    storage.write_json(path, index)


# ---------------------------------------------------------------------------
# 달 계산
# ---------------------------------------------------------------------------


def cutoff_month(hot_months: int, today: date | None = None) -> str:
    """이 달**보다 앞선** 달이 아카이브 대상입니다.

    hot_months=12, 오늘이 2026-08 이면 -> "2025-09".
    즉 2025-08 이하가 대상, 2025-09 ~ 2026-08 (딱 12개월) 은 DB 에 남습니다.

    이번 달도 12개월에 포함됩니다. 여기서 한 달을 잘못 세면 아직 매일
    갱신 중인 달을 아카이브해 버립니다.
    """
    today = today or date.fromisoformat(storage.today_str())
    total = today.year * 12 + (today.month - 1) - (hot_months - 1)
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_bounds(month: str) -> tuple[str, str]:
    """'2025-03' -> ('2025-03-01', '2025-04-01'). 끝은 **포함하지 않습니다**."""
    y, m = (int(x) for x in month.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"


# ---------------------------------------------------------------------------
# 쓰기 · 읽기
# ---------------------------------------------------------------------------


def _serialize(rows: list[dict]) -> bytes:
    """행 목록 -> gzip NDJSON 바이트.

    같은 입력이면 항상 같은 바이트가 나와야 합니다. 그래야 sha256 검증이
    의미가 있고, 다시 만들어도 git diff 가 안 생깁니다. 그래서
      - 키를 정렬하고 (sort_keys)
      - gzip 에 타임스탬프를 안 넣습니다 (mtime=0)
    """
    body = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")
    return gzip.compress(body, mtime=0)


def read_month(source: str, month: str) -> list[dict]:
    """아카이브 파일을 읽는다. 소비자도 이 함수를 그대로 쓰면 됩니다."""
    path = month_path(source, month)
    if not path.exists():
        raise ArchiveError(f"아카이브 파일이 없습니다: {path}")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 본체
# ---------------------------------------------------------------------------


@dataclass
class MonthResult:
    month: str
    rows: int = 0
    bytes: int = 0
    sha256: str = ""
    written: bool = False
    evicted: int = 0
    skipped: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def archive_month(
    source: str,
    month: str,
    *,
    evict: bool = False,
    dry_run: bool = False,
    log=print,
) -> MonthResult:
    """한 달을 파일로 내보내고, evict 면 DB 에서 비운다."""
    spec = TABLES.get(source)
    if spec is None:
        raise ArchiveError(f"{source} 는 아카이브 대상이 아닙니다.")
    if not db.enabled():
        raise ArchiveError(db.why_disabled())

    table = spec["table"]
    col = spec["month_column"]
    start, end = month_bounds(month)
    # 같은 컬럼에 조건을 두 개 걸어야 하는데 params 는 키가 겹칠 수 없습니다.
    # PostgREST 의 `and=(...)` 로 묶습니다.
    where = {"and": f"({col}.gte.{start},{col}.lt.{end})"}

    result = MonthResult(month=month)

    rows = db.select_all(table, order=spec["order"], **where)
    result.rows = len(rows)
    if not rows:
        result.skipped = "DB 에 그 달 데이터가 없습니다"
        log(f"  {month}  건너뜀 ({result.skipped})")
        return result

    blob = _serialize(rows)
    result.bytes = len(blob)
    result.sha256 = _sha256(blob)

    if dry_run:
        log(f"  {month}  {result.rows:>7,}행  {result.bytes/1024:>7.0f}KB  (dry-run)")
        return result

    path = month_path(source, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    result.written = True

    # ── 검증: 방금 쓴 파일을 다시 읽습니다 ──────────────────────────────
    # 쓰기가 성공했다는 것과 읽을 수 있다는 것은 다른 이야기입니다.
    disk = path.read_bytes()
    if _sha256(disk) != result.sha256:
        result.errors.append(f"{month}: 파일 sha256 불일치 -- 쓰기가 손상됐습니다")
        return result
    try:
        back = read_month(source, month)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"{month}: 파일을 다시 못 읽습니다 -- {exc}")
        return result
    if len(back) != result.rows:
        result.errors.append(
            f"{month}: 행 수 불일치 DB {result.rows} vs 파일 {len(back)}"
        )
        return result

    index = load_index(source)
    index["months"][month] = {
        "rows": result.rows,
        "bytes": result.bytes,
        "sha256": result.sha256,
        "archived_at": storage.iso_utc(),
        "evicted": False,
    }
    save_index(source, index)
    log(f"  {month}  {result.rows:>7,}행  {result.bytes/1024:>7.0f}KB  -> {path.name}")

    if evict:
        result.evicted = evict_month(source, month, table=table, where=where, log=log)

    return result


def evict_month(
    source: str,
    month: str,
    *,
    table: str | None = None,
    where: dict | None = None,
    log=print,
) -> int:
    """검증된 아카이브가 있을 때만 DB 에서 그 달을 비운다.

    호출 순서가 뒤바뀌어도(아카이브 없이 evict 만 불러도) 안전해야 하므로
    여기서 **다시** 확인합니다. 앞에서 확인했으니 괜찮겠지, 가 사고를 냅니다.
    """
    spec = TABLES[source]
    table = table or spec["table"]
    if where is None:
        start, end = month_bounds(month)
        col = spec["month_column"]
        where = {"and": f"({col}.gte.{start},{col}.lt.{end})"}

    index = load_index(source)
    entry = index["months"].get(month)
    if entry is None:
        raise ArchiveError(f"{month}: 아카이브 기록이 없습니다. 지울 수 없습니다.")

    path = month_path(source, month)
    if not path.exists():
        raise ArchiveError(f"{month}: 아카이브 파일이 없습니다: {path}")
    if _sha256(path.read_bytes()) != entry["sha256"]:
        raise ArchiveError(f"{month}: 파일이 기록된 sha256 과 다릅니다. 지우지 않습니다.")

    live = db.count(table, **where)
    if live != entry["rows"]:
        raise ArchiveError(
            f"{month}: DB {live}행 vs 아카이브 {entry['rows']}행. "
            "아카이브 후에 데이터가 바뀌었습니다. 다시 아카이브하세요."
        )

    removed = db.delete(table, **where)
    entry["evicted"] = True
    entry["evicted_at"] = storage.iso_utc()
    save_index(source, index)
    log(f"  {month}  DB 에서 {removed:,}행 비움 (파일 {entry['rows']:,}행 보관)")
    return removed


def run(
    source: str,
    *,
    hot_months: int = DEFAULT_HOT_MONTHS,
    before: str | None = None,
    evict: bool = False,
    dry_run: bool = False,
    log=print,
) -> list[MonthResult]:
    """DB 에 남은 달 중 기준보다 오래된 것을 전부 아카이브한다."""
    spec = TABLES.get(source)
    if spec is None:
        raise ArchiveError(f"{source} 는 아카이브 대상이 아닙니다.")

    cutoff = before or cutoff_month(hot_months)
    log(f"[{source}] {cutoff} 이전 달을 아카이브합니다 (최근 {hot_months}개월은 DB 유지)")

    months = sorted(m for m in db_months(source) if m < cutoff)
    if not months:
        log("  대상 없음 -- DB 에 오래된 달이 없습니다.")
        return []

    results = []
    for month in months:
        try:
            results.append(
                archive_month(source, month, evict=evict, dry_run=dry_run, log=log)
            )
        except Exception as exc:  # noqa: BLE001
            # 한 달이 실패해도 나머지는 계속합니다. 다만 그 달은 안 지웁니다.
            r = MonthResult(month=month)
            r.errors.append(f"{month}: {type(exc).__name__}: {exc}")
            results.append(r)
            log(f"  {month}  실패: {exc}")
    return results


def db_months(source: str) -> list[str]:
    """DB 에 실제로 들어 있는 달 목록.

    전체를 읽지 않고 날짜 컬럼만 받아 훑습니다. PostgREST 에 distinct 가
    없어서 이렇게 합니다 -- 컬럼 하나라 100만 행이어도 몇 MB 입니다.
    """
    spec = TABLES[source]
    col = spec["month_column"]
    rows = db.select_all(spec["table"], columns=col, order=f"{col}.asc")
    return sorted({str(r[col])[:7] for r in rows if r.get(col)})
