# VPS 를 다른 곳으로 이사하기

DB 를 VPS 로 옮기는 이야기는 [VPS_DB.md](VPS_DB.md) 에 있습니다. 이 문서는
**서버 자체를 다른 호스팅으로 옮기는** 이야기입니다.

---

## 먼저: 이 서버에 유일한 사본이 있나

**없습니다.** 그래서 이사가 어렵지 않습니다.

| 이 서버에 있는 것 | 유일한 사본인가 | 어떻게 되살리나 |
|---|---|---|
| 저장소 클론 | ✗ | `git clone` |
| `.venv` | ✗ | `pip install -r requirements.txt` |
| `data/`, `meta.json` | ✗ | GitHub 미러에 그대로 있습니다 |
| `logs/` | ✅ (저장소에 안 올라감) | **버려도 됩니다.** 원인 조사용이라 필요하면 미리 내려받으세요 |
| `.env` 의 `DATA_GO_KR_KEY` | ✅ | 포털에서 다시 받을 수 있지만, 그냥 복사가 빠릅니다 |
| SSH 배포 키 | ✅ | 새로 만들어 GitHub 에 등록하면 됩니다 |
| **DB (옮겼다면)** | ✅ | `pg_dump` 로 떠서 옮기거나, 미러에서 `ingest sync` 로 다시 만듭니다 |

챙길 건 실질적으로 **`.env` 한 줄과 (DB 를 올렸다면) DB** 입니다.

---

## 제일 큰 위험: 새 서버가 상류 API 에 못 닿을 수 있습니다

한국 공공 API 는 해외 클라우드 IP 대역을 흔히 차단합니다. 이 프로젝트가
GitHub Actions 를 못 쓰는 이유가 정확히 그것입니다 — 미국 Azure 러너에서
DNS 는 되는데 TCP 80 이 안 열립니다 ([SCHEDULE.md](SCHEDULE.md) 10장).

지금 서버(말레이시아)는 0.2~0.3초로 잘 붙습니다. **그게 우연히 그 대역이
안 막혀 있어서**지, 해외라서 되는 게 아닙니다. 새 서버가 어디든,
**돈 내기 전에 재세요.**

```bash
scp scripts/vps_preflight.sh 새서버:/tmp/
ssh 새서버 'DATA_GO_KR_KEY=... bash /tmp/vps_preflight.sh'
```

1번 항목이 실패하면 그 호스팅은 못 씁니다. 다른 곳을 알아보는 게 맞고,
전부 막히면 국내 호스팅으로 가야 합니다.

> 시간당 요금제라면 한 시간짜리 인스턴스를 띄워 이것만 돌려 보고 지워도
> 됩니다. 며칠 치 요금보다 쌉니다.

---

## 순서

### 1) 새 서버 준비

```bash
ssh 새서버 'bash -s' < scripts/vps_bootstrap.sh
```

코드 clone → venv → 의존성 → `.env` 틀 → **dry-run** 까지 합니다.
`--dry-run` 은 파일을 안 쓰므로 옛 서버와 겹쳐도 안전합니다.

`.env` 는 옛 서버에서 그대로 가져오는 게 빠릅니다.

```bash
scp 옛서버:/opt/jobs-common-data-injest/.env 새서버:/opt/jobs-common-data-injest/.env
```

### 2) 푸시 권한

미러에 커밋하는 게 이 서버의 유일한 '쓰기' 입니다.

```bash
ssh 새서버 "ssh-keygen -t ed25519 -C 'cmn-vps-new' -f ~/.ssh/id_ed25519 -N ''"
ssh 새서버 'cat ~/.ssh/id_ed25519.pub'
# GitHub > 저장소 > Settings > Deploy keys > Add (Allow write access 체크)
ssh 새서버 'cd /opt/jobs-common-data-injest && git remote set-url origin git@github.com:xg1988/jobs-common-data-injest.git'
```

옛 키는 **컷오버가 끝나고 며칠 뒤에** 지우세요. 롤백하려면 필요합니다.

