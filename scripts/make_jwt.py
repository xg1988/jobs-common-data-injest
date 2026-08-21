"""PostgREST 용 JWT 를 만듭니다. 표준 라이브러리만 씁니다.

PostgREST 는 요청의 JWT 안에 있는 `role` 클레임을 보고 그 역할로 갈아탑니다.
수집기가 쓰기를 하려면 `service_role` 로 서명한 토큰이 필요합니다.

    python scripts/make_jwt.py --secret "$JWT_SECRET" --role service_role

만료를 안 넣는 이유
    이 토큰은 서버 안(.env)에만 있고, 밖으로 나가지 않습니다. 만료를 넣으면
    어느 날 새벽 수집이 조용히 401 로 죽습니다 -- 그때 로그를 보기 전에는
    이유를 모릅니다. 돌려야 할 일이 생기면 JWT_SECRET 을 바꾸면 그 순간
    기존 토큰이 전부 무효가 됩니다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json


def b64(raw: bytes) -> str:
    """JWT 는 패딩 없는 base64url 을 씁니다."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make(secret: str, role: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"role": role}
    body = ".".join(
        b64(json.dumps(p, separators=(",", ":"), sort_keys=True).encode())
        for p in (header, payload)
    )
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{b64(sig)}"


def main() -> None:
    ap = argparse.ArgumentParser(description="PostgREST 용 JWT 생성")
    ap.add_argument("--secret", required=True, help="PGRST_JWT_SECRET (32자 이상)")
    ap.add_argument("--role", default="service_role", help="역할 (기본: service_role)")
    args = ap.parse_args()

    if len(args.secret) < 32:
        raise SystemExit(
            f"JWT_SECRET 이 {len(args.secret)}자입니다. PostgREST 는 32자 이상을 요구합니다."
        )
    print(make(args.secret, args.role))


if __name__ == "__main__":
    main()
