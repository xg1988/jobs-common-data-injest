# DB 쪽 코드

Supabase 에 올라가 있는 것들입니다. 마이그레이션으로 적용했고, 여기 사본을
둡니다 (저장소만 봐도 DB 가 뭘 하는지 알 수 있게).

| 파일 | 내용 |
|---|---|
| `schema.sql` | 테이블 5개 + RLS + 인덱스 + first_seen_at 보존 트리거 |
| `pull_from_mirror.sql` | 공개 미러에서 당겨오는 함수 + pg_cron 등록 |

## 왜 DB 가 당겨오나

수집은 VPS 가 합니다 (GitHub 러너는 상류 API 연결이 막힘 -- `docs/SCHEDULE.md` 10장).
VPS 가 DB 로 밀어 넣으려면 `service_role` 키를 VPS 에 둬야 하는데,
**당겨오는 방식이면 그 키가 아무 데도 필요 없습니다.**
미러가 공개 저장소라 인증 없이 읽힙니다. VPS 가 털려도 DB 는 못 건드립니다.

## 현재 등록된 작업

```sql
select jobid, schedule, jobname, active from cron.job;
--  1 | 40 15 * * * | mkt-pull-from-mirror | t     -- 매일 00:40 KST

-- 최근 실행 이력
select j.jobname, d.status,
       d.start_time at time zone 'Asia/Seoul' as 시작_KST,
       d.return_message
from cron.job_run_details d join cron.job j using (jobid)
order by d.start_time desc limit 10;
```

## 다시 적용하려면

`schema.sql` -> `pull_from_mirror.sql` 순서로 실행하세요.
둘 다 여러 번 돌려도 안전합니다 (`if not exists` / `create or replace`).