### 3) DB 도 옮긴다면

DB 를 VPS 에 두기로 했다면 새 서버에서 [VPS_DB.md](VPS_DB.md) 를 따르세요.

```bash
ssh 새서버 'cd /opt/jobs-common-data-injest && bash scripts/vps_db_setup.sh'
ssh 새서버 'cd /opt/jobs-common-data-injest && ./.venv/bin/python -m ingest sync --all'
```

미러에서 다시 만들면 되므로 옛 DB 를 옮길 필요가 없습니다. 아카이브로
넘긴 과거만 파일이 유일한 사본인데, 그건 저장소에 들어 있습니다.

### 4) 컷오버 — 순서가 전부입니다

**두 서버가 같은 날 수집하면 안 됩니다.** 둘 다 `data/` 를 커밋해서
푸시하므로, 겹치면 rebase 가 꼬이고 하루치가 두 번 들어갑니다.

```bash
# ① 옛 서버 cron 을 끕니다 (지우지 말고 주석 처리 -- 되돌리기 쉽게)
ssh 옛서버 'crontab -l > /tmp/cron.bak && crontab -l | sed "s|^10 0|#10 0|" | crontab -'
ssh 옛서버 'crontab -l'

# ② 새 서버에서 한 번 손으로 돌립니다 (진짜로 씁니다)
ssh 새서버 'cd /opt/jobs-common-data-injest && bash scripts/daily.sh; tail -30 logs/daily-*.log'

# ③ 커밋이 올라왔는지 확인
git log --oneline -3     # "collect: YYYY-MM-DD" 가 새로 보여야 합니다

# ④ 새 서버 cron 등록
ssh 새서버 'printf "CRON_TZ=Asia/Seoul\n10 0 * * * /opt/jobs-common-data-injest/scripts/daily.sh\n" | crontab -'
```

`CRON_TZ=Asia/Seoul` 을 빠뜨리지 마세요. 서버 기본 시간대가 UTC 면 수집이
09:10 KST 로 밀립니다. 실제로 한 번 겪은 일입니다
([SCHEDULE.md](SCHEDULE.md) 1장).

### 5) 하루 지켜보기

다음 날 아침에 봅니다.

```bash
curl -s https://raw.githubusercontent.com/xg1988/jobs-common-data-injest/main/meta.json | head -20
```

`last_success` 가 어젯밤이면 끝난 것입니다. 감시견(GitHub Actions)도
01:30·11:00 KST 에 같은 걸 보고, 이상하면 메일을 보냅니다.

### 6) 정리

- 옛 서버는 **최소 일주일** 남겨 두세요 (cron 은 꺼진 채로)
- `logs/` 중 필요한 게 있으면 그때 내려받습니다
- 옛 배포 키를 GitHub 에서 지웁니다
- `docs/SCHEDULE.md` 1장의 서버 이름과 `.github/workflows/watchdog.yml`
  마지막 줄의 `ssh cmn-vps ...` 안내를 새 주소로 고칩니다

---

## 되돌리려면

옛 서버 cron 을 다시 켜고, 새 서버 cron 을 끄면 됩니다.

```bash
ssh 옛서버 'crontab /tmp/cron.bak'
ssh 새서버 'crontab -r'
```

옛 서버는 그동안 놀고 있었을 뿐 상태가 상하지 않습니다. `daily.sh` 가
맨 처음에 `git pull --rebase --autostash` 를 하므로, 며칠 밀린 것도
알아서 따라잡습니다.

---

## 옮기면서 같이 하면 좋은 것

- **시간대를 서버에서도 Asia/Seoul 로** — `timedatectl set-timezone Asia/Seoul`.
  로그 시간이 KST 로 찍혀서 사고 조사가 편해집니다.
- **`logs/` 정리** — `logrotate` 나 `find logs -mtime +30 -delete` 를 cron 에.
  지금은 무한히 쌓입니다.
- **`.env` 권한** — `chmod 600 .env`. 새로 만들 때 자주 빠뜨립니다.
