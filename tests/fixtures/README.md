# fixtures

여기 있는 파일은 **테스트 입력**입니다. 테스트는 실제 API 를 호출하지 않습니다.

## 지금 상태

| 파일 | 출처 | 상태 |
|---|---|---|
| `molit_apt_trade_page1.xml` | 손으로 만든 것 | ⚠️ 합성 |
| `molit_apt_trade_page2.xml` | 손으로 만든 것 | ⚠️ 합성 |
| `molit_apt_trade_empty.xml` | 손으로 만든 것 | ⚠️ 합성 |
| `molit_apt_trade_authfail.xml` | 손으로 만든 것 | ⚠️ 합성 |

**⚠️ 합성** = 공공데이터포털 문서를 근거로 추정해 만든 것이지, 실응답이 아닙니다.
필드명·타입·페이징 구조는 아직 확정되지 않았습니다 (기획서 8-1, 8-4의 `[확인 필요]`).

## 실응답으로 교체하는 법 (기획서 16장 2단계)

```
python -m ingest capture --source molit_apt_trade --region 11680 --month 2026-06 --rows 10
```

이 명령은 XML/JSON 두 형식을 각각 한 번씩 호출해 원문 그대로 이 디렉터리에 저장합니다.
저장된 파일을 보고 아래를 확정한 뒤, 위 표의 "합성" 표시를 지우세요.

1. 응답 형식 — JSON(`_type=json`)이 되는가, XML 뿐인가 (열린 질문 1)
2. 태그명 — `aptNm` 계열(영문)인가 `아파트` 계열(한글)인가
   → `ingest/sources/molit_apt_trade.py` 의 `FIELD_ALIASES` 를 실제 것만 남기고 정리
3. `dealAmount` 의 실제 표기 (앞 공백 / 콤마)
4. `cdealType` / `cdealDay` 의 실제 표기
5. `totalCount` 위치와 페이징 동작
