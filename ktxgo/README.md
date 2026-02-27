# KTXgo

Playwright(Firefox) 기반 KTX 예매 자동화 CLI.

코레일 웹사이트의 DynaPath 매크로 차단을 우회하여 열차 검색 → 좌석 예매 → 자동결제 → 텔레그램 알림까지 자동화합니다.

> **주의**: 본 프로그램의 모든 상업적·영리적 이용을 엄격히 금지합니다. 사용에 따른 모든 책임은 사용자에게 있으며, 개발자는 어떠한 책임도 부담하지 않습니다.

## 동작 원리

1. **Playwright Firefox**로 korail.com에 접속하여 DynaPath JS를 정상 로드
2. 브라우저 컨텍스트 내에서 `fetch()` API를 직접 호출하여 열차 검색·예약·결제 수행
3. DynaPath는 Chrome CDP를 차단하지만 **Firefox에서는 감지하지 않음**
4. 로그인은 DynaPath의 최엄격 보호 대상이므로 **수동 로그인 + 쿠키 저장** 방식 사용

## 요구사항

- Python 3.10+
- [Playwright](https://playwright.dev/python/) (Firefox)

```bash
pip install playwright click keyring python-telegram-bot
playwright install firefox
```

## 초기 설정

### 코레일 계정 (keyring)

```bash
keyring set KTX id        # 회원번호
keyring set KTX pass      # 비밀번호 (현재 수동 로그인이므로 미사용, 향후 대비)
```

### 카드 정보 (자동결제 사용 시)

```bash
keyring set KTX card_number     # 카드번호 (하이픈 없이)
keyring set KTX card_password   # 카드 비밀번호 앞 2자리
keyring set KTX birthday        # 생년월일 YYMMDD (개인) / 사업자등록번호 10자리
keyring set KTX card_expire     # 유효기간 YYMM
```

### 텔레그램 알림 (선택)

```bash
keyring set telegram token      # 봇 토큰
keyring set telegram chat_id    # 채팅 ID
```

## 사용법

```bash
# 기본 실행 (대화형: 화살표로 출발역/도착역/날짜/시간/좌석선호 선택 + 열차 선택)
python3 -m ktxgo

# 옵션 지정 후 대화형 실행
python3 -m ktxgo \
  --departure 서울 \
  --arrival 부산 \
  --date 20260305 \
  --time 06 \
  --seat general \
  --auto-pay \
  --telegram

# 브라우저 창 표시 (디버깅용)
python3 -m ktxgo --no-headless

# 시도 횟수 제한
python3 -m ktxgo --max-attempts 100

# 기존 방식(비대화형, 조회된 열차 전체 대상)
python3 -m ktxgo --no-interactive
```

### CLI 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--departure` | 서울 | 출발역 |
| `--arrival` | 부산 | 도착역 |
| `--date` | 현재+10분 기준 | 출발일 (YYYYMMDD) |
| `--time` | 현재+10분 기준 | 출발 시간대 (HH) |
| `--interactive` / `--no-interactive` | TTY에서 interactive | 날짜/시간/열차 선택 프롬프트 사용 여부 |
| `--seat` | any | `general` / `special` / `any` / `standing` |
| `--headless` / `--no-headless` | headless | 브라우저 표시 여부 |
| `--max-attempts` | 0 (무한) | 최대 검색 시도 횟수 |
| `--auto-pay` | off | 예약 성공 후 자동 카드결제 |
| `--telegram` | off | 예약/결제 결과 텔레그램 알림 |

### 지원 역 목록

서울, 용산, 광명, 수서, 영등포, 수원, 평택, 천안아산, 천안, 오송, 조치원, 대전, 서대전, 김천구미, 구미, 동대구, 대구, 경주, 울산(통도사), 포항, 경산, 밀양, 부산, 구포, 창원중앙, 평창, 진부(오대산), 강릉, 익산, 전주, 광주송정, 목포, 순천, 청량리, 정동진

## 실행 흐름

```
시작
 ├─ 저장된 쿠키로 로그인 확인
 │   ├─ 성공 → headless 모드로 진행
 │   └─ 실패 → 브라우저 창 열어 수동 로그인 대기 (5분)
 │            └─ 로그인 성공 → 쿠키 저장 → headless 전환
 │
 ├─ (interactive 모드) 출발/도착/날짜/시간/좌석 입력
 │   └─ 초기 조회 결과에서 예약 시도할 열차 선택 (다중선택 가능)
 │
 ├─ 매크로 루프 시작
 │   ├─ ScheduleView API로 열차 검색
 │   ├─ 선택한 열차(비대화형은 전체) 중 좌석 있는 열차 발견
 │   │   └─ TicketReservation API로 예약
 │   │       ├─ 성공
 │   │       │   ├─ --auto-pay → ReservationPayment API로 결제
 │   │       │   ├─ --telegram → 텔레그램 알림 전송
 │   │       │   └─ 종료
 │   │       └─ 실패 → 다음 열차 시도
 │   └─ 좌석 없음 → 1.2초 대기 후 재검색
 │
 └─ 세션 만료 감지 시 자동 재인증
```

## 프로젝트 구조

```
ktxgo/
├── __init__.py     # 패키지 초기화, 버전
├── __main__.py     # python -m ktxgo 진입점
├── config.py       # API 엔드포인트, 역 목록, 상수
├── browser.py      # Playwright Firefox 관리, 쿠키 저장/복원
├── korail.py       # KorailAPI (검색, 예약, 결제, 로그인 확인)
└── cli.py          # Click CLI, 매크로 루프, 자동결제, 텔레그램
```

## 데이터 저장 경로

| 파일 | 경로 | 설명 |
|------|------|------|
| 쿠키 | `~/.ktxgo/cookies.json` | 브라우저 세션 쿠키 |
| 카드/계정 | OS keyring | `keyring` 라이브러리 사용 |

## 기술적 세부사항

### DynaPath 우회

코레일은 DynaPath SDK를 사용하여 매크로/봇을 차단합니다:

- **loginProcess**: 최엄격 보호 → 수동 로그인으로 우회
- **ScheduleView**: `web_s` 레벨 보호 → Firefox fetch()로 우회
- **TicketReservation**: `web_r` 레벨 보호 → Firefox fetch()로 우회
- **ReservationPayment**: dpCnf 목록에 없음 → 직접 fetch() 호출 가능

### Chrome vs Firefox

| | Chrome (CDP) | Firefox (Playwright) |
|---|---|---|
| DynaPath 감지 | O (차단됨) | X (우회됨) |
| `navigator.webdriver` | 감지됨 | 제거 가능 |
| 결과 | 매크로 오류 | 정상 동작 |

## 감사의 말

- [SRT](https://github.com/ryanking13/SRT) by ryanking13 (MIT License)
- [korail2](https://github.com/carpedm20/korail2) by carpedm20 (BSD License)
- [srtgo](https://github.com/lapis42/srtgo) by lapis42 — 원본 프로젝트
