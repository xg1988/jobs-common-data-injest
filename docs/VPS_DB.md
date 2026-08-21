# DB 를 VPS 로 옮기기

## 왜

Supabase 무료 조직은 **활성 프로젝트가 2개**까지입니다. 이 조직에는 4개가
있고, `xg1988's Project`(실거래가 DB)가 정지(paused)됐습니다.

정지되면 서브도메인이 **DNS 에서 사라집니다.**

```
$ nslookup hmsyfipqrvdfitzmuaph.supabase.co
*** Non-existent domain
```

그래서:

| | |
|---|---|
| 소비자 | PostgREST 를 못 읽습니다. HTTP 에러도 아니고 DNS 실패입니다 |
| pg_cron | 안 돕니다. 미러에서 당겨오는 게 멈췄습니다 |
| 감시견 | **못 잡습니다.** `meta.json`(저장소 파일)만 보니까요 |

세 번째가 제일 나쁩니다. 주 저장소가 내려갔는데 화면과 알림은 조용했습니다.

무료 한도(500 MB)도 어차피 전국 기준 19개월이면 찹니다
([STORAGE.md](STORAGE.md)). 옮기면 두 문제가 같이 없어집니다.

---

## 옮겨도 안 바뀌는 것

**PostgREST 를 그대로 씁니다.** Supabase 의 API 는 PostgREST 이고, 그건
오픈소스라 VPS 에서도 똑같이 뜹니다. 그래서:

| | 바뀌나 |
|---|---|
| 소비자 쿼리 문자열 (`?select=...&region_code=eq.11680&order=...`) | ✗ 그대로 |
| 테이블·컬럼 이름 | ✗ 그대로 |
| `ingest/db.py` | ✗ 그대로 (주소·키만 환경변수로) |
| `db/schema.sql` | 거의 그대로 (역할 이름만 만들어 주면 됩니다) |
| **base URL** | ✅ 바뀝니다 |
| **읽기 키** | ✅ 바뀝니다 |

소비자가 고칠 건 **두 줄**입니다.

```diff
- BASE=https://hmsyfipqrvdfitzmuaph.supabase.co/rest/v1
- KEY=sb_publishable_...
+ BASE=https://<새 도메인>/rest/v1
+ KEY=<새 공개 읽기 키>
```

---

## 옮기면 바뀌는 것

### 1. `pull_from_mirror` 가 필요 없어집니다

지금 구조는 이렇습니다.

```
VPS 수집 -> GitHub 미러 -> (pg_cron) Supabase 가 당겨옴
```

DB 가 VPS 안에 있으면 당겨올 이유가 없습니다. 수집 직후 바로 씁니다.

```
VPS 수집 -> 파일 -> GitHub 미러 (그대로 유지)
              \-> localhost:3000 (PostgREST) -> Postgres
```

`ingest sync` 가 이미 그 일을 합니다. `scripts/daily.sh` 에 한 줄
추가하면 끝입니다. `db/pull_from_mirror.sql` 은 **지우지 말고 남겨 두세요** —
되돌릴 때 그대로 씁니다.

### 2. 보안 성질이 바뀝니다 (중요)

지금은 관리자 키가 **아무 데도 없습니다.** DB 가 공개 미러에서 스스로
당겨오기 때문입니다. VPS 가 털려도 DB 는 못 지웁니다.

옮기면 그 성질이 사라집니다. DB 가 VPS 안에 있으니, VPS 가 털리면 DB 도
털립니다. 이건 **잃는 것**입니다. 대신 이렇게 줄입니다.

- PostgREST 를 `127.0.0.1:3000` 에만 묶고, 밖으로는 리버스 프록시가
  **읽기 전용 역할**로만 노출합니다
- 쓰기 키는 VPS 안에서만 씁니다 (`DB_API_URL=http://127.0.0.1:3000`)
- Postgres 포트(5432)는 **절대 열지 않습니다**
- 매일 `pg_dump` 를 떠서 별도 위치에 둡니다 (아래 5단계)

### 3. 아카이브 압박이 사라집니다

500 MB 한도가 없어지므로 12개월 hot 정책을 고집할 이유가 없습니다.
5년치 전부(약 1.5 GB) DB 에 두는 게 가능합니다 — **디스크가 되면.**
`ingest archive` 는 그대로 두세요. 백업을 가볍게 만드는 데 여전히 씁니다.

---

## 옮기기 전에 확인할 것 세 가지

이게 안 되면 옮겨도 소용없습니다. **먼저 재세요.**

