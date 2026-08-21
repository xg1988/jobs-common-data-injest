#!/usr/bin/env bash
#
# VPS 에 DB(Postgres) + PostgREST 를 올립니다. docs/VPS_DB.md 의 1~4단계.
#
# 여러 번 돌려도 안전합니다. 이미 있는 것은 건드리지 않습니다.
#   - 비밀값은 처음 한 번만 만들고 db/.env.db 에 남깁니다
#   - 역할·스키마는 둘 다 "없으면 만든다" 로 되어 있습니다
#   - **데이터는 절대 지우지 않습니다.** 내리려면 사람이 직접 down 하세요
#
#   bash scripts/vps_db_setup.sh
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/db/.env.db"
PROJECT="mktdb"
COMPOSE=(docker compose -f "$REPO/db/compose.yml" --project-directory "$REPO/db" \
         --env-file "$ENV_FILE" -p "$PROJECT")

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---- 0. 먼저 잰다 -----------------------------------------------------------
# 디스크가 모자라면 한참 뒤 백필 도중에 멈춥니다. 지금 아는 게 낫습니다.
say "0) 자리 확인"
df -h "$REPO" | awk 'NR==1 || NR==2'
free -m 2>/dev/null | awk 'NR==1 || NR==2' || true
command -v docker >/dev/null || { echo "docker 가 없습니다."; exit 1; }

# ---- 1. 비밀값 --------------------------------------------------------------
say "1) 비밀값"
if [ -f "$ENV_FILE" ]; then
  echo "이미 있습니다: $ENV_FILE (그대로 씁니다)"
else
  # openssl 이 없는 서버도 있어서 /dev/urandom 으로 만듭니다.
  gen() { head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-40; }
  umask 077
  cat > "$ENV_FILE" <<EOF
# scripts/vps_db_setup.sh 가 만들었습니다. 저장소에 올리지 마세요.
POSTGRES_PASSWORD=$(gen)
AUTHENTICATOR_PASSWORD=$(gen)
JWT_SECRET=$(gen)$(gen)
EOF
  echo "만들었습니다: $ENV_FILE (권한 600)"
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

# ---- 2. 올린다 --------------------------------------------------------------
say "2) 컨테이너"
"${COMPOSE[@]}" up -d
echo "Postgres 가 준비될 때까지 기다립니다..."
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T db pg_isready -U postgres -d market >/dev/null 2>&1; then
    echo "준비됐습니다."; break
  fi
  sleep 2
done

# ---- 3. 역할 + 스키마 -------------------------------------------------------
# 순서가 중요합니다. schema.sql 이 anon/authenticated/service_role 를
# 참조하므로 역할이 먼저입니다.
say "3) 역할"
"${COMPOSE[@]}" exec -T db psql -U postgres -d market \
  -v ON_ERROR_STOP=1 -v authenticator_password="'$AUTHENTICATOR_PASSWORD'" \
  < "$REPO/db/vps_roles.sql"

say "4) 스키마"
"${COMPOSE[@]}" exec -T db psql -U postgres -d market -v ON_ERROR_STOP=1 \
  < "$REPO/db/schema.sql"

# 스키마를 나중에 만들었으니 권한을 한 번 더 얹습니다.
# (alter default privileges 는 이 세션 이후에 만들어진 것에만 걸립니다)
"${COMPOSE[@]}" exec -T db psql -U postgres -d market -v ON_ERROR_STOP=1 -c "
  grant select on all tables    in schema public to anon, authenticated;
  grant all    on all tables    in schema public to service_role;
  grant usage  on all sequences in schema public to service_role;"

# ---- 4. 확인 ----------------------------------------------------------------
say "5) 확인"
sleep 2
echo -n "익명 읽기: "
curl -fsS "http://127.0.0.1:3000/mkt_source_state?select=source" \
  && echo || { echo "실패 -- docker logs mkt-rest 를 보세요"; exit 1; }

TOKEN="$(python3 "$REPO/scripts/make_jwt.py" --secret "$JWT_SECRET" --role service_role)"

# ---- 5. 다음에 할 일 --------------------------------------------------------
say "6) 다음"
cat <<EOF
$REPO/.env 에 아래 두 줄을 넣으세요:

  DB_API_URL=http://127.0.0.1:3000
  DB_API_KEY=$TOKEN

그다음 파일을 DB 로 밀어 넣습니다:

  python -m ingest sync --all

밖에서 읽게 하려면 리버스 프록시(caddy/nginx)로 127.0.0.1:3000 을 TLS 로
내보내세요. Postgres(5432)는 열지 마세요. 자세한 건 docs/VPS_DB.md.
EOF
