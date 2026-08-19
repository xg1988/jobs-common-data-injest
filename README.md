# jobs-common-data-injest

공공 API에서 데이터를 받아 **여러 소비자가 나눠 쓸 수 있는 형태로 저장하는 계층**.

단순 프록시가 아니라 **시계열을 만드는 것**이 핵심입니다. 공공 API는 대부분 "지금"
상태만 돌려주고 과거 이력을 주지 않습니다. "어제보다 늘었다"를 말하려면 어제 찍어 둔
스냅샷이 있어야 합니다.

소비자는 이 저장소를 `raw.githubusercontent.com` 으로 읽습니다.
(블로그 자동화 컨테이너는 공공 API 도메인이 전부 차단돼 있습니다. 이 제약이 전체
설계를 결정합니다 — 결과물은 반드시 저장소에 커밋되어야 합니다.)

---

## 빠르게 돌려보기

```bash
pip install -r requirements.txt
cp .env.example .env          # DATA_GO_KR_KEY 를 채우세요
python -m ingest list
python -m ingest run --source molit_apt_trade --verbose
pytest
```

---

## 소비자용 계약

**데이터를 쓰기 전에 반드시 `meta.json` 부터 읽으세요.**

```jsonc
{
  "sources": {
    "molit_apt_trade": {
      "status": "ok",            // ok | stale | quarantined | failed
      "as_of": "2026-08",
      "record_count": 3421
    }
  }
}
```

`status != "ok"` 이면 그 소스를 쓰지 않습니다.
봉투의 `partial: true` 도 같은 뜻입니다.

| 경로 | 내용 | 누가 읽나 |
|---|---|---|
| `data/latest/{source}.json` | 최신 정규화 스냅샷 | 블로그 파이프라인, 웹사이트 |
| `data/series/{source}.json` | 시점별 요약 누적 | 블로그 파이프라인 |
| `data/diff/{source}/{날짜}.json` | 전일 대비 변화 | 알림 봇 |
| `data/raw/{source}/{날짜}.json` | 원본 (정규화 전) | 사람 (재처리용) |
| `data/quarantine/{source}/{날짜}.json` | 검사 실패분 | 사람 (디버깅용) |

### `collected_at` 과 `as_of` 는 다릅니다

- `collected_at` — 내가 받은 시각
- `as_of` — 데이터 기준 시점

실거래가는 계약일로부터 30일 이내 신고라, 오늘 받아도 이번 달 데이터는 3분의 1도
들어와 있지 않습니다. 구분하지 않으면 "이번 달 거래 급감" 같은 틀린 해석이 나옵니다.

---

## CLI

```
run       --source <name> | --all       일일 수집
backfill  --source <name> --from --to   과거 채우기 (latest 를 건드리지 않음)
validate  --source <name>               저장된 latest 재검사
diff      --source <name> --from --to   두 날짜 raw 로 diff 재계산
capture   --source <name>               실응답을 tests/fixtures/ 에 저장
list                                    등록된 소스 목록
```

공통 옵션: `--dry-run` (파일 안 씀), `--verbose`

종료 코드: `0` 전부 ok · `1` 일부 실패 · `2` 연속 3회 실패 (Actions 알림용)

---

## 설계에서 양보하지 않는 것

**검사에 걸리면 `latest` 를 덮어쓰지 않습니다.**
API가 빈 응답이나 이상한 값을 줬을 때 덮어쓰면, 다음 날 소비자가 그 잘못된
데이터로 글을 씁니다. 오래된 데이터가 틀린 데이터보다 낫습니다.
(`ingest/pipeline.py` 5번 단계)

**`fetch` 와 `normalize` 를 분리합니다.**
원본을 그대로 `raw/` 에 저장해야 정규화 로직에 버그가 있었을 때 다시 돌릴 수
있습니다. 섞어 놓으면 그날 데이터는 영영 복구 못 합니다.

**해제(취소) 거래를 삭제하지 않습니다.**
`canceled=true` 플래그만 붙여 보관하고, 집계에서 빼는 건 소비자 몫입니다.
`series` 요약에서는 기본적으로 제외합니다 — 안 그러면 취소된 신고가가
"역대 최고가"로 잡힙니다.

---

## 새 소스 붙이기

