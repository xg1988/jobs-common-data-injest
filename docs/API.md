# API 명세

두 층으로 나뉩니다.

- **상류(수집 대상)** — 공공데이터포털 API. 이 저장소만 호출합니다.
- **하류(소비자용)** — Supabase PostgREST + GitHub 미러. 블로그·알림봇·웹사이트가 읽습니다.

소비자는 상류를 직접 부르지 않습니다. 블로그 자동화 컨테이너는 공공 API 도메인이
차단돼 있고, 그 제약이 이 구조를 만들었습니다.

---

# 1. 하류 — 소비자용 API

## 1-1. 접속 정보

```
BASE = https://hmsyfipqrvdfitzmuaph.supabase.co/rest/v1
KEY  = sb_publishable_KxD30KDmxEqMP4dsNcLvTA_NMp1duPt
```

모든 요청에 `apikey: $KEY` 헤더가 필요합니다. 이 키는 **읽기 전용**입니다.
RLS 로 `select` 만 허용돼 있어, 이 키로 쓰기를 시도하면 `42501` 로 거부됩니다.

미러(파일)로도 같은 데이터를 읽을 수 있습니다.

```
RAW = https://raw.githubusercontent.com/xg1988/jobs-common-data-injest/main
```

## 1-2. 가장 먼저 읽을 것 — `mkt_source_state`

**데이터를 쓰기 전에 반드시 이 테이블부터 읽으세요.**

```bash
curl "$BASE/mkt_source_state?select=*" -H "apikey: $KEY"
```

```json
[{
  "source": "molit_apt_trade",
  "status": "ok",
  "last_success": "2026-08-19T05:46:57+00:00",
  "last_attempt": "2026-08-19T05:46:57+00:00",
  "consecutive_failures": 0,
  "record_count": 2813,
  "as_of": "2026-08",
  "as_of_precision": "month",
  "schema_version": 1,
  "quarantined_count": 0,
  "partial": false,
  "error": null,
  "updated_at": "2026-08-19T05:46:57+00:00"
}]
```

| 필드 | 뜻 |
|---|---|
| `status` | `ok` \| `stale` \| `quarantined` \| `failed` |
| `partial` | 일부만 받았음 |
| `as_of` | 데이터 기준 시점 (`collected_at` 과 다릅니다 — 1-7 참고) |
| `record_count` | 마지막 성공 시점의 레코드 수 |
| `consecutive_failures` | 연속 실패 횟수. 3 이상이면 알림이 나갑니다 |

**소비자 규칙**

```
status != "ok"  또는  partial == true   →  그 소스를 쓰지 않는다
```

`stale` 은 "받긴 했는데 못 믿겠다"입니다 (빈 응답, 부분 수집).
`quarantined` 는 "값이 이상해서 반영을 막았다"입니다.
둘 다 **이전 데이터는 그대로 남아 있습니다** — 덮어쓰지 않는 게 원칙입니다.

## 1-3. `mkt_apt_trade` — 아파트 매매 실거래가

한 행 = 한 거래. `latest` 스냅샷에 해당합니다.

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `key` | text (PK) | 거래 식별자. `지역\|법정동\|단지\|면적\|층\|거래일\|가격` |
| `region_code` | text | 법정동코드 앞 5자리 (시군구) |
| `dong` | text | 법정동명 |
| `apt_name` | text | 단지명 (원문 유지 — 표기 흔들림 통합 안 함) |
| `area_m2` | numeric(10,4) | 전용면적 |
| `floor` | int | 층 |
| `built_year` | int \| null | 건축년도 |
| `deal_date` | date | 계약일 |
| `price_manwon` | int | **거래금액. 단위는 만원** |
| `canceled` | bool | 해제(취소) 여부 |
| `canceled_date` | date \| null | 해제사유발생일 |
| `deal_type` | text \| null | `중개거래` \| `직거래` \| null |
| `first_seen_at` | timestamptz | 처음 관측한 시각 (갱신돼도 유지) |
| `last_seen_at` | timestamptz | 마지막으로 관측한 시각 |

