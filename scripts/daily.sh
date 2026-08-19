#!/usr/bin/env bash
#
# 매일 도는 수집 스크립트. VPS 의 cron 이 부릅니다.
#
# GitHub Actions 에서 못 도는 이유:
#   러너(미국 Azure)에서 apis.data.go.kr:80 연결이 막힙니다.
#   DNS 는 되는데 TCP 가 안 열립니다 (2026-08-19 확인).
#   그래서 수집은 여기서 하고, 결과만 GitHub 로 밀어 올립니다.
#
# 설치:
#   crontab -e
#   10 0 * * * /opt/jobs-common-data-injest/scripts/daily.sh
#
set -uo pipefail

REPO="/opt/jobs-common-data-injest"
PY="$REPO/.venv/bin/python"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/daily-$(date +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1

echo "================================================================"
echo "시작: $(date '+%Y-%m-%d %H:%M:%S %Z')"
cd "$REPO" || exit 1

# ---- 1. 최신 코드 받기 ------------------------------------------------------
# 남이 고친 게 있으면 반영합니다.
# --autostash: 남아 있는 로컬 변경(파일 모드 등)을 잠깐 치웠다 되돌립니다.
#              이게 없으면 사소한 변경 하나에 수집 전체가 멈춥니다.
git fetch --quiet origin main
if ! git pull --quiet --rebase --autostash origin main; then
  echo "!! 코드 받기 실패 -- 충돌을 수동으로 푸세요. 이번 회차는 건너뜁니다."
  git rebase --abort 2>/dev/null
  exit 1
fi

# ---- 2. 수집 ---------------------------------------------------------------
"$PY" -m ingest run --all --verbose
RUN_CODE=$?
echo "수집 종료 코드: $RUN_CODE"

# ---- 3. 커밋 & 푸시 ---------------------------------------------------------
# 수집이 실패했어도 여기까지 옵니다. raw/ 와 meta.json 이 저장소에 남아야
# 나중에 원인을 볼 수 있습니다.
git add data/ meta.json
if git diff --staged --quiet; then
  echo "변경 없음 -- 커밋 건너뜀"
else
  git commit --quiet -m "collect: $(date +%Y-%m-%d)"
  if git push --quiet origin main; then
    echo "푸시 완료"
  else
    echo "!! 푸시 실패 -- 다음 회차에 다시 시도됩니다"
  fi
fi

# ---- 4. 종료 코드 -----------------------------------------------------------
# 2 = 연속 3회 이상 실패. cron 이 메일을 보내도록 그대로 넘깁니다.
echo "끝: $(date '+%Y-%m-%d %H:%M:%S %Z')"
exit $RUN_CODE
