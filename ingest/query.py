"""아카이브 여부와 상관없이 같은 방식으로 조회한다.

문제
    거래를 DB 와 파일 두 곳에 나눠 두면, 쓰는 쪽은 편해도 **읽는 쪽이
    괴로워집니다.** "2024년 강남구" 를 보려면 그게 아직 DB 에 있는지
    아카이브로 넘어갔는지를 먼저 알아야 한다면, 그 아카이브는 없는 것과
    같습니다.

해결
    여기서 갈라 줍니다. 요청한 기간을 최근 구간(DB)과 과거 구간(파일)으로
    쪼개서 각각 읽고 하나로 합칩니다. 부르는 쪽은 어디서 왔는지 몰라도 됩니다.

    돌려주는 값에 `sources` 를 같이 담습니다. 어디서 몇 건이 왔는지
    보이지 않으면, 아카이브 파일이 통째로 빠져도 그냥 "그 시기엔 거래가
    적었나 보다" 로 넘어가게 됩니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ingest import archive, db


@dataclass
class QueryResult:
    rows: list[dict] = field(default_factory=list)
    #: 어느 달을 어디서 읽었는지. {"2025-03": "archive", "2026-07": "db"}
    sources: dict[str, str] = field(default_factory=dict)
    #: 읽다가 **실패한** 달. 파일이 없거나 깨졌습니다.
    missing: list[str] = field(default_factory=list)
    #: 읽기는 됐는데 **0건**인 달.
    #:
    #: missing 과 반드시 구분해야 합니다. 둘을 뭉뚱그리면 "파일이 사라졌다"
    #: 와 "그 달엔 거래가 없었다" 가 같은 것으로 보입니다. 전국 단위에서
    #: 한 달 0건은 사실상 불가능하므로, 0건이 보이면 의심해야 합니다.
    empty: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


def months_between(start: str, end: str) -> list[str]:
    """'2024-01', '2024-03' -> ['2024-01', '2024-02', '2024-03']"""
    sy, sm = (int(x) for x in start.split("-")[:2])
    ey, em = (int(x) for x in end.split("-")[:2])
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def fetch(
    source: str,
    *,
    start: str,
    end: str,
    region_code: str | None = None,
    include_canceled: bool = False,
) -> QueryResult:
    """`start` ~ `end` (YYYY-MM, 양끝 포함) 거래를 전부 가져온다.

    최근 달은 DB 에서, 아카이브된 달은 파일에서 읽습니다.
    """
    spec = archive.TABLES.get(source)
    if spec is None:
        raise archive.ArchiveError(f"{source} 는 조회 대상이 아닙니다.")

    index = archive.load_index(source)
    result = QueryResult()

    for month in months_between(start, end):
        entry = index["months"].get(month)
        # 아카이브가 있고 DB 에서 비워졌다면 파일이 유일한 사본입니다.
        # 아직 안 비웠다면 DB 가 최신이므로 DB 를 씁니다 (아카이브 후에도
        # 정정 신고가 들어올 수 있습니다).
        if entry and entry.get("evicted"):
            try:
                rows = archive.read_month(source, month)
                result.sources[month] = "archive"
            except archive.ArchiveError:
                result.missing.append(month)
                continue
        else:
            try:
                rows = _from_db(spec, month)
                result.sources[month] = "db"
            except Exception:  # noqa: BLE001 -- DB 가 꺼져 있으면 파일로 넘어갑니다
                if not entry:
                    result.missing.append(month)
                    continue
                rows = archive.read_month(source, month)
                result.sources[month] = "archive"

        if not rows:
            result.empty.append(month)
        if region_code:
            rows = [r for r in rows if str(r.get("region_code")) == region_code]
        if not include_canceled:
            # 취소 거래를 섞으면 취소된 신고가가 역대 최고가로 잡힙니다.
            rows = [r for r in rows if not r.get("canceled")]
        result.rows.extend(rows)

    result.rows.sort(key=lambda r: (str(r.get("deal_date")), str(r.get("key"))))
    return result


def _from_db(spec: dict, month: str) -> list[dict]:
    col = spec["month_column"]
    start, end = archive.month_bounds(month)
    return db.select_all(
        spec["table"],
        order=spec["order"],
        **{"and": f"({col}.gte.{start},{col}.lt.{end})"},
    )


def restore(source: str, month: str, *, log=print) -> int:
    """아카이브된 달을 DB 로 되돌린다.

    SQL 로 이리저리 뜯어봐야 할 때 씁니다. 파일을 지우지 않으니
    다 본 뒤에 `ingest archive --evict` 로 다시 비우면 됩니다.
    """
    spec = archive.TABLES.get(source)
    if spec is None:
        raise archive.ArchiveError(f"{source} 는 아카이브 대상이 아닙니다.")

    rows = archive.read_month(source, month)
    if not rows:
        log(f"  {month}  0행 -- 되돌릴 것이 없습니다")
        return 0

    written = db.upsert(spec["table"], rows, on_conflict="key")

    index = archive.load_index(source)
    if month in index["months"]:
        index["months"][month]["evicted"] = False
        index["months"][month]["restored_at"] = archive.storage.iso_utc()
        archive.save_index(source, index)

    log(f"  {month}  {written:,}행 DB 로 되돌림 (파일은 그대로 둡니다)")
    return written
