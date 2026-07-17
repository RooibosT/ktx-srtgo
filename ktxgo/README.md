# KTXgo

Chromium extension 기반 코레일 열차 예매 자동화 CLI.

코레일 웹사이트의 DynaPath 매크로 차단 환경에서 열차 검색 → 좌석 예매/예약대기 → 자동결제 → 텔레그램 알림까지 자동화합니다.

> **주의**: 본 프로그램의 모든 상업적·영리적 이용을 엄격히 금지합니다. 사용에 따른 모든 책임은 사용자에게 있으며, 개발자는 어떠한 책임도 부담하지 않습니다.

## 동작 원리

1. 기본 백엔드는 **Chromium extension**입니다.
2. 일반 Chromium 프로필(`~/.ktxgo/chromium-extension-profile`)로 `www.korail.com`에 접속합니다. 기본 실행은 `--headless`라서 재사용 가능한 로그인 세션이 있으면 창이 보이지 않습니다.
3. unpacked extension이 Korail 페이지에 `page.js`를 주입하고, Python CLI와 로컬 제어 서버(`127.0.0.1`)로 통신합니다.
4. 조회/예약/결제 요청은 페이지 컨텍스트의 `XMLHttpRequest`로 실행합니다. 이때 Korail DynaPath가 요청을 정상 브라우저 요청으로 감싸므로 `MACRO ERROR`를 피할 수 있습니다.
5. 첫 실행 또는 세션 만료 시에는 보이는 Chromium 창에서 사용자가 직접 로그인하고 Enter를 누릅니다. 코레일 로그인 상태가 새 headless 프로세스로 재사용되지 않는 경우가 있어, 이때는 창을 자동 최소화하고 같은 로그인 브라우저에서 예약 루프를 계속합니다.

기존 Playwright Firefox 방식은 `--api-backend playwright`로 남겨 두었지만, 현재 기본값은 extension 백엔드입니다.

## 요구사항

- Python 3.10+
- Playwright `1.42.0`
- Playwright Firefox + Chromium 브라우저

권장 설치는 저장소 루트의 설치 스크립트를 사용합니다.

```bash
./install.sh --uv
# 또는
./install.sh --conda --env-name srtgo-env
```

`install.sh`는 확인된 Chromium 버전이 포함된 Playwright `1.42.0`을 설치하고, `firefox`와 `chromium` 브라우저를 함께 내려받습니다.

수동 설치가 필요하면 다음과 같이 맞춥니다.

```bash
pip install -e . 'playwright==1.42.0'
python -m playwright install firefox chromium
```

## 초기 설정

### 코레일 로그인

기본 extension 백엔드는 재사용 가능한 로그인 세션이 있으면 창을 띄우지 않습니다. 첫 실행 또는 세션 만료 시에는 Chromium 창을 열어 로그인을 받습니다. 창에서 코레일 로그인을 직접 완료한 뒤 터미널에서 Enter를 누르세요. 로그인 직후 headless 재사용이 확인되지 않으면 창을 자동 최소화하고, 닫지 않은 상태에서 같은 로그인 브라우저로 조회/예약 루프를 계속합니다.

```bash
python3 -m ktxgo
```

로그인 프로필은 기본적으로 아래에 저장됩니다.

```text
~/.ktxgo/chromium-extension-profile
```

다른 Chromium 실행 파일이나 프로필을 쓰고 싶으면 다음 옵션을 사용합니다.

```bash
python3 -m ktxgo \
  --extension-chromium ~/.cache/ms-playwright/chromium-1105/chrome-linux/chrome \
  --extension-profile ~/.ktxgo/my-ktx-profile
```

### 코레일 계정 (선택, legacy Playwright 백엔드용)

`--api-backend playwright`를 사용할 때 자동/수동 로그인 보조에 사용됩니다.

```bash
keyring set KTX id        # 회원번호
keyring set KTX pass      # 비밀번호
```

### 카드 정보 (자동결제 사용 시)

```bash
# TTY 등록 (권장)
python3 -m ktxgo --set-card

# 직접 keyring 등록
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

### 예약대기 좌석배정 SMS 알림 (선택)

```bash
# interactive 메뉴 등록/수정
python3 -m ktxgo

