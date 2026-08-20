# 아카이브 — 오래된 달을 DB에서 파일로

## 왜

Supabase 무료 한도는 500 MB입니다. 전국 거래는 한 달 약 4.4만 건, 행당 621 바이트라 **19개월이면 꽉 찹니다.** 5년치(약 1.5 GB)는 들어가지 않습니다.

그렇다고 지울 수는 없습니다. 그래서 오래된 달은 파일로 내보내고 DB에서만 비웁니다. **데이터는 없어지지 않습니다.**

| | 크기 |
|---|---|
| 한 달치 파일 (gzip) | 약 830 KB |
| 5년치 60개 전부 | 약 50 MB |
| 저장소에 그대로 들어가나 | 예 |

## 순서가 전부입니다

```
① DB에서 그 달을 전부 읽는다
② 파일로 쓴다
③ 다시 읽어서 행 수와 sha256이 맞는지 본다   ← 이걸 건너뛰면 안 됩니다
④ 맞을 때만 DB에서 지운다
```

③이 없으면 언젠가 유일한 사본을 날립니다. 쓰기가 도중에 끊겼는지 디스크가 찼는지는 **다시 읽어 보기 전에는 알 수 없습니다.**

`--evict` 없이 실행하면 ①~③만 하고 DB는 건드리지 않습니다. 처음에는 이렇게 돌려서 파일을 확인하세요.

## 안 지우는 경우

`evict`는 아래 중 하나라도 걸리면 **거부하고 멈춥니다.** 지우지 않습니다.

| 상황 | 이유 |
|---|---|
| 아카이브 기록이 없음 | DB가 유일한 사본입니다 |
| 파일이 없음 | 위와 같습니다 |
| 파일 sha256이 기록과 다름 | 파일이 손상됐습니다 |
| DB 행 수 ≠ 아카이브 행 수 | 아카이브 후에 정정 신고가 들어왔습니다. 다시 아카이브하세요 |

## 쓰는 법

```bash
python -m ingest archive --source molit_apt_trade --dry-run
```

```bash
python -m ingest archive --source molit_apt_trade --hot-months 12
```

```bash
python -m ingest archive --source molit_apt_trade --hot-months 12 --evict
```

`--hot-months 12`는 **이번 달 포함 최근 12개월**을 DB에 남깁니다. 오늘이 2026-08이면 2025-09 ~ 2026-08이 남고, 2025-08 이하가 대상입니다.

## 아카이브된 기록은 어떻게 보나

세 가지 방법이 있습니다. 편한 것부터.

### ① `ingest query` — DB든 파일이든 알아서 찾습니다

기간만 주면 됩니다. 어디에 있는지 몰라도 됩니다.

```bash
python -m ingest query --source molit_apt_trade --from 2023-01 --to 2026-08 --region 11680
```

```
2023-01 ~ 2026-08  지역 11680  ->  4,812건
  출처: archive 32개월, db 12개월
  가격(만원)  중앙값 198,000  최저 61,000  최고 1,050,000
```

`--json`을 붙이면 원본이 그대로 나옵니다. 다른 프로그램에서 쓸 때는 이쪽입니다.

**출처 줄을 꼭 보세요.** 아카이브 파일이 통째로 빠져도 숫자만 보면 "그 시기엔 거래가 적었나 보다"로 넘어갑니다. 못 읽은 달이 있으면 `⚠ 못 읽은 달`로 따로 나옵니다.

### ② 파일을 직접 — 도구가 필요 없습니다

NDJSON + gzip이라 한 줄에 한 거래입니다.

```bash
zcat data/archive/molit_apt_trade/2024-03.ndjson.gz | jq -r 'select(.region_code=="11680") | [.deal_date, .apt_name, .price_manwon] | @tsv'
```

저장소에서 바로 받아도 됩니다.

```bash
curl -sL https://raw.githubusercontent.com/xg1988/jobs-common-data-injest/main/data/archive/molit_apt_trade/2024-03.ndjson.gz | gunzip | head
```

무엇이 있는지는 `index.json`을 보면 됩니다. 달마다 행 수·크기·sha256·비운 시각이 들어 있습니다.

```bash
curl -sL https://raw.githubusercontent.com/xg1988/jobs-common-data-injest/main/data/archive/molit_apt_trade/index.json | jq '.months | keys'
```

### ③ SQL로 뜯어봐야 할 때 — DB로 되돌립니다

```bash
python -m ingest restore --source molit_apt_trade --from 2024-01 --to 2024-12
```

파일은 그대로 둡니다. 다 본 뒤에 `--evict`로 다시 비우면 됩니다.

한 해치(약 53만 행)는 Supabase 무료 한도의 60% 정도를 씁니다. 되돌린 채로 두지 마세요.

## 파일 형식을 NDJSON으로 잡은 이유

Parquet이 5~10배 작고 분석도 빠릅니다. 그런데 `pyarrow`가 필요하고, 소비자가 `zcat | jq`로 바로 못 봅니다.

이 계층의 목적은 **누구나 읽을 수 있는 것**입니다. 5년 뒤에 이 파일을 여는 사람이 우리가 쓰던 라이브러리를 갖고 있을 거라고 가정하지 않습니다. gzip과 JSON은 어디에나 있습니다.

한 달 830 KB는 아껴서 얻을 게 없는 크기입니다.

## 같은 입력이면 같은 파일

gzip은 기본으로 타임스탬프를 넣습니다. 그대로 두면 내용이 하나도 안 바뀌어도 매번 다른 파일이 나와서

- sha256 검증이 무의미해지고
- 저장소에 쓸데없는 diff가 쌓입니다

그래서 `mtime=0`으로 고정하고 JSON 키를 정렬합니다. 같은 달을 다시 만들면 **바이트까지 같은 파일**이 나옵니다.

## 언제 도나

일일 수집과 분리해서 **월 1회**만 돌리면 충분합니다. 매일 돌 이유가 없고, 매일 도는 것에 삭제를 붙이면 사고 확률만 올라갑니다.

```
0 4 1 * *   cd /opt/jobs-common-data-injest && .venv/bin/python -m ingest archive --source molit_apt_trade --hot-months 12 --evict >> logs/archive.log 2>&1
```

돌린 뒤 `data/archive/`를 커밋해야 파일이 저장소에 올라갑니다. `scripts/daily.sh`가 `data/` 전체를 커밋하므로 다음 일일 수집 때 같이 올라갑니다.