| 확인 | 어떻게 | 안 되면 |
|---|---|---|
| **소비자가 새 도메인에 닿는가** | 블로그 자동화 컨테이너에서 `curl -sI https://<새 도메인>/rest/v1/` | 이게 제일 큽니다. 그 컨테이너는 공공 API 도메인이 전부 막혀 있어서, 새 도메인도 막힐 수 있습니다. 막히면 소비자는 GitHub 미러(조각 파일)로만 읽어야 합니다 |
| **디스크** | `df -h /` | 5년치 1.5 GB + 인덱스 + 백업. 최소 10 GB 여유 |
| **메모리** | `free -m` | Postgres + PostgREST 최소 1 GB. 수집 파이썬과 같이 도는 걸 감안 |

---

## 순서

### 1) 올린다

`/opt/jobs-common-data-injest/db/compose.yml`

```yaml
services:
  db:
    image: postgres:17
    restart: unless-stopped
    environment:
      POSTGRES_PASSWORD: ${PGPASSWORD}
      POSTGRES_DB: market
    volumes: ["./pgdata:/var/lib/postgresql/data"]
    ports: ["127.0.0.1:5432:5432"]      # 밖으로 열지 않습니다
  rest:
    image: postgrest/postgrest:v12.2.3
    restart: unless-stopped
    environment:
      PGRST_DB_URI: postgres://authenticator:${AUTHPASS}@db:5432/market
      PGRST_DB_SCHEMAS: public
      PGRST_DB_ANON_ROLE: web_anon
      PGRST_JWT_SECRET: ${JWT_SECRET}    # 32자 이상
    ports: ["127.0.0.1:3000:3000"]
    depends_on: [db]
```

### 2) 역할을 만든다

`schema.sql` 은 `anon` / `authenticated` / `service_role` 을 씁니다.
Supabase 가 미리 만들어 두는 이름이라, VPS 에서는 직접 만듭니다.

```sql
create role web_anon nologin;
create role anon nologin;                      -- schema.sql 의 정책이 씁니다
create role authenticated nologin;
create role service_role nologin bypassrls;
create role authenticator noinherit login password '...';
grant web_anon, anon, authenticated, service_role to authenticator;

grant usage on schema public to web_anon, anon, authenticated, service_role;
grant select on all tables in schema public to web_anon, anon, authenticated;
grant all    on all tables in schema public to service_role;
alter default privileges in schema public
  grant select on tables to web_anon, anon, authenticated;
```

그다음 `db/schema.sql` 을 그대로 실행합니다. RLS 정책("공개 읽기")이
그대로 붙습니다.

### 3) 데이터를 넣는다

Supabase 를 되살릴 수 있으면 `pg_dump` 가 제일 깨끗합니다. 정지된 채로
두겠다면 **미러에서 다시 만듭니다** — 그러라고 미러가 있습니다.

```bash
python -m ingest sync --all          # 파일 -> DB
python -m ingest restore --source molit_apt_trade --from 2024-01 --to 2026-07
```

### 4) 주소를 바꾼다

VPS `.env`:

```
DB_API_URL=http://127.0.0.1:3000
DB_API_KEY=<service_role 로 서명한 JWT>
```

`ingest/db.py` 는 `DB_API_*` 를 먼저 보고, 없으면 예전 `SUPABASE_*` 로
물러섭니다. 그래서 **한 줄씩 옮겨도 중간에 안 깨집니다.**

`scripts/daily.sh` 의 수집 다음 줄에 추가:

```bash
"$PY" -m ingest sync --all
```

### 5) 백업

DB 가 유일한 사본인 구간이 생깁니다. 매일 뜨세요.

```bash
0 3 * * * docker exec db pg_dump -U postgres market | gzip > /backup/market-$(date +\%F).sql.gz
```

미러(GitHub)에 `latest`·`series`·`raw` 가 남아 있어서 최근 것은 복구
가능하지만, 아카이브로 넘긴 과거는 파일이 유일한 사본입니다.

### 6) 소비자에게 알린다

`README.md` 와 `docs/API.md` 의 `BASE`/`KEY` 를 바꾸고, 새 주소가 뜨는 걸
확인한 다음에 알립니다. 확인 전에 알리면 두 번 알리게 됩니다.

---

## 되돌리려면

Supabase 프로젝트를 restore 하고 `.env` 의 `DB_API_*` 두 줄을 지우면
됩니다. `SUPABASE_*` 로 자동으로 물러섭니다. `pull_from_mirror.sql` 은
그대로 남아 있으니 pg_cron 도 다시 돕니다.

---

## 옮기든 안 옮기든 해야 할 것

**감시견이 DB 를 안 봅니다.** 이번에 정지된 걸 아무도 못 알아챈 이유입니다.
`meta.json` 만 보지 말고, 공개 읽기 주소로 한 번 찔러서

- 응답하는가
- `mkt_source_state.last_attempt` 가 `meta.json` 과 같은가

를 봐야 합니다. DB 가 어디에 있든 필요한 검사입니다.
