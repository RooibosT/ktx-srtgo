# KTX-SRTgo: K-Train (KTX, SRT) Reservation Assistant
📌 최근 코레일톡 업데이트로 KTX API에서 사용자 토큰 기반 MACRO 차단 이슈를 해결하기 위해 제작되었습니다.

[![Upload Python Package](https://github.com/lapis42/srtgo/actions/workflows/python-publish.yml/badge.svg)](https://github.com/lapis42/srtgo/actions/workflows/python-publish.yml)
[![Downloads](https://static.pepy.tech/badge/srtgo)](https://pepy.tech/project/srtgo)
[![Downloads](https://static.pepy.tech/badge/srtgo/month)](https://pepy.tech/project/srtgo)
[![Python version](https://img.shields.io/pypi/pyversions/srtgo)](https://pypistats.org/packages/srtgo)

> [!NOTE]
> 공정한 예매 문화 조성을 위해 본 프로젝트의 개발 및 지원을 중단하기로 결정했습니다. 양해 부탁드립니다.

> [!WARNING]
> 본 프로그램의 모든 상업적, 영리적 이용을 엄격히 금지합니다. 본 프로그램 사용에 따른 민형사상 책임을 포함한 모든 책임은 사용자에게 있으며, 본 프로그램의 개발자는 민형사상 책임을 포함한 어떠한 책임도 부담하지 않습니다. 본 프로그램을 내려받음으로써 모든 사용자는 위 사항에 이의 없이 동의하는 것으로 간주됩니다.

---
> [!NOTE]
> I have decided to discontinue the development and support for this project. Thank you for your understanding.

> [!WARNING]
> All commercial and profit-making use of this program is strictly prohibited. Use of this program is at your own risk, and the developers of this program shall not be liable for any liability, including civil or criminal liability. By downloading this program, all users are deemed to agree to the above terms without any objection.

## Quick Start

### 1) 설치 (`uv` 또는 `conda`)

```bash
./install.sh
```

첫 실행 시 환경 관리자를 선택합니다.
- `uv`: `.venv` 생성
- `conda`: 기본 `srtgo-env` 생성 (`--env-name`으로 변경 가능)

자주 쓰는 옵션:

```bash
./install.sh --uv
./install.sh --conda --env-name my-train-env
./install.sh --reconfigure
```

### 2) 실행 (`run.sh`)

```bash
./run.sh
```

`run.sh`는 다음을 자동으로 처리합니다.
- `install.sh`에서 선택한 환경(`uv`/`conda`) 활성화
- 화살표 메뉴로 `KTX` / `SRT` 선택 후 실행
- `KTX` 최초 실행 시 웹 로그인 창이 열리며, 여기서 한 번 로그인해야 합니다.

직접 지정 실행:

```bash
./run.sh --ktx
./run.sh --srt
```

### 3) (선택) bash alias 등록

매번 경로를 입력하지 않으려면 `run.sh`를 alias로 등록해 두면 편합니다.

```bash
echo "alias ktxgo='/home/chan/archive/srtgo/run.sh'" >> ~/.bashrc
source ~/.bashrc
```

이후에는 어디서든 `ktxgo`로 실행할 수 있습니다.

## 개별 실행

직접 커맨드로 실행할 수도 있습니다.

```bash
srtgo
python -m ktxgo
```

KTX 카드 등록(자동결제 사용 시):

```bash
python -m ktxgo --set-card
```

## KTXgo 주요 기능

- 수동 로그인 + 쿠키 저장/재사용
- TTY 메뉴
  - 예매 시작
  - 예매 정보 확인 (예약/발권 내역)
  - 로그인 설정
  - 역 설정
  - 카드 등록/수정
- 출발/도착/날짜/시간/인원/열차/좌석선호 기반 예매 루프
- 자동결제(스마트티켓 기본 ON), 텔레그램 알림

세부 옵션/구조 설명은 [ktxgo/README.md](ktxgo/README.md)를 참고하세요.

## Acknowledgments

This project includes code from:
- [SRT](https://github.com/ryanking13/SRT) by ryanking13 (MIT License)
- [korail2](https://github.com/carpedm20/korail2) by carpedm20 (BSD License)