`ingest/base.py` 의 `Source` 를 상속해 세 개만 구현하면 됩니다.

```python
@register
class MySource(Source):
    name = "my_source"
    as_of_precision = "month"
    kind = "update"          # append | update

    def fetch(self) -> FetchResult: ...
    def normalize(self, raw: dict) -> list[dict]: ...   # _key, _watch 포함
    def as_of(self, raw: dict) -> str: ...
```

선택 오버라이드: `validate`, `series_metrics`, `fetch_period`(백필),
`raw_record_count`.

diff 엔진은 append형·update형을 구분하지 않습니다. 짝짓기는 `_key`,
변화 감지는 `_watch` 로만 합니다.

---

## 기획서에서 바꾼 것 (근거 포함)

| 항목 | 기획서 | 구현 | 왜 |
|---|---|---|---|
| `raw/` 봉투 | `latest` 와 동일 구조 | 봉투 필드는 동일, 본문 키만 `records` → `raw` | raw 는 정의상 정규화 전이라 레코드 리스트가 없습니다. `normalize()` 가 이 `raw` 를 그대로 받습니다 |
| `series/` 내용 | "누적" (형태 미정) | 시점별 **요약 지표**(`points[]`)만 누적 | 레코드 전체를 쌓으면 몇 달 만에 못 쓸 크기가 됩니다. 원본이 필요하면 `raw/` 를 봅니다. 열린 질문 5의 잠정 답 |
| 거래일 검사 | "`as_of` 월과 일치" | "조회한 월 집합 안에 있을 것" | 수집이 롤링 윈도우(최근 3개월)라 한 달에 고정할 수 없습니다 |
| `partial` 처리 | "true면 소비자가 쓰면 안 됨" | `meta.status = "stale"` 로 내림 (`sources.yml: partial_marks_stale`) | 소비자가 `meta.json` 만 보고 판단할 수 있어야 합니다. 지역을 전국으로 늘리면 매일 한두 건은 실패할 테니, 그때 이 값을 `false` 로 바꾸는 걸 검토하세요 |
| 빈 응답 | `partial=true` | 그대로 + `status="stale"` | |

---

## 열린 질문 (기획서 18장) — 현재 답

1. **실거래가 API가 JSON을 지원하는가** — ⏳ 미확인.
   파서는 XML/JSON 둘 다 처리하도록 써 뒀고, `config/sources.yml` 의
   `response_format` 으로 전환합니다. `python -m ingest capture` 로 확인하세요.
2. **대상 지역 범위** — ✅ 서울 주요 8개 구로 시작 (`config/regions.yml`).
   하루 24회 호출. 동작 확인 후 늘립니다. 전국(약 250개 시군구)이면 하루 750회가
   되므로, 늘리기 전에 소요시간과 실패율을 먼저 재보세요.
3. **전월세 실거래가** — ✅ 1단계에서는 안 붙입니다. 기획서 17장대로 두 번째 소스는
   성격이 반대인 **금융상품(update형)** 이어야 어댑터 인터페이스가 검증됩니다.
   전월세는 실거래가와 같은 append형이라 검증 가치가 낮습니다.
4. **백필 시작 시점** — ✅ `--from 2024-01` 로 시작 (약 31개월).
   지역 8개 × 31개월 = 248회 호출, 요청 간 1초 지연이라 5분 안팎.
   더 옛날이 필요해지면 그때 늘리면 됩니다 (백필은 `latest` 를 안 건드리므로 안전).
5. **`series/` 분할 규칙** — 요약만 쌓으므로 당분간 분할 불필요.
   월 1회 실행 × 지역 25개 기준으로 수년치가 수 MB 수준입니다.

---

## 상태

1단계 진행 중. 완료 기준은 `기획서 6장` 참고.

- [x] 뼈대 / CLI
- [ ] **API 연결 확인** ← 여기서 막혀 있음: `DATA_GO_KR_KEY` 필요
- [x] 어댑터 (`fetch`/`normalize`/`as_of`) — 필드명은 실응답으로 확정 필요
- [x] 저장 계층
- [x] 품질 검사 + 격리
- [x] diff
- [x] meta.json
- [x] Actions 워크플로
- [x] 백필 CLI
- [ ] 지역 확대
