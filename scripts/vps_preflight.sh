#!/usr/bin/env bash
#
# 새 VPS 를 쓸 수 있는지 **계약 전에** 잽니다.
#
#   bash vps_preflight.sh                # 상류 도달성만 (저장소 없이도 됩니다)
#   DATA_GO_KR_KEY=... bash vps_preflight.sh   # 실제 응답까지 확인
#
# 이 스크립트만 새 서버에 scp 해서 돌려도 됩니다. 저장소를 안 씁니다.
#
# 제일 중요한 건 1번입니다. 한국 공공 API 는 해외 클라우드 IP 대역을
# 흔히 차단합니다. GitHub Actions 러너(미국 Azure)가 정확히 그래서
# 못 씁니다 -- DNS 는 되는데 TCP 가 안 열립니다. 이걸 확인 안 하고 옮기면
# 서버를 다 옮긴 다음에야 수집이 안 되는 걸 알게 됩니다.

UP_HOST="apis.data.go.kr"
UP_PATH="/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
fail=0
warn=0
up_fail=0

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31m실패\033[0m  %s\n' "$*"; fail=$((fail+1)); }
soft() { printf '  \033[33m주의\033[0m  %s\n' "$*"; warn=$((warn+1)); }
say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "0) 여기가 어디인가"
curl -s --max-time 8 https://ipinfo.io/json 2>/dev/null \
  | tr -d '{}", ' | grep -E '^(ip|city|region|country|org):' || echo "  (못 읽음)"

say "1) 상류 API 에 닿는가  <- 이게 안 되면 여기서 멈추세요"
if ! getent hosts "$UP_HOST" >/dev/null 2>&1 && ! nslookup "$UP_HOST" >/dev/null 2>&1; then
  bad "DNS 조회 실패: $UP_HOST"
  up_fail=1
else
  ok "DNS 조회"
  # 80 포트가 열리는지. 막힌 서버는 여기서 타임아웃납니다.
  start=$(date +%s)
  if timeout 15 bash -c "cat < /dev/null > /dev/tcp/$UP_HOST/80" 2>/dev/null; then
    ok "TCP 80 열림 ($(( $(date +%s) - start ))초)"
  else
    bad "TCP 80 안 열림 (15초 타임아웃) -- 이 서버에서는 수집이 안 됩니다"
    up_fail=1
  fi

  if [ -n "${DATA_GO_KR_KEY:-}" ]; then
    body=$(curl -s --max-time 20 \
      "http://$UP_HOST$UP_PATH?serviceKey=$DATA_GO_KR_KEY&LAWD_CD=11680&DEAL_YMD=202606&numOfRows=1")
    case "$body" in
      *"<resultCode>00"*|*'"resultCode":"00"'*) ok "실제 응답 정상 (resultCode 00)" ;;
      "")            bad "실제 호출: 빈 응답"; up_fail=1 ;;
      *SERVICE_KEY*) soft "키 문제로 보입니다 -- 네트워크는 열렸습니다" ;;
      *)             soft "응답은 왔는데 resultCode 00 이 아닙니다: $(echo "$body" | head -c 160)" ;;
    esac
  else
    soft "DATA_GO_KR_KEY 가 없어 실제 호출은 건너뜁니다 (TCP 만 확인)"
  fi
fi

say "2) GitHub 에 닿는가 (미러 푸시·소비자 읽기)"
curl -sfI --max-time 10 https://github.com >/dev/null && ok "github.com" || bad "github.com"
curl -sfI --max-time 10 https://raw.githubusercontent.com >/dev/null \
  && ok "raw.githubusercontent.com" || bad "raw.githubusercontent.com"

say "3) 파이썬"
PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  bad "python3 이 없습니다"
else
  v=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  # pyproject 가 py311 을 겨냥합니다.
  case "$v" in 3.1[1-9]|3.[2-9][0-9]) ok "python $v ($PY)" ;;
                                   *) bad "python $v -- 3.11 이상이 필요합니다" ;;
  esac
  "$PY" -m venv --help >/dev/null 2>&1 && ok "venv" || bad "python3-venv 가 없습니다"
fi

say "4) 도구"
for c in git curl; do
  command -v "$c" >/dev/null && ok "$c" || bad "$c 가 없습니다"
done
command -v docker >/dev/null && ok "docker (DB 를 여기 올릴 수 있습니다)" \
  || soft "docker 없음 -- DB 를 이 서버에 둘 거면 필요합니다 (docs/VPS_DB.md)"
command -v crontab >/dev/null && ok "crontab" || bad "cron 이 없습니다"

say "5) 자리"
# 필드를 뒤에서 셉니다. 장치 이름에 공백이 있으면 $2/$4 가 밀립니다.
df -Ph / | awk 'NR==2 {print "  디스크 여유: " $(NF-2) " / " $(NF-4)}'
avail=$(df -Pk / | awk 'NR==2 {print int($(NF-2)/1024/1024)}')
[ "${avail:-0}" -ge 10 ] && ok "디스크 ${avail}GB" \
  || soft "디스크 ${avail}GB -- DB 까지 올리려면 10GB 이상 권합니다"
if [ -r /proc/meminfo ]; then
  mem=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  [ "$mem" -ge 1024 ] && ok "메모리 ${mem}MB" || soft "메모리 ${mem}MB -- 1GB 이상 권합니다"
fi

say "6) 시간"
echo "  현재: $(date '+%Y-%m-%d %H:%M:%S %Z')  (UTC 기준으로 cron 을 걸 것이므로 참고만)"
if [ -f /usr/share/zoneinfo/Asia/Seoul ]; then
  ok "tzdata (Asia/Seoul 있음)"
else
  soft "tzdata 가 없습니다 -- crontab 의 CRON_TZ=Asia/Seoul 이 안 먹습니다 (apt install tzdata)"
fi

say "결과"
if [ "$up_fail" -gt 0 ]; then
  echo "  ★ 상류 API 에 못 닿습니다. 이 호스팅은 쓸 수 없습니다 -- 다른 곳을 알아보세요."
  echo "    설정으로 못 고칩니다. 네트워크 차단입니다 (docs/SCHEDULE.md 10장)."
  exit 1
fi
if [ "$fail" -gt 0 ]; then
  echo "  상류는 열렸는데 나머지에서 $fail 개 실패했습니다 -- 대부분 설치로 해결됩니다."
  echo "  예: apt install -y git curl cron python3-venv tzdata"
  exit 1
fi
echo "  통과 (주의 $warn 개). scripts/vps_bootstrap.sh 로 넘어가세요."
