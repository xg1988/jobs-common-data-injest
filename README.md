# jobs-common-data-injest

공공 API에서 데이터를 받아 **여러 소비자가 나눠 쓸 수 있는 형태로 저장하는 계층**.

단순 프록시가 아니라 **시계열을 만드는 것**이 핵심입니다. 공공 API는 대부분 "지금"
상태만 돌려주고 과거 이력을 주지 않습니다. "어제보다 늘었다"를 말하려면 어제 찍어 둔
스냅샷이 있어야 합니다.

저장은 두 곳에 합니다 -- **Supabase(주)** 와 **GitHub 저장소(미러)**.

블로그 자동화 컨테이너는 공공 API 도메인이 전부 차단돼 있어서, 소비자가 공공 API 를
직접 부를 수 없습니다. 그래서 수집은 여기서만 하고, 소비자는 DB(PostgREST) 또는
`raw.githubusercontent.com` 으로 읽습니다. 이 제약이 전체 설계를 결정합니다.

---

## 문서

- [docs/API.md](docs/API.md) — 소비자용 API 명세 + 상류(공공데이터) API 명세
- [docs/SCHEDULE.md](docs/SCHEDULE.md) — 스케줄링·재시도·품질검사·알림 명세
- [db/](db/) — Supabase 스키마와 미러 당겨오기 함수
- [docs/DASHBOARD.md](docs/DASHBOARD.md) — 대시보드 기획 (무엇에 답하는 화면인가)
- [web/](web/) — 대시보드와 API 문서 (Swagger)

**바로 열기** (GitHub Pages)

| | |
|---|---|
| 📊 대시보드 | https://xg1988.github.io/jobs-common-data-injest/web/ |
| 🔧 API 문서 | https://xg1988.github.io/jobs-common-data-injest/web/swagger.html |

Swagger 의 **Try it out** 은 실제 데이터를 부릅니다 (읽기 전용 키가 미리 채워져 있음).

---

## 빠르게 돌려보기

```bash
pip install -r requirements.txt
cp .env.example .env          # DATA_GO_KR_KEY 를 채우세요 (DB 를 쓰려면 SUPABASE_* 도)
python -m ingest list
python -m ingest run --source molit_apt_trade --verbose
pytest
```

---

## 저장 위치는 두 곳입니다

**DB(Supabase)가 주 저장소, GitHub 저장소가 미러**입니다. 같은 수집 결과를 양쪽에
씁니다. DB 가 죽어도 파일은 남고, 나중에 `ingest sync` 로 다시 채웁니다.

| | DB (Supabase) | GitHub 미러 |
|---|---|---|
| 읽는 법 | PostgREST (HTTPS + publishable 키) | `raw.githubusercontent.com` |
| 부분 조회 | ✅ 지역·기간·가격으로 필터 | ❌ 파일 통째로 |
| 응답 크기 | 강남구만 147 KB | 전체 1.3 MB |
| 원본(raw) | ✗ | ✅ gzip |
| 버전 이력 | 이벤트 테이블 | git |

---

## 소비자용 계약

**데이터를 쓰기 전에 반드시 상태부터 읽으세요.** `status != "ok"` 이면 그 소스를
쓰지 않습니다. 봉투/행의 `partial: true` 도 같은 뜻입니다.

```bash
BASE=https://hmsyfipqrvdfitzmuaph.supabase.co/rest/v1
KEY=sb_publishable_KxD30KDmxEqMP4dsNcLvTA_NMp1duPt   # 공개 읽기 전용

# 1) 상태 확인
curl "$BASE/mkt_source_state?select=source,status,as_of,record_count" -H "apikey: $KEY"

# 2) 강남구 84제곱미터대, 취소 제외, 비싼 순
curl "$BASE/mkt_apt_trade?select=dong,apt_name,area_m2,floor,deal_date,price_manwon\
&region_code=eq.11680&area_m2=gte.84&area_m2=lt.85&canceled=is.false\
&order=price_manwon.desc&limit=10" -H "apikey: $KEY"

# 3) 오늘 바뀐 것 (알림 봇)
curl "$BASE/mkt_apt_trade_event?observed_on=eq.2026-08-19&event=eq.changed" -H "apikey: $KEY"

# 4) 월별 추이
curl "$BASE/mkt_series_point?source=eq.molit_apt_trade&order=as_of" -H "apikey: $KEY"
```

| 테이블 | 내용 | 누가 읽나 |
|---|---|---|
| `mkt_source_state` | 소스별 상태 (`meta.json` 과 같은 계약) | 전부 — **제일 먼저** |
| `mkt_apt_trade` | 거래 현재 상태. 한 행 = 한 거래 | 블로그 파이프라인, 웹사이트 |
| `mkt_apt_trade_event` | 무엇이 언제 바뀌었나 (added/removed/changed) | 알림 봇 |
| `mkt_series_point` | 기준시점 × 관측일 요약 | 블로그 파이프라인 |
| `mkt_collection_run` | 실행 로그 | 사람 |

쓰기는 `service_role` 키로만 가능합니다. 위 publishable 키는 **읽기 전용**입니다
(RLS 로 select 만 허용).

### GitHub 미러 경로

