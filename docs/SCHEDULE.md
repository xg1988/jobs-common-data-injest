# 스케줄링 명세

무엇이 언제 돌고, 실패하면 어떻게 되는가.

---

## 0. 전체 그림

하루에 두 번 돕니다. 서로 다른 곳에서.

```
00:10 KST   VPS cron          수집 → 파일 저장 → GitHub 에 커밋·푸시
00:40 KST   Supabase pg_cron  공개 미러에서 스스로 당겨와 DB 갱신
```

**왜 나눴나.** 수집은 GitHub 러너에서 못 합니다 (10장). VPS 가 DB 로 직접
밀어 넣으려면 관리자 키를 VPS 에 둬야 하는데, DB 가 **당겨오는** 방식이면
그 키가 아무 데도 필요 없습니다. 미러가 공개 저장소라 인증 없이 읽힙니다.

---

## 1. 일일 수집 (VPS)

| 항목 | 값 |
|---|---|
| 어디서 | VPS `cmn-vps` (`/opt/jobs-common-data-injest`) |
| 트리거 | `crontab`: `10 15 * * *` (UTC) = **매일 00:10 KST** |
| 스크립트 | `scripts/daily.sh` |
| 명령 | `python -m ingest run --all --verbose` |
| 로그 | `logs/daily-{YYYY-MM-DD}.log` (저장소에 안 올라감) |

`daily.sh` 가 하는 일:

1. `git pull --rebase --autostash` — 남이 고친 코드를 받습니다
2. 수집
3. `data/` 와 `meta.json` 을 커밋하고 푸시 — **수집이 실패해도 합니다.**
   `raw/` 와 `meta.json` 이 남아야 나중에 원인을 봅니다
4. 수집 종료 코드를 그대로 반환 — cron 이 실패를 알립니다

### DB 당겨오기 (Supabase)

| 항목 | 값 |
|---|---|
| 트리거 | `pg_cron`: `40 15 * * *` (UTC) = **매일 00:40 KST** |
| 함수 | `public.mkt_pull_from_mirror()` |
| 읽는 곳 | `raw.githubusercontent.com/.../main` (공개) |
| 인증 | 없음 |

30분 여유를 둔 이유는 `raw.githubusercontent.com` 캐시가 몇 분 걸리기 때문입니다.

`meta.json` 의 `status` 가 `ok` 가 아니면 **거래 데이터를 건드리지 않습니다.**
파일 쪽에서 `latest` 를 안 덮어썼다는 뜻이고, DB 도 똑같이 굽니다.

수동 실행:

```sql
select * from public.mkt_pull_from_mirror();
```

### 왜 00:10 KST 인가

날짜가 바뀐 직후에 찍어야 "그날의 스냅샷"이 하루에 하나만 생깁니다.
파일 이름과 `run_date` 는 **KST 기준**입니다 (`storage.today_str()`).
UTC 로 찍으면 전날 날짜가 붙습니다.

### 무엇을 조회하는가 — 롤링 윈도우

조회 단위는 **(지역코드 × 계약년월)** 입니다.

```
오늘이 2026-08-19 → 2026-06, 2026-07, 2026-08 을 각각 조회
```

신고 지연 때문에 최근 달은 계속 채워집니다. 그래서 매일 최근 3개월을
**다시** 받습니다. 설정은 `config/regions.yml` 의 `rolling_months`.

| 지역 수 | 월 수 | 호출 수 | 소요 (VPS 실측) |
|---|---|---|---|
| 8 (서울 주요 구) | 3 | **24회** | 8.6초 |
| 25 (서울 전역) | 3 | 75회 | 약 30초 |
| ~250 (전국) | 3 | 750회 | 약 5분 |

지역 목록은 `config/regions.yml` 에서 늘립니다. 늘리기 전에 소요시간과
실패율을 먼저 재세요.

### 재시도

| 항목 | 값 | 어디 |
|---|---|---|
| 요청 타임아웃 | 20초 | `sources.yml: timeout_seconds` |
| 재시도 | 3회 | `sources.yml: max_retries` |
| 백오프 | 1s → 2s → 4s (지수) | `molit_apt_trade._fetch_one` |
| 요청 간 지연 | 0.2초 | `sources.yml: request_delay` |

(지역 × 월) 하나가 3회 다 실패하면 그 조합만 버리고 나머지는 계속 갑니다.
**전부 실패하면** 부분 수집이 아니라 예외를 던집니다 — 인증키 만료나 서비스
점검이 "빈 응답"으로 둔갑하지 않게.

---

## 2. 실행 순서와 분기

