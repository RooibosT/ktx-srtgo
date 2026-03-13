# KTXgo 예약대기 좌석배정 SMS 알림 발견 과정

## 목적

`ktxgo`에 예약대기 후 좌석배정 `SMS` 알림 등록 기능을 붙이기 위해, 코레일톡에서 실제로 어떤 후속 요청을 보내는지 찾아낸 과정을 정리한다.

이 문서는 구현 상세보다도 다음 질문에 답하는 데 초점을 둔다.

- 예약대기 알림 등록이 정말 별도 요청인지
- 어떤 API를 찾아야 하는지
- 그 API의 파라미터를 어떻게 복원했는지

## 한 줄 요약

이번 기능은 공개 문서만으로 구현한 것이 아니다. 아래 3가지를 조합해서 요청 계약을 복원했다.

1. 코레일 공식 안내 문구
2. 저장소 안의 기존 KTX/SRT 예약 흐름
3. 저장소에 포함되어 있던 코레일톡 APK 역분석

## 1. 먼저 사용자 흐름부터 확인

가장 먼저 확인한 것은 "예약대기 신청"과 "좌석 배정 알림 신청"이 같은 단계인지, 아니면 분리된 단계인지였다.

코레일 공식 모바일 안내를 보면:

- 먼저 예약대기를 신청하고
- 이후 `예약내역`에서 `좌석 배정 알림 신청`을 별도로 할 수 있다고 설명한다

이걸 통해 구현 방향을 먼저 확정할 수 있었다.

- 기존 `TicketReservation(txtJobId=1102)` 요청은 그대로 유지
- 예약대기 성공 후 후속 API를 한 번 더 호출

즉, 예약대기 API 자체에 알림 파라미터를 억지로 끼워 넣는 방식이 아니라, "예약대기 성공 -> 알림 등록"의 2단계 흐름으로 보는 것이 맞다고 판단했다.

## 2. 저장소 안 기존 구현으로 구조 가설 세우기

다음으로 이 저장소 안의 기존 코드를 봤다.

### KTXgo 쪽에서 확인한 점

`ktxgo`는 이미:

- `www.korail.com`에서 로그인하고
- 브라우저 세션을 유지한 채
- `_api_call()`로 코레일 모바일 계열 엔드포인트를 POST 호출한다

즉, 새로운 후속 요청만 찾을 수 있으면 같은 구조에 쉽게 붙일 수 있는 상태였다.

관련 파일:

- [ktxgo/korail.py](/home/chan/archive/ktx-srtgo/ktxgo/korail.py)
- [ktxgo/cli.py](/home/chan/archive/ktx-srtgo/ktxgo/cli.py)

### SRTgo 쪽에서 확인한 점

SRT 구현은 이미:

- 예약대기 신청
- 예약대기 옵션 설정(SMS 등)

을 분리된 요청으로 처리하고 있었다.

이것이 KTX와 완전히 동일하다는 뜻은 아니지만, 적어도 "후속 옵션/알림 요청이 별도로 존재할 가능성"을 강하게 뒷받침했다.

관련 파일:

- [srtgo/srt.py](/home/chan/archive/ktx-srtgo/srtgo/srt.py)

## 3. 코레일톡 APK에서 문자열 단서 찾기

저장소 안에는 이미 코레일톡 APK와 추출된 DEX 파일이 있었다.

- [com.korail.talk.apk](/home/chan/archive/ktx-srtgo/tmp/korailtalk_643/com.korail.talk.apk)
- [classes.dex](/home/chan/archive/ktx-srtgo/tmp/korailtalk_643/classes.dex)
- [classes2.dex](/home/chan/archive/ktx-srtgo/tmp/korailtalk_643/classes2.dex)
- [classes3.dex](/home/chan/archive/ktx-srtgo/tmp/korailtalk_643/classes3.dex)

그래서 첫 단계는 DEX에 대해 `strings` 검색을 하는 것이었다.

검색 키워드는 대략 아래와 같았다.

- `예약대기`
- `알림`
- `sms`
- `kakao`
- `pnr`
- `telNo`
- `CpNo`

이 단계에서 바로 중요한 단서가 나왔다.

- `/classes/com.korail.mobile.reservationWait.ReservationWait`
- `ReservationWaitService`
- `RsvWaitDao$RsvWaitRequest`
- `txtPnrNo`
- `txtCpNo`
- `txtSmsSndFlg`
- `txtPsrmClChgFlg`
- `rsv_receive_sms_when_seat_assigned`

여기서 거의 감이 왔다.

- 예약번호가 들어간다
- 전화번호가 들어간다
- SMS 전송 여부 플래그가 있다
- 좌석등급 변경 관련으로 보이는 추가 플래그가 하나 있다

즉, 후속 요청의 대략적인 모양은 이미 이 시점에 보였다.

## 4. 문자열 수준이 아니라 APK 구조로 검증

`strings`만으로는 부족했다. 이유는 다음과 같다.