| 경로 | 내용 |
|---|---|
| `data/latest/{source}.json` | 최신 정규화 스냅샷 |
| `data/series/{source}.json` | 시점별 요약 누적 |
| `data/diff/{source}/{날짜}.json` | 전일 대비 변화 |
| `data/raw/{source}/{날짜}.json.gz` | **원본** (정규화 전, gzip) — DB 에는 없습니다 |
| `data/quarantine/{source}/{날짜}.json` | 검사 실패분 |
| `meta.json` | 소스별 상태 |

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
sync      --source <name> | --all       저장된 파일을 DB 로 재동기화
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
| `raw/` 압축 | (언급 없음) | gzip (`.json.gz`) | 하루치 1,916 KB -> 80 KB. 전국으로 늘리면 압축 없이는 저장소가 못 버팁니다 |
| `series/` 내용 | "누적" (형태 미정) | 시점별 **요약 지표**(`points[]`)만 누적 | 레코드 전체를 쌓으면 몇 달 만에 못 쓸 크기가 됩니다. 원본이 필요하면 `raw/` 를 봅니다. 열린 질문 5의 잠정 답 |
| 거래일 검사 | "`as_of` 월과 일치" | "조회한 월 집합 안에 있을 것" | 수집이 롤링 윈도우(최근 3개월)라 한 달에 고정할 수 없습니다 |
| `partial` 처리 | "true면 소비자가 쓰면 안 됨" | `meta.status = "stale"` 로 내림 (`sources.yml: partial_marks_stale`) | 소비자가 `meta.json` 만 보고 판단할 수 있어야 합니다. 지역을 전국으로 늘리면 매일 한두 건은 실패할 테니, 그때 이 값을 `false` 로 바꾸는 걸 검토하세요 |
| 빈 응답 | `partial=true` | 그대로 + `status="stale"` | |

---

## 열린 질문 (기획서 18장) — 현재 답

1. **실거래가 API가 JSON을 지원하는가** — ✅ **지원합니다** (2026-08-19 실응답 확인).
   `_type=json` 을 붙이면 됩니다. 다만 기본값은 XML 로 둡니다 -- 포털 문서상
   데이터 포맷이 XML 이고, 둘의 내용이 같아 바꿀 이유가 없습니다.
   `config/sources.yml` 의 `response_format` 으로 언제든 전환됩니다.
   에러 응답의 JSON 봉투는 모양이 달라서(`OpenAPI_ServiceResponse.cmmMsgHeader`)
   따로 처리합니다.
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

1단계 완료 기준(기획서 6장) 대비.

- [x] `python -m ingest run --source molit_apt_trade` 로컬 실행 성공
- [x] `data/raw/molit_apt_trade/{날짜}.json.gz` 생성 (원본 그대로)
- [x] `data/latest/molit_apt_trade.json` 생성 (정규화 + 봉투)
- [x] `data/series/molit_apt_trade.json` 누적
- [x] `meta.json` 갱신
- [x] 이틀치 diff 생성 — `ingest diff --from --to` 로 실데이터 검증 (`+30 -0 ~3`)
- [x] API 실패 시 `latest` 미덮어쓰기
- [x] 품질 검사 실패분 `quarantine/` 격리
- [x] `pytest` 전부 통과 (65개)
- [x] GitHub 저장소 푸시 + 익명 읽기 확인
- [x] 백필 2024-01 ~ 2026-07 (31개월, 58,486건)
- [x] DB(Supabase) 적재 + PostgREST 부분 조회 확인
- [x] **매일 자동 실행** — VPS cron 00:10 KST + Supabase pg_cron 00:40 KST
- [ ] 실패 알림 (메일·슬랙 등)
- [ ] 지역 확대

### 첫 실수집 결과 (2026-08-19)

| 항목 | 값 |
|---|---|
| 레코드 | 2,813건 (서울 8개 구 × 최근 3개월) |
| 소요 | 8.6초 (24회 호출) |
| 격리 | 0건 |
| 해제(취소) 거래 | 61건 — 삭제 안 하고 플래그만 |
| `_key` 충돌 | 52건 (1.8%) — 해제분이 기준 키 유지, 나머지에 일련번호 |
| 월별 | 2026-06: 1,394 / 2026-07: 1,271 / **2026-08: 148** |

마지막 줄이 `collected_at` 과 `as_of` 를 나눈 이유입니다. 8월은 아직 신고가
안 들어와서 148건뿐입니다. 이걸 "거래 급감"으로 읽으면 안 됩니다.

### 저장 크기

`raw/` 는 gzip 입니다 — 하루치 1,916 KB → **80 KB**. 사람이 재처리할 때만
읽으므로 압축해도 됩니다. `latest`/`series`/`diff` 는 소비자가
`raw.githubusercontent.com` 으로 직접 읽어야 해서 평문으로 둡니다
(`latest` 1.3 MB, `series` 4 KB).

---

## 매일 어떻게 도나

```
00:10 KST   VPS cron          수집 -> 파일 -> GitHub 커밋·푸시
00:40 KST   Supabase pg_cron  공개 미러에서 스스로 당겨와 DB 갱신
```

GitHub Actions 에서는 **수집이 안 됩니다.** 러너(미국 Azure)에서
`apis.data.go.kr:80` 연결이 막혀 있습니다. 자세한 건
[docs/SCHEDULE.md](docs/SCHEDULE.md) 10장.

DB 가 미러에서 당겨오는 구조라, 관리자 키를 서버 어디에도 두지 않습니다.