### ⚠️ `canceled` 를 반드시 처리하세요

해제된 거래를 **삭제하지 않고 플래그만** 달아 보관합니다. 집계할 때 빼는 건
소비자 몫입니다. 안 빼면 취소된 신고가가 "역대 최고가"로 잡힙니다.

```bash
# 항상 이렇게
...&canceled=is.false
```

실측: 2,813건 중 61건(2.2%)이 해제 거래입니다.

### `key` 충돌에 대해

같은 거래가 해제분·정상분으로 두 번 오는 경우가 있어(실측 1.8%), 충돌하면
뒤에 `#2`, `#3` 이 붙습니다. 붙는 순서는 응답 순서가 아니라 레코드 내용
기준이라, 매일 같은 레코드에 같은 `key` 가 붙습니다.

### 질의 예시

```bash
# 강남구, 84제곱미터대, 취소 제외, 비싼 순
curl "$BASE/mkt_apt_trade?select=dong,apt_name,area_m2,floor,deal_date,price_manwon\
&region_code=eq.11680&area_m2=gte.84&area_m2=lt.85&canceled=is.false\
&order=price_manwon.desc&limit=10" -H "apikey: $KEY"

# 최근 한 달, 15억 이상
curl "$BASE/mkt_apt_trade?deal_date=gte.2026-07-01&price_manwon=gte.150000\
&canceled=is.false&order=deal_date.desc" -H "apikey: $KEY"

# 여러 지역
curl "$BASE/mkt_apt_trade?region_code=in.(11680,11650,11710)&canceled=is.false" \
  -H "apikey: $KEY"

# 개수만
curl "$BASE/mkt_apt_trade?select=key&region_code=eq.11680" \
  -H "apikey: $KEY" -H "Prefer: count=exact" -H "Range: 0-0" -I
```

PostgREST 연산자: `eq` `neq` `gt` `gte` `lt` `lte` `like` `ilike` `in` `is`
(`not.` 접두사로 부정). 정렬 `order=컬럼.desc`, 페이징 `limit` / `offset`.

기본 페이지 크기가 걸려 있으니 대량 조회는 `Range` 헤더나 `limit`/`offset` 으로
나눠 받으세요.

## 1-4. `mkt_apt_trade_event` — 무엇이 언제 바뀌었나

알림 봇이 읽는 테이블입니다. `diff` 에 해당합니다.

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | bigint (PK) | |
| `observed_on` | date | 관측한 날 (수집 실행일) |
| `observed_at` | timestamptz | 관측 시각 |
| `event` | text | `added` \| `removed` \| `changed` |
| `key` | text | `mkt_apt_trade.key` |
| `field` | text \| null | `changed` 일 때 바뀐 필드 |
| `before_value` | jsonb \| null | |
| `after_value` | jsonb \| null | |
| `record` | jsonb \| null | `added`/`removed` 일 때 레코드 전체 |

```bash
# 오늘 취소된 신고 (뉴스거리)
curl "$BASE/mkt_apt_trade_event?observed_on=eq.2026-08-19&event=eq.changed\
&field=eq.canceled&after_value=eq.true" -H "apikey: $KEY"

# 오늘 새로 신고된 거래
curl "$BASE/mkt_apt_trade_event?observed_on=eq.2026-08-19&event=eq.added" \
  -H "apikey: $KEY"
```

`removed` 는 실거래가에서 거의 안 나와야 정상입니다. 많이 나오면 조회 범위가
어긋난 신호이고, 전체의 5% 를 넘으면 수집 쪽에서 경고를 남깁니다.
**행을 지우지는 않습니다** — 사라진 이유를 모르는 채 지우면 복구가 안 됩니다.

## 1-5. `mkt_series_point` — 시계열

