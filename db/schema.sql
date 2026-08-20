-- Supabase 스키마. 여러 번 돌려도 안전합니다.
-- 같은 프로젝트에 다른 작업물이 있어 mkt_ 접두사를 씁니다.

-- meta.json 과 같은 계약. 소비자는 이걸 제일 먼저 읽습니다.
create table if not exists public.mkt_source_state (
  source                text primary key,
  status                text not null check (status in ('ok','stale','quarantined','failed')),
  last_success          timestamptz,
  last_attempt          timestamptz,
  consecutive_failures  int  not null default 0,
  record_count          int  not null default 0,
  as_of                 text,
  as_of_precision       text,
  schema_version        int,
  quarantined_count     int  not null default 0,
  partial               boolean not null default false,
  error                 text,
  updated_at            timestamptz not null default now()
);

create table if not exists public.mkt_collection_run (
  id                 bigint generated always as identity primary key,
  source             text not null,
  run_date           date not null,
  collected_at       timestamptz not null,
  as_of              text not null,
  as_of_precision    text not null,
  status             text not null,
  record_count       int  not null default 0,
  quarantined_count  int  not null default 0,
  partial            boolean not null default false,
  added              int,
  removed            int,
  changed            int,
  notes              text,
  errors             text[] not null default '{}',
  warnings           text[] not null default '{}',
  raw_url            text
);
create index if not exists mkt_collection_run_source_date_idx
  on public.mkt_collection_run (source, run_date desc);

-- 한 점 = 한 기준시점(as_of) 을 한 날(collected_date) 에 잰 것.
create table if not exists public.mkt_series_point (
  source          text not null,
  as_of           text not null,
  collected_date  date not null,
  collected_at    timestamptz not null,
  record_count    int  not null,
  partial         boolean not null default false,
  backfill        boolean not null default false,
  metrics         jsonb not null default '{}'::jsonb,
  primary key (source, as_of, collected_date)
);
create index if not exists mkt_series_point_source_asof_idx
  on public.mkt_series_point (source, as_of);

-- 거래 현재 상태. 한 행 = 한 거래.
create table if not exists public.mkt_apt_trade (
  key            text primary key,
  region_code    text not null,
  dong           text not null,
  apt_name       text not null,
  area_m2        numeric(10,4) not null,
  floor          int  not null,
  built_year     int,
  deal_date      date not null,
  price_manwon   int  not null,
  canceled       boolean not null default false,
  canceled_date  date,
  deal_type      text,
  first_seen_at  timestamptz not null,
  last_seen_at   timestamptz not null
);
comment on column public.mkt_apt_trade.canceled is
  '해제(취소) 거래. 삭제하지 않고 플래그만 둡니다. 집계할 때는 기본적으로 제외하세요 -- 안 그러면 취소된 신고가가 역대 최고가로 잡힙니다.';
comment on column public.mkt_apt_trade.price_manwon is '단위: 만원';

create index if not exists mkt_apt_trade_region_deal_idx on public.mkt_apt_trade (region_code, deal_date desc);
create index if not exists mkt_apt_trade_deal_date_idx   on public.mkt_apt_trade (deal_date desc);
create index if not exists mkt_apt_trade_live_idx        on public.mkt_apt_trade (region_code, deal_date desc) where not canceled;

create table if not exists public.mkt_apt_trade_event (
  id           bigint generated always as identity primary key,
  observed_on  date not null,
  observed_at  timestamptz not null,
  event        text not null check (event in ('added','removed','changed')),
  key          text not null,
  field        text,
  before_value jsonb,
  after_value  jsonb,
  record       jsonb
);
create index if not exists mkt_apt_trade_event_observed_idx on public.mkt_apt_trade_event (observed_on desc, event);
create index if not exists mkt_apt_trade_event_key_idx      on public.mkt_apt_trade_event (key);
-- 같은 날 두 번 당겨와도 이벤트가 겹치지 않게
create unique index if not exists mkt_apt_trade_event_dedup_idx
  on public.mkt_apt_trade_event (observed_on, event, key, coalesce(field, ''));

-- ---------------------------------------------------------------------------
-- upsert 는 보낸 컬럼을 전부 덮어씁니다. first_seen_at(첫 관측 시각)까지
-- 매일 갱신되면 "언제 처음 신고됐나" 를 잃습니다. 더 이른 값을 지킵니다.
-- ---------------------------------------------------------------------------
create or replace function public.mkt_keep_first_seen()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  new.first_seen_at := least(old.first_seen_at, new.first_seen_at);
  return new;
end;
$$;
revoke execute on function public.mkt_keep_first_seen() from anon, authenticated, public;

drop trigger if exists mkt_apt_trade_keep_first_seen on public.mkt_apt_trade;
create trigger mkt_apt_trade_keep_first_seen
  before update on public.mkt_apt_trade
  for each row execute function public.mkt_keep_first_seen();

-- ---------------------------------------------------------------------------
-- RLS: 누구나 읽기, 쓰기는 service_role 만 (service_role 은 RLS 를 우회합니다)
-- ---------------------------------------------------------------------------
alter table public.mkt_source_state      enable row level security;
alter table public.mkt_collection_run    enable row level security;
alter table public.mkt_series_point      enable row level security;
alter table public.mkt_apt_trade         enable row level security;
alter table public.mkt_apt_trade_event   enable row level security;

do $$
declare t text;
begin
  foreach t in array array['mkt_source_state','mkt_collection_run','mkt_series_point',
                           'mkt_apt_trade','mkt_apt_trade_event'] loop
    if not exists (select 1 from pg_policies
                   where schemaname='public' and tablename=t and policyname='공개 읽기') then
      execute format('create policy "공개 읽기" on public.%I for select to anon, authenticated using (true)', t);
    end if;
  end loop;
end $$;
