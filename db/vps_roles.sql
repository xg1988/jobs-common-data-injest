-- Supabase 가 미리 만들어 주던 역할들을 직접 만듭니다.
--
-- schema.sql 은 `anon` / `authenticated` / `service_role` 을 씁니다.
-- 그 이름들은 Postgres 기본이 아니라 Supabase 가 얹어 둔 것이라,
-- VPS 에서는 없습니다. 없으면 schema.sql 이 중간에 멈춥니다.
--
-- 여러 번 돌려도 안전합니다. schema.sql **보다 먼저** 돌리세요.
--
-- 쓰는 법 (scripts/vps_db_setup.sh 가 대신 해 줍니다):
--   psql -v authenticator_password="'비밀번호'" -f db/vps_roles.sql

\set ON_ERROR_STOP on

-- 로그인 없는 역할들. 여기엔 변수가 안 들어가므로 DO 블록으로 묶습니다.
do $$
begin
  -- 키 없이 들어온 요청이 되는 역할 (PGRST_DB_ANON_ROLE).
  -- schema.sql 의 "공개 읽기" 정책이 anon/authenticated 를 지목하고 있어서,
  -- 이름을 바꾸면 익명 읽기가 RLS 에 막힙니다.
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;

  -- 쓰기 역할. RLS 를 우회합니다 -- 수집기만 이 역할로 들어옵니다.
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
end
$$;

-- authenticator 는 비밀번호(psql 변수)가 들어갑니다.
--
-- psql 은 달러 인용($$...$$) **안쪽에서는 변수를 바꿔치기하지 않습니다.**
-- 그래서 위 DO 블록 안에 넣으면 :'authenticator_password' 가 그대로 남아
-- 문법 오류가 납니다. 밖에서 만들고 \gexec 로 실행합니다.
select 'create role authenticator noinherit login'
where not exists (select 1 from pg_roles where rolname = 'authenticator')
\gexec

-- noinherit 이 핵심입니다. 이게 없으면 익명 요청이 service_role 권한을
-- 물려받습니다 -- 아무나 쓰기가 됩니다.
select format('alter role authenticator with noinherit login password %L',
               :'authenticator_password')
\gexec

grant anon, authenticated, service_role to authenticator;

grant usage on schema public to anon, authenticated, service_role;

-- 지금 있는 테이블
grant select on all tables    in schema public to anon, authenticated;
grant all    on all tables    in schema public to service_role;
grant usage  on all sequences in schema public to service_role;

-- 앞으로 만들 테이블 (schema.sql 을 이 다음에 돌리므로 이게 있어야 합니다)
alter default privileges in schema public
  grant select on tables to anon, authenticated;
alter default privileges in schema public
  grant all on tables to service_role;
alter default privileges in schema public
  grant usage on sequences to service_role;