**한 점 = 한 기준시점(`as_of`) 을 한 날(`collected_date`) 에 잰 것.**

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `source` | text | PK 1/3 |
| `as_of` | text | PK 2/3. 기준 시점 (`YYYY-MM`) |
| `collected_date` | date | PK 3/3. 관측한 날 |
| `collected_at` | timestamptz | |
| `record_count` | int | 그 기준시점의 레코드 수 |
| `partial` / `backfill` | bool | |
| `metrics` | jsonb | 요약 지표 (아래) |

`metrics` 구조 (해제 거래 제외한 값입니다):

```json
{
  "deal_count": 1358,
  "canceled_count": 36,
  "overall": {
    "price_manwon_median": 144750,
    "price_manwon_mean": 178000,
    "price_manwon_min": 5400,
    "price_manwon_max": 2500000,
    "area_m2_median": 84.23
  },
  "by_region": {
    "11680": { "deal_count": 373, "price_manwon_median": 271000, "...": "..." }
  }
}
```

```bash
# 월별 추이
curl "$BASE/mkt_series_point?source=eq.molit_apt_trade&select=as_of,record_count,metrics\
&order=as_of" -H "apikey: $KEY"

# 같은 달을 여러 시점에 잰 기록 (신고 지연이 메워지는 걸 봅니다)
curl "$BASE/mkt_series_point?as_of=eq.2026-08&order=collected_date" -H "apikey: $KEY"
```

두 번째 질의가 이 프로젝트의 핵심입니다. `2026-08` 을 8/19 에 재면 146건이지만
9/19 에 재면 훨씬 늘어납니다. 신고 지연 때문입니다.

## 1-6. `mkt_collection_run` — 실행 로그

수집이 언제 어떻게 돌았는지. 디버깅용입니다.

`source` `run_date` `collected_at` `as_of` `status` `record_count`
`quarantined_count` `partial` `added` `removed` `changed` `errors[]` `warnings[]`

## 1-7. ⚠️ `collected_at` 과 `as_of` 는 다릅니다

- `collected_at` — **내가 받은 시각**
- `as_of` — **데이터 기준 시점**

실거래가는 계약일로부터 30일 이내 신고입니다. 오늘 받아도 이번 달 데이터는
3분의 1도 안 들어와 있습니다.

실측 (2026-08-19 수집):

| 기준월 | 건수 |
|---|---|
| 2026-06 | 1,394 |
| 2026-07 | 1,271 |
| **2026-08** | **148** |

8월이 148건인 건 거래가 준 게 아니라 **아직 신고가 안 된 것**입니다.
이걸 "거래 급감"으로 쓰면 틀린 기사가 나갑니다.

## 1-8. GitHub 미러

DB 를 못 쓰는 상황을 위한 대체 경로입니다. 원본(`raw/`)은 여기에만 있습니다.

| 경로 | 내용 |
|---|---|
| `$RAW/meta.json` | `mkt_source_state` 와 같은 계약 |
| `$RAW/data/latest/molit_apt_trade.json` | 정규화 스냅샷 — **전국에서는 비어 있습니다** (아래) |
| `$RAW/data/latest/molit_apt_trade/index.json` | 지역별 파일 목록 |
| `$RAW/data/latest/molit_apt_trade/{지역코드}.json` | 그 지역만 (약 200 KB) |
| `$RAW/data/series/molit_apt_trade.json` | 시계열 |
| `$RAW/data/diff/molit_apt_trade/{날짜}.json` | 전일 대비 변화 |
| `$RAW/data/raw/molit_apt_trade/{날짜}.json.gz` | **원본** — DB 에 없습니다 |
| `$RAW/data/archive/molit_apt_trade/{달}.ndjson.gz` | DB 에서 내보낸 오래된 달 |
| `$RAW/data/quarantine/molit_apt_trade/{날짜}.json` | 격리된 레코드 |

파일은 통째로만 받을 수 있습니다. 부분 조회가 필요하면 DB 를 쓰세요.

### ⚠️ 합본이 없을 때가 있습니다 — `sharded` 를 먼저 보세요

