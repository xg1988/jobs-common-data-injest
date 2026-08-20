-- DB 가 공개 GitHub 미러에서 스스로 당겨옵니다.
--
-- 수집은 VPS 가 합니다 (GitHub 러너는 상류 API 연결이 막힘 -- docs/SCHEDULE.md 10장).
-- VPS 가 DB 로 밀어 넣으려면 service_role 키를 VPS 에 둬야 하는데,
-- 당겨오는 방식이면 그 키가 아무 데도 필요 없습니다.

create extension if not exists http with schema extensions;
create extension if not exists pg_cron;

create or replace function public.mkt_pull_from_mirror()
returns table (step text, rows_affected bigint)
language plpgsql
security definer
set search_path = public, extensions
as $fn$
declare
  repo      text := 'xg1988/jobs-common-data-injest';
  sha       text;
  base      text;
  meta_doc  jsonb;
  entry     jsonb;
  latest    jsonb;
  series    jsonb;
  diff_doc  jsonb;
  run_date  date;
  n         bigint;
begin
  -- 0) 어느 커밋을 읽을지 고정 ---------------------------------------------
  -- raw.githubusercontent.com 은 파일마다 따로 캐시합니다. `main` 으로 받으면
  -- meta.json 은 새 것인데 latest 는 옛 것이 오는 일이 생깁니다
  -- (2026-08-20 실제로 겪음: meta 2,850건 / latest 2,813건).
  -- SHA 가 붙은 주소는 내용이 안 바뀌므로 한 커밋 파일이 항상 같이 옵니다.
  select (content::jsonb ->> 'sha') into sha
  from extensions.http_get('https://api.github.com/repos/' || repo || '/commits/main');

  if sha is null then
    step := 'aborted (커밋 SHA 를 못 읽음)'; rows_affected := 0; return next; return;
  end if;

  base := 'https://raw.githubusercontent.com/' || repo || '/' || sha;
  step := 'commit ' || left(sha, 7); rows_affected := 0; return next;

  -- 1) 상태 -----------------------------------------------------------------
  select content::jsonb into meta_doc from extensions.http_get(base || '/meta.json');
  entry := meta_doc -> 'sources' -> 'molit_apt_trade';
  run_date := ((entry ->> 'last_attempt')::timestamptz at time zone 'Asia/Seoul')::date;

  insert into public.mkt_source_state (
    source, status, last_success, last_attempt, consecutive_failures,
    record_count, as_of, as_of_precision, schema_version, quarantined_count, error, updated_at)
  values (
    'molit_apt_trade', entry ->> 'status',
    (entry ->> 'last_success')::timestamptz, (entry ->> 'last_attempt')::timestamptz,
    (entry ->> 'consecutive_failures')::int, (entry ->> 'record_count')::int,
    entry ->> 'as_of', 'month', (entry ->> 'schema_version')::int,
    (entry ->> 'quarantined_count')::int, entry ->> 'error', now())
  on conflict (source) do update set
    status = excluded.status, last_success = excluded.last_success,
    last_attempt = excluded.last_attempt,
    consecutive_failures = excluded.consecutive_failures,
    record_count = excluded.record_count, as_of = excluded.as_of,
    quarantined_count = excluded.quarantined_count,
    error = excluded.error, updated_at = now();
  step := 'source_state (' || (entry ->> 'status') || ')'; rows_affected := 1; return next;

  -- status 가 ok 가 아니면 거래 데이터는 건드리지 않습니다.
  -- 파일 쪽에서 latest 를 안 덮어썼다는 뜻이고, DB 도 똑같이 굽니다.
  if entry ->> 'status' <> 'ok' then
    step := 'skipped (status != ok)'; rows_affected := 0; return next; return;
  end if;

  -- 2) 거래 -----------------------------------------------------------------
  select content::jsonb into latest
  from extensions.http_get(base || '/data/latest/molit_apt_trade.json');

  -- 같은 커밋에서 받았어도 한 번 더 확인합니다. 어긋나면 아무것도 안 씁니다.
  if (latest ->> 'record_count')::int <> (entry ->> 'record_count')::int then
    step := 'aborted (meta ' || (entry ->> 'record_count') ||
            ' vs latest ' || (latest ->> 'record_count') || ' 불일치)';
    rows_affected := 0; return next; return;
  end if;

  with rec as (
    select (latest ->> 'collected_at')::timestamptz as ts,
           jsonb_array_elements(latest -> 'records') as r
  )
  insert into public.mkt_apt_trade (
    key, region_code, dong, apt_name, area_m2, floor, built_year,
    deal_date, price_manwon, canceled, canceled_date, deal_type,
    first_seen_at, last_seen_at)
  select r ->> '_key', r ->> 'region_code', r ->> 'dong', r ->> 'apt_name',
         (r ->> 'area_m2')::numeric, (r ->> 'floor')::int, (r ->> 'built_year')::int,
         (r ->> 'deal_date')::date, (r ->> 'price_manwon')::int,
         (r ->> 'canceled')::boolean, (r ->> 'canceled_date')::date,
         r ->> 'deal_type', ts, ts
  from rec
  on conflict (key) do update set
    canceled = excluded.canceled, canceled_date = excluded.canceled_date,
    deal_type = excluded.deal_type, last_seen_at = excluded.last_seen_at;
  get diagnostics n = row_count;
  step := 'apt_trade'; rows_affected := n; return next;

  -- 3) 시계열 ---------------------------------------------------------------
  select content::jsonb into series
  from extensions.http_get(base || '/data/series/molit_apt_trade.json');

  insert into public.mkt_series_point (
    source, as_of, collected_date, collected_at, record_count, partial, backfill, metrics)
  select 'molit_apt_trade', p ->> 'as_of',
         ((p ->> 'collected_at')::timestamptz)::date, (p ->> 'collected_at')::timestamptz,
         (p ->> 'record_count')::int, coalesce((p ->> 'partial')::boolean, false),
         coalesce((p ->> 'backfill')::boolean, false), coalesce(p -> 'metrics', '{}'::jsonb)
  from jsonb_array_elements(series -> 'points') as p
  on conflict (source, as_of, collected_date) do update set
    record_count = excluded.record_count, metrics = excluded.metrics,
    backfill = excluded.backfill;
  get diagnostics n = row_count;
  step := 'series_point'; rows_affected := n; return next;

  -- 4) 변화 -----------------------------------------------------------------
  -- diff 파일은 변화가 있을 때만 만들어집니다. 없으면 조용히 넘어갑니다.
  begin
    select content::jsonb into diff_doc
    from extensions.http_get(base || '/data/diff/molit_apt_trade/' || run_date || '.json')
    where status = 200;
  exception when others then
    diff_doc := null;
  end;

  if diff_doc is not null then
    insert into public.mkt_apt_trade_event (observed_on, observed_at, event, key, record)
    select run_date, now(), 'added', e ->> '_key', e -> 'record'
    from jsonb_array_elements(diff_doc -> 'added') as e on conflict do nothing;

    insert into public.mkt_apt_trade_event (observed_on, observed_at, event, key, record)
    select run_date, now(), 'removed', e ->> '_key', e -> 'record'
    from jsonb_array_elements(diff_doc -> 'removed') as e on conflict do nothing;

    insert into public.mkt_apt_trade_event (
      observed_on, observed_at, event, key, field, before_value, after_value)
    select run_date, now(), 'changed', e ->> '_key', e ->> 'field', e -> 'before', e -> 'after'
    from jsonb_array_elements(diff_doc -> 'changed') as e on conflict do nothing;

    step := 'events'; rows_affected := jsonb_array_length(diff_doc -> 'added')
                                     + jsonb_array_length(diff_doc -> 'removed')
                                     + jsonb_array_length(diff_doc -> 'changed');
  else
    step := 'events (변화 없음)'; rows_affected := 0;
  end if;
  return next;

  -- 5) 실행 기록 -------------------------------------------------------------
  insert into public.mkt_collection_run (
    source, run_date, collected_at, as_of, as_of_precision, status,
    record_count, partial, notes)
  values ('molit_apt_trade', run_date, (latest ->> 'collected_at')::timestamptz,
          latest ->> 'as_of', 'month', 'ok',
          (latest ->> 'record_count')::int,
          coalesce((latest ->> 'partial')::boolean, false),
          '미러에서 당겨옴 @ ' || left(sha, 7));
  step := 'collection_run'; rows_affected := 1; return next;
end;
$fn$;

-- 이 함수와 http 확장은 밖에서 못 부르게 막습니다.
-- (서버가 임의 주소를 호출하는 통로가 되면 안 됩니다. cron 만 씁니다.)
revoke execute on function public.mkt_pull_from_mirror() from anon, authenticated, public;
revoke usage on schema extensions from anon, authenticated;

-- 매일 00:40 KST = 15:40 UTC
-- VPS 가 00:10 에 수집·푸시하므로 30분 여유를 둡니다.
select cron.unschedule('mkt-pull-from-mirror')
where exists (select 1 from cron.job where jobname = 'mkt-pull-from-mirror');

select cron.schedule(
  'mkt-pull-from-mirror',
  '40 15 * * *',
  'select public.mkt_pull_from_mirror()'
);
