#!/usr/bin/env bash
#
# 새 VPS 에 수집기를 세웁니다. preflight 를 통과한 다음에 돌리세요.
#
#   bash vps_bootstrap.sh
#
# 이 스크립트만 새 서버에 scp 해서 돌려도 됩니다 (저장소를 여기서 받습니다).
#
# **cron 은 걸지 않습니다.** 옛 서버와 겹쳐 돌면 같은 커밋을 두 서버가
# 밀어 올려 푸시가 엉킵니다. 컷오버는 사람이 순서대로 -- docs/VPS_MOVE.md.
#
set -euo pipefail

REPO_URL="https://github.com/xg1988/jobs-common-data-injest.git"
DEST="${DEST:-/opt/jobs-common-data-injest}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1) 코드"
if [ -d "$DEST/.git" ]; then
  echo "이미 있습니다: $DEST"
  git -C "$DEST" pull --rebase --autostash origin main
else
  mkdir -p "$(dirname "$DEST")"
  git clone "$REPO_URL" "$DEST"
fi
cd "$DEST"

say "2) 파이썬"
PY=$(command -v python3 || command -v python)
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
echo "$(./.venv/bin/python -V) 준비됨"

say "3) 비밀값"
if [ -f .env ]; then
  echo ".env 가 이미 있습니다 (그대로 둡니다)"
else
  cp .env.example .env
  chmod 600 .env
  echo ".env 를 만들었습니다. **DATA_GO_KR_KEY 를 채우세요.**"
  echo "  옛 서버에서:  scp 옛서버:$DEST/.env ."
fi

say "4) 돌아가는지 본다 (파일 안 씀)"
if grep -qE '^DATA_GO_KR_KEY=.+' .env; then
  ./.venv/bin/python -m ingest run --source molit_apt_trade --dry-run --verbose | tail -20
else
  echo "DATA_GO_KR_KEY 가 비어 있어 건너뜁니다. 채운 뒤 이걸 돌리세요:"
  echo "  ./.venv/bin/python -m ingest run --source molit_apt_trade --dry-run --verbose"
fi

say "5) GitHub 에 밀어 올릴 수 있는가"
# 미러 푸시가 이 서버의 유일한 '쓰기' 입니다. 안 되면 수집만 하고
# 아무 데도 못 올립니다 -- 조용히 그렇게 되는 게 제일 나쁩니다.
if git ls-remote origin >/dev/null 2>&1; then
  echo "읽기 OK. 쓰기 권한은 컷오버 때 첫 커밋으로 확인됩니다."
  echo "SSH 키를 쓰려면:"
  echo "  ssh-keygen -t ed25519 -C 'cmn-vps-new' -f ~/.ssh/id_ed25519 -N ''"
  echo "  cat ~/.ssh/id_ed25519.pub   # GitHub > Settings > Deploy keys (write 체크)"
  echo "  git -C $DEST remote set-url origin git@github.com:xg1988/jobs-common-data-injest.git"
else
  echo "origin 에 못 붙습니다. 네트워크나 인증을 보세요."
fi

say "다음"
cat <<EOF
1. .env 의 DATA_GO_KR_KEY 를 채웁니다 (옛 서버에서 그대로 가져오면 됩니다)
2. 푸시 권한을 붙입니다 (위 SSH 키 안내)
3. DB 도 이 서버에 둘 거면:  bash scripts/vps_db_setup.sh
4. 컷오버는 docs/VPS_MOVE.md 순서대로 -- **옛 서버 cron 을 먼저 끕니다**

cron 은 아직 안 걸었습니다. 컷오버 때 이 줄을 넣으세요:

  CRON_TZ=Asia/Seoul
  10 0 * * * $DEST/scripts/daily.sh
EOF