# 직접 keyring 등록
keyring set KTX waitlist_alert_phone   # 좌석배정 SMS를 받을 전화번호
```

interactive 메뉴의 `예약대기 SMS 알림 번호 등록/수정`에서도 같은 값을 저장할 수 있습니다.

현재 확인된 코레일톡 요청 계약은 `SMS` 알림만 노출합니다. 카카오톡 채널은 아직 확인되지 않았습니다.

## 사용법

```bash
# 기본 실행 (extension 백엔드, 메뉴 표시)
python3 -m ktxgo

# 옵션 지정 후 대화형 실행
python3 -m ktxgo \
  --departure 서울 \
  --arrival 부산 \
  --date 20260305 \
  --time 06 \
  --train-type itx-saemaeul \
  --train-type mugunghwa \
  --seat general \
  --waitlist-alert-phone 01012341234 \
  --auto-pay \
  --telegram

# 시도 횟수 제한
python3 -m ktxgo --max-attempts 100

# 비대화형 실행 (기본은 KTX만)
python3 -m ktxgo --no-interactive

# 일반열차 포함 비대화형 실행
python3 -m ktxgo \
  --no-interactive \
  --train-type itx-saemaeul \
  --train-type mugunghwa \
  --train-type itx-cheongchun

# legacy Playwright 경로로 디버깅
python3 -m ktxgo --api-backend playwright --no-headless

# extension Chromium 창을 계속 보이게 실행하고 싶을 때
python3 -m ktxgo --no-headless
```

### CLI 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--departure` | 서울 | 출발역 |
| `--arrival` | 부산 | 도착역 |
| `--date` | 현재+10분 기준 | 출발일 (YYYYMMDD) |
| `--time` | 현재+10분 기준 | 출발 시간대 (HH) |
| `--interactive` / `--no-interactive` | TTY에서 interactive | 날짜/시간/열차 선택 프롬프트 사용 여부. interactive에서는 `KTX만` / `KTX + ITX/무궁화 등` 프리셋 제공 |
| `--api-backend` | extension | `extension`(기본 Chromium extension) / `playwright`(legacy Playwright) |
| `--extension-chromium` | 자동 탐색 | extension 백엔드에서 사용할 Chromium 실행 파일. 기본적으로 Playwright `chromium-1105`를 우선 사용 |
| `--extension-profile` | `~/.ktxgo/chromium-extension-profile` | extension 백엔드 로그인 세션 저장 프로필 |
| `--train-type` | `ktx` | 반복 지정 가능. `ktx`, `itx-saemaeul`, `mugunghwa`, `tonggeun`, `itx-cheongchun`, `itx-maeum`, `airport`, `legacy-all` (`saemaeul`, `nuriro` alias 지원) |
| `--seat` | any | `general` / `special` / `any` / `standing` |
| `--headless` / `--no-headless` | headless | extension 백엔드는 재사용 가능한 세션이 있으면 창을 숨겨 실행합니다. 로그인이 필요하면 보이는 Chromium 창을 열고, 로그인 후 자동 최소화합니다. `--no-headless`는 예매 루프 동안 Chromium 창을 계속 보입니다 |
| `--set-card` | off | TTY에서 카드 정보 등록 후 종료 |
| `--max-attempts` | 0 (무한) | 최대 검색 시도 횟수 |
| `--auto-pay` | off | 예약 성공 후 자동 카드결제 |
| `--smart-ticket` / `--no-smart-ticket` | smart-ticket | 자동결제 시 스마트티켓 발권 여부 |
| `--telegram` | off | 예약/결제 결과 텔레그램 알림 |
| `--waitlist-alert-phone` | off | 예약대기 성공 시 좌석배정 SMS 알림을 등록할 전화번호. 미지정 시 `keyring`의 `KTX waitlist_alert_phone` 사용 |

### 지원 역 목록

서울, 용산, 광명, 수서, 영등포, 수원, 평택, 천안아산, 천안, 오송, 조치원, 대전, 서대전, 김천구미, 구미, 동대구, 대구, 경주, 울산(통도사), 포항, 경산, 밀양, 부산, 구포, 창원중앙, 평창, 진부(오대산), 강릉, 익산, 전주, 광주송정, 목포, 순천, 청량리, 정동진

## 실행 흐름

