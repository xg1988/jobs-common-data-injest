# fixtures

여기 있는 파일은 **테스트 입력**입니다. 테스트는 실제 API 를 호출하지 않습니다.

## 전부 실응답입니다 (2026-08-19 캡처)

| 파일 | 어떻게 받았나 | 쓰는 곳 |
|---|---|---|
| `molit_apt_trade_page1.xml` | `LAWD_CD=11680 DEAL_YMD=202606 pageNo=1 numOfRows=2` | 정규화·페이징 |
| `molit_apt_trade_page2.xml` | 위와 같고 `pageNo=2` | 페이징 |
| `molit_apt_trade_canceled.xml` | `11680/202606` 100건 중 해제 2건 + 정상 2건만 남김 (내용은 원문 그대로) | 해제 거래 보존, `_key` 충돌 |
| `molit_apt_trade_empty.xml` | `11680/202712` (아직 거래가 없는 미래 월) | 빈 응답 |
| `molit_apt_trade_authfail.xml` | 활용신청 승인 전 호출 | 인증 실패 |
| `molit_apt_trade_authfail.json` | 위와 같고 `_type=json` | JSON 에러 봉투 |

다시 받으려면:

```
python -m ingest capture --source molit_apt_trade --region 11680 --month 2026-06
```

## 실응답으로 확정된 것 (기획서 8-1 · 8-4 의 `[확인 필요]`)

1. **응답 형식** — XML 이 기본이고, `_type=json` 을 붙이면 **JSON 도 됩니다.**
   에러 응답도 JSON 으로 오는데 봉투 모양이 다릅니다
   (`OpenAPI_ServiceResponse.cmmMsgHeader`).
2. **태그명** — **영문**입니다. 한글 태그(`아파트` 등)는 구 서비스의 것입니다.
   ```
   aptDong aptNm buildYear buyerGbn cdealDay cdealType dealAmount dealDay
   dealMonth dealYear dealingGbn estateAgentSggNm excluUseAr floor jibun
   landLeaseholdGbn rgstDate sggCd slerGbn umdNm
   ```
   기획서가 추정한 `roadNm`(도로명)은 **없습니다.**
3. **`dealAmount`** — `"145,000"` 처럼 콤마가 붙은 문자열. 단위는 만원.
4. **`cdealType` / `cdealDay`** — 해제면 `"O"` / `"26.07.25"` (**YY.MM.DD**).
   해제가 아니면 둘 다 공백 한 칸(`" "`)이 들어옵니다.
5. **`totalCount`** — `<body>` 아래. `numOfRows=2` 로 잡으면 223건이 112페이지로
   쪼개집니다. 페이징은 이 값을 보고 끝까지 돌아야 합니다.
6. **`_key` 충돌이 실제로 납니다** — 같은 거래가 해제분·정상분으로 두 번 옵니다.
   실측 2,813건 중 52건(1.8%). 드문 일이 아닙니다.