레코드가 40,000건을 넘으면 합본을 만들지 않습니다. 전국이면 한 파일이 45 MB 가
넘고, 그걸 매일 커밋하면 저장소가 감당하지 못합니다.

이때도 `data/latest/molit_apt_trade.json` 은 **있습니다.** 다만 `records` 가
빈 배열이고 `sharded: true` 가 붙습니다. 지우지 않는 이유는, 지우면 소비자가
404 를 받고 옛 캐시를 계속 쓰기 때문입니다. **빈 배열을 "오늘은 거래가 없었다"
로 읽지 마세요.**

```json
{
  "record_count": 103407,
  "sharded": true,
  "records": [],
  "notes": "레코드가 103,407건이라 합본을 만들지 않습니다. data/latest/molit_apt_trade/index.json 을 읽고 필요한 지역 파일만 받으세요."
}
```

```python
env = get(f"{RAW}/data/latest/{src}.json")
if env.get("sharded"):
    index = get(f"{RAW}/data/latest/{src}/index.json")   # {"regions": {"11680": {...}}, ...}
    records = [r for code in index["regions"]
                 for r in get(f"{RAW}/data/latest/{src}/{code}.json")["records"]]
else:
    records = env["records"]
```

```bash
# 강남구만 -- 전국 전체를 받을 필요가 없습니다
curl -s "$RAW/data/latest/molit_apt_trade/11680.json" | jq '.record_count'
```

`index.json` 의 지역별 `record_count` 를 합하면 `meta.json` 의 `record_count` 와
같아야 합니다. 다르면 받다 만 것이니 쓰지 마세요.

봉투(envelope) 구조:

```json
{
  "source": "molit_apt_trade",
  "schema_version": 1,
  "collected_at": "2026-08-19T05:46:57Z",
  "as_of": "2026-08",
  "as_of_precision": "month",
  "record_count": 2813,
  "partial": false,
  "notes": null,
  "records": [ /* _key, _watch 포함 */ ]
}
```

`raw/` 만 `records` 대신 `raw` 키를 쓰고 gzip 입니다 (정규화 전이라 레코드
리스트가 없습니다).

## 1-9. 버전 관리

`schema_version` 이 올라가면 정규화 스키마가 바뀐 것입니다. 소비자에게 알립니다.
컬럼 추가는 버전을 올리지 않습니다 — 모르는 컬럼은 무시하도록 짜 두세요.

---

# 2. 상류 — 수집 대상 API

이 저장소만 호출합니다. 소비자는 볼 필요 없지만, 값의 출처를 알아야 할 때
참고하세요.

## 2-1. 국토교통부_아파트 매매 실거래가 자료

```
엔드포인트  http://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade
방식        GET / REST
포털 등록   publicDataPk=15126469, 개발계정 자동승인, 24개월, 일 10,000회
```

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `serviceKey` | ✅ | 인증키 |
| `LAWD_CD` | ✅ | 법정동코드 **앞 5자리** (예: `11680`) |
| `DEAL_YMD` | ✅ | 계약년월 `YYYYMM` (예: `202606`) |
| `pageNo` | | 기본 1 |
| `numOfRows` | | 기본 10. 이 저장소는 1000 |
| `_type` | | `json` 을 주면 JSON. 없으면 XML |

### ⚠️ 인증키 인코딩

포털이 같은 키를 두 벌 줍니다.

- **Encoding 키** — 이미 URL 인코딩됨 (`%2B`, `%3D` 가 보임)
- **Decoding 키** — 원본 (`+`, `=` 가 보임)