```text
시작
 ├─ headless extension Chromium 실행
 │   ├─ 저장된 Chromium 프로필 로그인 확인 성공 → 숨겨진 상태로 진행
 │   └─ 실패/세션 만료 → 보이는 Chromium 로그인 창 실행
 │       └─ 사용자가 Chromium 창에서 로그인 완료 후 Enter → 로그인 확인 → 창 자동 최소화 후 같은 브라우저에서 진행
 │
 ├─ (interactive 모드) 시작 메뉴
 │   ├─ 로그인 설정
 │   ├─ 역 설정
 │   ├─ 예약대기 SMS 알림 번호 등록/수정
 │   ├─ 카드 등록/수정
 │   └─ 예매 시작
 │      ├─ 출발/도착/날짜/시간/조회 열차 범위(`KTX만` / `KTX + ITX/무궁화 등`) 입력
 │      ├─ 초기 조회 결과에서 예약 시도할 열차 선택 (다중선택 가능)
 │      └─ 좌석 선호 + 자동결제 여부 + 스마트티켓 발권 여부 확인
 │
 ├─ 반복 조회/예약 루프 시작
 │   ├─ ScheduleView API로 열차 검색
 │   ├─ 선택한 열차(비대화형은 전체) 중
 │   │   ├─ 좌석 있으면 TicketReservation API(`txtJobId=1101`)로 예매
 │   │   └─ 좌석 매진 + 예약대기 가능이면 TicketReservation API(`txtJobId=1102`)로 예약대기 신청
 │   │       ├─ 성공
 │   │       │   ├─ `ReservationWait` API로 좌석배정 SMS 알림 전화번호 등록
 │   │       │   ├─ 좌석 예매 + --auto-pay → ReservationPayment API로 결제
 │   │       │   ├─ --telegram → 텔레그램 알림 전송
 │   │       │   └─ 종료
 │   │       └─ 실패 → 다음 열차 시도
 │   └─ 좌석/예약대기 모두 불가 → 1.2초 대기 후 재검색
 │
 └─ 세션 만료 감지 시 보이는 Chromium 로그인 창으로 재로그인 후 자동 최소화
```

## 프로젝트 구조

```text
ktxgo/
├── __init__.py          # 패키지 초기화, 버전
├── __main__.py          # python -m ktxgo 진입점
├── config.py            # API 엔드포인트, 역 목록, 상수
├── browser.py           # legacy Playwright 브라우저 관리
├── extension_backend.py # Chromium extension 제어 서버/runner/API adapter
├── korail.py            # KorailAPI (검색, 예약, 결제, 로그인 확인)
└── cli.py               # Click CLI, 반복 예약 루프, 자동결제, 텔레그램
```

## 데이터 저장 경로

| 파일/디렉터리 | 경로 | 설명 |
|------|------|------|
| Chromium 프로필 | `~/.ktxgo/chromium-extension-profile` | extension 백엔드 로그인 세션 |
| Chromium extension | `~/.ktxgo/chromium-extension` | 실행 시 생성되는 unpacked extension |
| 쿠키 | `~/.ktxgo/cookies.json` | legacy Playwright/Firefox 세션 쿠키 |
| 카드/계정/알림전화 | OS keyring | `keyring` 라이브러리 사용 (`KTX id/pass`, 카드 정보, `KTX waitlist_alert_phone`) |

## 기술적 세부사항

### DynaPath 우회

코레일은 DynaPath SDK를 사용하여 매크로/봇을 차단합니다.

- 단순 `requests`/쿠키 재사용, Playwright `fetch()`, WebDriver 계열 호출은 `MACRO ERROR`가 발생할 수 있습니다.
- 현재 동작하는 경로는 **Chromium 프로세스(headless 가능) + unpacked extension + 페이지 컨텍스트 `XMLHttpRequest`**입니다.
- `ScheduleView`, `TicketReservation`, `ReservationWait`, `ReservationPayment` 요청은 기존 `KorailAPI` 파서/예약 로직을 재사용하되, 실제 전송만 extension runner가 담당합니다.

### Chromium 버전

확인된 기본 경로는 Playwright `1.42.0`이 설치하는 Chromium `1105`입니다.

```text
~/.cache/ms-playwright/chromium-1105/chrome-linux/chrome
```

`ktxgo`는 위 경로를 먼저 찾고, 없으면 다른 Playwright Chromium 캐시와 시스템 `chromium`/`chromium-browser`를 순서대로 찾습니다. 문제가 있으면 `--extension-chromium`으로 직접 지정하세요.

## 감사의 말

- [SRT](https://github.com/ryanking13/SRT) by ryanking13 (MIT License)
- [korail2](https://github.com/carpedm20/korail2) by carpedm20 (BSD License)
- [srtgo](https://github.com/lapis42/srtgo) by lapis42 — 원본 프로젝트