- 어떤 필드가 같은 요청 모델에 속하는지 확정할 수 없음
- 어떤 엔드포인트가 그 요청 모델을 실제로 쓰는지 확정할 수 없음
- 서비스 호출 시 인자 순서를 알 수 없음

그래서 `androguard`를 설치해 APK를 구조적으로 파싱했다.

## 5. 실제 요청 모델 복원

APK 안에서 다음 클래스를 찾았다.

- `Lcom/korail/talk/network/dao/reservationWait/RsvWaitDao$RsvWaitRequest;`

이 요청 모델의 필드는 아래 4개였다.

- `txtPnrNo`
- `txtCpNo`
- `txtSmsSndFlg`
- `txtPsrmClChgFlg`

즉, 예약대기 좌석배정 알림 등록 요청이 최소한 이 4개 값을 가진다는 점을 구조적으로 확인했다.

## 6. 실제 서비스 호출 형태 확인

다음으로 찾은 클래스는 아래였다.

- `Lcom/korail/talk/network/dao/reservationWait/ReservationWaitService;`

이 서비스에는 아래 메서드가 있었다.

- `rsvWait(String, String, String, String, String, String, String)`

인자가 7개인 이유는, 요청 모델의 4개 필드 외에 공통 모바일 요청 파라미터 3개가 붙기 때문이라고 볼 수 있었다.

## 7. DAO 실행 코드에서 인자 순서 복원

정확한 요청 순서를 알기 위해 `RsvWaitDao.executeDao()`를 확인했다.

여기서 실제 호출 순서는 아래와 같이 나왔다.

1. `Device`
2. `Version`
3. `Key`
4. `txtPnrNo`
5. `txtPsrmClChgFlg`
6. `txtSmsSndFlg`
7. `txtCpNo`

그리고 이 호출이 연결되는 경로 문자열은 앞서 찾은 것과 일치했다.

- `/classes/com.korail.mobile.reservationWait.ReservationWait`

이 단계에서 사실상 `ktxgo`에 넣을 수 있는 요청 계약이 완성됐다.

## 8. KTXgo에 반영한 실제 요청 형태

위에서 복원한 계약을 `ktxgo`에서는 아래 형태로 사용했다.

```python
{
    "Device": "AD",
    "Version": "250601002",
    "Key": "korail1234567890",
    "txtPnrNo": pnr_no,
    "txtPsrmClChgFlg": "N",
    "txtSmsSndFlg": "Y",
    "txtCpNo": phone,
}
```

적용 위치:

- [ktxgo/config.py](/home/chan/archive/ktx-srtgo/ktxgo/config.py)
- [ktxgo/korail.py](/home/chan/archive/ktx-srtgo/ktxgo/korail.py)
- [ktxgo/cli.py](/home/chan/archive/ktx-srtgo/ktxgo/cli.py)

현재 흐름은 다음과 같다.

1. 기존 방식대로 예약대기 신청
2. 응답에서 `h_pnr_no` 확보
3. `--waitlist-alert-phone` 또는 keyring의 `KTX waitlist_alert_phone`에서 전화번호 확보
4. `ReservationWait` 후속 호출 수행
5. 알림 등록 실패는 예약대기 자체를 깨지 않도록 비치명적으로 처리

## 9. 왜 지금은 SMS만 구현했는가

APK 안에는 카카오 관련 문자열이 많이 있었지만, 이번에 복원한 예약대기 알림 요청 모델에서는 아래 필드만 명확히 확인됐다.

- `txtSmsSndFlg`

반대로 같은 요청 경로에서 카카오 전용 플래그는 확인하지 못했다.

그래서 현재 구현은 `SMS` 알림 등록만 지원한다.

카카오톡 알림은:

- 같은 요청의 다른 숨은 파라미터일 수도 있고
- 앱 전용 다른 흐름일 수도 있고
- 별도 링크/연동 상태를 전제로 할 수도 있다

이번 작업 범위에서는 거기까지 확정하지 못했다.

## 10. 나중에 다시 찾을 때의 재현 절차

같은 흐름을 다시 찾아야 하면 가장 짧은 경로는 아래다.

1. 공식 안내에서 사용자 흐름을 먼저 확인
2. APK DEX에 대해 `strings` 검색
3. 후보 DAO / Request / Service 클래스명 확보
4. `androguard`로 APK 파싱
5. 아래를 복원
   - 요청 모델 필드
   - 서비스 메서드
   - 엔드포인트 문자열
   - DAO 내부 인자 순서
6. 그 계약을 `ktxgo`의 `_api_call()`에 반영

## 주의사항

이번 작업은:

- 코레일톡 APK 기준으로 요청 계약을 복원했고
- 로컬 테스트로 동작을 검증했지만
- 실제 코레일 계정으로 라이브 예약대기 후 SMS 등록까지 end-to-end smoke test를 수행한 상태는 아니다

즉, "요청 계약을 충분히 복원해서 코드로 붙였다"는 단계까지는 끝났고, 실제 운영 환경 검증은 별도로 한 번 더 필요하다.