HTTP 라이브러리는 파라미터를 **다시** 인코딩합니다. Encoding 키를 그대로 넘기면
`%` 가 `%25` 로 이중 인코딩돼 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` 가 납니다.

이 저장소는 **Decoding 키로 통일**하고, `%` 가 보이면 한 번 `unquote` 해서
되돌립니다 (`ingest/sources/molit_apt_trade.py:normalize_service_key`).

### 응답 (XML 기본)

```xml
<response>
  <header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
  <body>
    <items>
      <item>
        <aptNm>까치마을</aptNm><buildYear>1993</buildYear>
        <dealAmount>145,000</dealAmount>
        <dealYear>2026</dealYear><dealMonth>6</dealMonth><dealDay>20</dealDay>
        <excluUseAr>34.44</excluUseAr><floor>6</floor>
        <sggCd>11680</sggCd><umdNm>수서동</umdNm>
        <cdealType> </cdealType><cdealDay> </cdealDay>
        <dealingGbn>중개거래</dealingGbn>
        <!-- 안 쓰는 것: aptDong buyerGbn estateAgentSggNm
             landLeaseholdGbn rgstDate slerGbn jibun -->
      </item>
    </items>
    <numOfRows>2</numOfRows><pageNo>1</pageNo><totalCount>223</totalCount>
  </body>
</response>
```

**태그는 영문 20개입니다.** 기획서가 추정했던 `roadNm`(도로명)은 **없습니다.**

### 정규화 규칙

| 정규화 필드 | 원본 태그 | 처리 |
|---|---|---|
| `region_code` | `sggCd` | 5자리 |
| `dong` | `umdNm` | trim |
| `apt_name` | `aptNm` | trim, 원문 유지 |
| `area_m2` | `excluUseAr` | float |
| `floor` | `floor` | int (음수 가능) |
| `built_year` | `buildYear` | int \| null |
| `deal_date` | `dealYear`+`dealMonth`+`dealDay` | `YYYY-MM-DD` |
| `price_manwon` | `dealAmount` | **콤마·공백 제거 후 int** |
| `canceled` | `cdealType` | `"O"` → true |
| `canceled_date` | `cdealDay` | `"26.07.25"`(YY.MM.DD) → `2026-07-25` |
| `deal_type` | `dealingGbn` | 빈 값 → null |

**함정**

1. `dealAmount` 는 `"145,000"` 같은 **문자열**입니다.
2. 빈 값은 빈 문자열이 아니라 **공백 한 칸**(`<cdealDay> </cdealDay>`)으로 옵니다.
3. `totalCount` 를 보고 **페이징을 끝까지** 돌아야 합니다.
4. 같은 단지 표기가 흔들립니다(`래미안` / `래미안아파트`). 1단계는 원문 유지.

### 에러 응답

정상이 아니면 봉투 자체가 바뀝니다. `resultCode` 만 보면 놓칩니다.

```xml
<OpenAPI_ServiceResponse>
  <cmmMsgHeader>
    <errMsg>SERVICE ERROR</errMsg>
    <returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>
    <returnReasonCode>30</returnReasonCode>
  </cmmMsgHeader>
</OpenAPI_ServiceResponse>
```

JSON 도 마찬가지입니다.

```json
{"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
  "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
  "returnAuthMsg": "등록되지 않은 서비스키",
  "returnReasonCode": "30"}}}
```

| `returnReasonCode` | 뜻 |
|---|---|
| `30` | 등록되지 않은 서비스키 (활용신청 미승인/중지) |
| `22` | 일일 트래픽 초과 |
| `20` | 서비스 접근 거부 |

이 봉투를 못 잡으면 **인증 실패가 "빈 응답"으로 둔갑**합니다. 그러면 `meta` 가
`failed` 가 아니라 `stale` 로 기록돼 원인이 가려집니다.

## 2-2. 새 소스를 붙일 때

`ingest/base.py` 의 `Source` 를 상속해 세 개만 구현하면 됩니다.

```python
@register
class MySource(Source):
    name = "my_source"
    as_of_precision = "month"     # day | month | quarter
    kind = "update"               # append | update

    def fetch(self) -> FetchResult: ...          # 원본 그대로. 가공 금지
    def normalize(self, raw) -> list[dict]: ...  # _key, _watch 포함
    def as_of(self, raw) -> str: ...
```

선택: `validate`, `series_points`, `series_metrics`, `fetch_period`(백필),
`raw_record_count`, `db_table` / `db_rows` / `db_event_rows`.