```
1. fetch()                     지역×월 전부. 실패는 개별 재시도
2. raw/ 저장 (gzip)            ★ 실패해도 여기까지는 남긴다
3. normalize()
4. validate()                  이전 latest 와 비교
5. 검사 실패?
     yes ─ quarantine/ 저장
          ─ meta status = quarantined | stale
          ─ ★ latest 를 덮어쓰지 않고 종료
     no  ─ 계속
6. diff 계산                   이전 latest vs 새 records
7. latest/ 덮어쓰기
8. series/ append              기준시점마다 점 하나씩
9. diff/ 저장                  변화 없으면 파일 안 만듦
10. meta.json 갱신
11. git commit & push          daily.sh 가 담당. 변경 없으면 커밋 안 함

(DB 는 30분 뒤 Supabase 가 이 결과를 당겨갑니다 -- 0장)
```

**5번이 이 설계의 핵심입니다.** API 가 이상한 값을 줬을 때 `latest` 를 덮어쓰면,
다음 날 소비자가 그 잘못된 데이터로 글을 씁니다.
**오래된 데이터가 틀린 데이터보다 낫습니다.**

---

## 3. 품질 검사 — 무엇이 수집을 막는가

### 공통 규칙

| 검사 | 기준 | 결과 |
|---|---|---|
| `_key` 누락 | 하나라도 | 에러 → 격리 |
| 빈 응답 | `record_count == 0` | `status=stale`, latest 유지 |
| 레코드 수 급변 | 전일 대비 **±30% 초과** | `status=quarantined`, latest 유지 |
| `as_of` 역행 | 기준 시점이 과거로 감 | `status=quarantined`, latest 유지 |

### 실거래가 전용 — 레코드 단위

| 검사 | 범위 |
|---|---|
| `price_manwon` | 1,000 ~ 10,000,000 (1천만 ~ 1000억) |
| `area_m2` | 10 ~ 500 |
| `floor` | -5 ~ 100 |
| `deal_date` | 미래 불가, 조회한 월 집합 안 |

범위를 벗어난 **그 레코드만** 격리하고 나머지는 통과시킵니다.
단, **격리 비율이 5% 를 넘으면 전체를 격리**합니다 — 파싱 로직이 깨진 신호입니다.

### 경고 (막지는 않음)

| 검사 | 기준 |
|---|---|
| `removed` 급증 | 전체의 5% 초과 |
| `_key` 충돌 | 카운트만 기록 (실측 1.8%, 정상) |

---

## 4. 저장 대상과 실패 처리

| 대상 | 언제 | 실패하면 |
|---|---|---|
| 파일 (`data/`, `meta.json`) | 수집 직후 | 파이프라인 중단 |
| GitHub 푸시 | 수집 직후 | 다음 회차에 재시도 |
| DB (Supabase) | 30분 뒤 당겨감 | 다음 회차에 재시도 |

DB 가 하루 못 따라와도 데이터는 안 잃습니다. 미러가 진실이고, 다음 날
당겨올 때 그동안의 변화가 한꺼번에 반영됩니다 (upsert 라 여러 번 돌려도
결과가 같습니다).

수동으로 지금 당장 맞추려면:

```sql
select * from public.mkt_pull_from_mirror();   -- DB 에서
```

```bash
python -m ingest sync --all     # 또는 VPS 에서 (SUPABASE_* 환경변수 필요)
```

---

## 5. 실패 알림

| 종료 코드 | 뜻 |
|---|---|
| `0` | 전부 ok |
| `1` | 일부 실패 (stale/quarantined 포함). 커밋은 진행 |
| `2` | **연속 3회 이상 실패** |

`daily.sh` 는 이 코드를 그대로 반환합니다. cron 은 0 이 아니면 로컬 메일을
남기므로, 서버에서 확인하려면:

```bash
tail -50 /opt/jobs-common-data-injest/logs/daily-$(date +%Y-%m-%d).log
```

연속 실패 횟수는 `meta.json` 의 `consecutive_failures` 이고, 성공하면 0 으로
돌아갑니다. 커밋이 계속 올라오므로 저장소 커밋 이력만 봐도 상태를 알 수 있습니다.

> **아직 안 된 것**: 실패를 사람에게 밀어주는 알림(메일·슬랙 등)이 없습니다.
> 지금은 저장소 커밋이 끊기거나 `mkt_source_state.status` 가 `ok` 가 아닌 걸로
> 알아채야 합니다.

---

## 6. 비밀값

| 이름 | 어디 | 없으면 |
|---|---|---|
| `DATA_GO_KR_KEY` | VPS `.env` | 수집이 안 됨 (필수) |
| SSH 배포 키 | VPS `~/.ssh/id_ed25519` | GitHub 푸시가 안 됨 |
| `SUPABASE_SERVICE_ROLE_KEY` | **아무 데도 필요 없음** | — |

DB 가 공개 미러에서 당겨오는 구조라, 관리자 키를 서버에 두지 않습니다.
VPS 가 털려도 DB 를 지울 수는 없습니다.

`.env` 는 `.gitignore` 에 있어 저장소에 올라가지 않습니다.

---

## 7. 백필 (일회성)

과거를 채웁니다. **일일 수집과 다른 경로**로, `latest` 를 건드리지 않고
`series` 에만 씁니다.

```bash
python -m ingest backfill --source molit_apt_trade --from 2024-01 --to 2026-07
```

| 항목 | 값 |
|---|---|
| 요청 간 지연 | **1.0초** (`sources.yml: backfill_delay`) — API 에 부담 주지 않게 |
| 실측 | 31개월 × 8지역 = 248회, **58,486건**, 약 6분, 실패 0 |
| raw 저장 | `data/raw/{source}/backfill-{YYYY-MM}.json.gz` |
| latest | **건드리지 않음** |

이미 2024-01 ~ 2026-07 은 채워져 있습니다. 더 과거가 필요할 때만 다시 도세요.

---

## 8. 시계열이 쌓이는 방식

한 점 = **한 기준시점(`as_of`) 을 한 날(`collected_date`) 에 잰 것**.

일일 수집은 롤링 3개월을 받으므로 **점을 3개** 만듭니다. 한 점에 몰아넣으면
그 점만 3개월치가 돼서, 백필 점(한 달치)과 나란히 놓았을 때 최신 달만 2배로
치솟는 가짜 급등이 그려집니다.

쪼갠 덕에 원래 원하던 게 나옵니다 — 같은 달을 여러 시점에 잰 기록:

```
as_of=2026-08  collected_date=2026-08-19  →  146건
as_of=2026-08  collected_date=2026-09-19  →  (늘어남)
```

신고 지연이 얼마나 메워지는지가 그대로 보입니다.

같은 `(source, as_of, collected_date)` 로 두 번 돌리면 덮어씁니다 —
하루에 두 번 돌려도 점이 두 개 생기지 않습니다.

---

## 9. 저장 크기

| 대상 | 하루치 | 비고 |
|---|---|---|
| `raw/*.json.gz` | **80 KB** | 압축 전 1,916 KB (24배) |
| `latest/*.json` | 1.3 MB | 매일 덮어씀 (누적 아님) |
| `series/*.json` | 84 KB | 32개월 누적 |
| `diff/*.json` | 변화량에 비례 | 변화 없으면 안 만듦 |

`raw` 만 압축합니다. 나머지는 소비자가 `raw.githubusercontent.com` 으로 직접
읽어야 해서 평문입니다.

전국(250개 구)으로 늘리면 `raw` 는 하루 약 1 MB, `latest` 는 약 40 MB 가 됩니다.
`latest` 가 커지면 지역별 분할을 검토하세요 (`docs/API.md` 참고).

---

## 10. 왜 GitHub Actions 가 아니라 VPS 인가

처음엔 GitHub Actions 로 짰다가 옮겼습니다. **러너에서 상류 API 에 연결이 안 됩니다.**

2026-08-19 확인 (`check-api` 워크플로 실행 결과):

```
러너 위치   52.154.140.87  Microsoft Azure, 미국 아이오와
DNS 조회    성공  ->  27.101.236.63
TCP 80      안 열림 (15초 타임아웃)
```

주소는 찾는데 **연결 자체가 안 됩니다.** 느린 게 아니라 막힌 것입니다.
한국 공공 API 가 해외 클라우드 IP 대역을 차단하는 흔한 경우입니다.

실제로 `workflow_dispatch` 로 돌린 수집이 `fetch...` 에서 15분 멈췄고,
그대로 뒀으면 24요청 x 3시도 x (20초 + 백오프) = 약 25분 뒤 전부 실패로
끝났을 것입니다.

| | `apis.data.go.kr` |
|---|---|
| VPS (Hostinger, 말레이시아) | 0.2 ~ 0.3초 |
| GitHub 러너 (Azure, 미국) | **연결 불가** |

코드 문제가 아니라 네트워크라, 설정으로는 못 고칩니다.

> `test` 워크플로(ruff + pytest)는 GitHub 에서 그대로 돕니다. 외부 API 를
> 안 부르기 때문입니다.
