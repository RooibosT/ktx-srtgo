"""Configuration constants for KTXgo."""

from pathlib import Path
from typing import Sequence

# Korail Web Base
BASE_URL = "https://www.korail.com"
LOGIN_URL = f"{BASE_URL}/ticket/login"
SEARCH_URL = f"{BASE_URL}/ticket/search/general"

# Persistent state directory
DATA_DIR = Path.home() / ".ktxgo"
COOKIE_PATH = DATA_DIR / "cookies.json"
STORAGE_STATE_PATH = DATA_DIR / "storage_state.json"

# API Endpoints (relative, called via fetch from browser context)
API_SCHEDULE = "/classes/com.korail.mobile.seatMovie.ScheduleView"
API_LOGIN = "/ebizweb/common/loginProcess"
API_LOGIN_CHECK = "/ebizweb/common/loginCheck"
API_RESERVE = "/classes/com.korail.mobile.certification.TicketReservation"
API_RESERVATION_LIST = "/classes/com.korail.mobile.certification.ReservationList"
API_RESERVATION_VIEW = "/classes/com.korail.mobile.reservation.ReservationView"
API_CANCEL = "/classes/com.korail.mobile.reservationCancel.ReservationCancelChk"
API_MYTICKET = "/classes/com.korail.mobile.myTicket.MyTicketList"
API_PAY = "/classes/com.korail.mobile.payment.ReservationPayment"

# Mobile API defaults observed in KorailTalk
MOBILE_DEVICE = "AD"
MOBILE_VERSION = "250601002"
MOBILE_KEY = "korail1234567890"

# Stealth
STEALTH_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

# Timeouts
NAV_TIMEOUT = 30_000
POLL_INTERVAL_S = 1.2

# Reservation codes
RSV_AVAILABLE = "11"
RSV_SOLD_OUT = "13"
RSV_WAITING = "09"

# Train groups
TRAIN_GROUP_KTX = "100"
TRAIN_GROUP_ALL = "00"
TRAIN_TYPE_KTX = "ktx"
TRAIN_TYPE_ITX_SAEMAEUL = "itx-saemaeul"
TRAIN_TYPE_MUGUNGHWA = "mugunghwa"
TRAIN_TYPE_TONGGUEN = "tonggeun"
TRAIN_TYPE_ITX_CHEONGCHUN = "itx-cheongchun"
TRAIN_TYPE_AIRPORT = "airport"
TRAIN_TYPE_LEGACY_ALL = "legacy-all"
TRAIN_TYPE_SAEMAEUL_ALIAS = "saemaeul"
TRAIN_TYPE_NURIRO_ALIAS = "nuriro"
TRAIN_TYPE_ORDER = (
    TRAIN_TYPE_KTX,
    TRAIN_TYPE_ITX_SAEMAEUL,
    TRAIN_TYPE_MUGUNGHWA,
    TRAIN_TYPE_TONGGUEN,
    TRAIN_TYPE_ITX_CHEONGCHUN,
    TRAIN_TYPE_AIRPORT,
)
DEFAULT_TRAIN_TYPES = (TRAIN_TYPE_KTX,)
TRAIN_TYPE_CODE_BY_NAME = {
    TRAIN_TYPE_KTX: "100",
    TRAIN_TYPE_ITX_SAEMAEUL: "101",
    TRAIN_TYPE_MUGUNGHWA: "102",
    TRAIN_TYPE_TONGGUEN: "103",
    TRAIN_TYPE_ITX_CHEONGCHUN: "104",
    TRAIN_TYPE_AIRPORT: "105",
}
TRAIN_TYPE_LABEL_BY_NAME = {
    TRAIN_TYPE_KTX: "KTX",
    TRAIN_TYPE_ITX_SAEMAEUL: "ITX-새마을",
    TRAIN_TYPE_MUGUNGHWA: "무궁화/누리로",
    TRAIN_TYPE_TONGGUEN: "통근",
    TRAIN_TYPE_ITX_CHEONGCHUN: "ITX-청춘",
    TRAIN_TYPE_AIRPORT: "공항",
}
TRAIN_TYPE_ALIAS_TO_NAME = {
    TRAIN_TYPE_SAEMAEUL_ALIAS: TRAIN_TYPE_ITX_SAEMAEUL,
    TRAIN_TYPE_NURIRO_ALIAS: TRAIN_TYPE_MUGUNGHWA,
}
TRAIN_TYPE_OPTION_CHOICES = (
    *TRAIN_TYPE_ORDER,
    TRAIN_TYPE_LEGACY_ALL,
    *TRAIN_TYPE_ALIAS_TO_NAME.keys(),
)


def normalize_train_types(train_types: Sequence[str] | None) -> tuple[str, ...]:
    requested = train_types or DEFAULT_TRAIN_TYPES
    selected: set[str] = set()

    for raw_value in requested:
        value = raw_value.strip().lower()
        if not value:
            continue
        if value == TRAIN_TYPE_LEGACY_ALL:
            selected.update(TRAIN_TYPE_ORDER)
            continue
        value = TRAIN_TYPE_ALIAS_TO_NAME.get(value, value)
        if value not in TRAIN_TYPE_CODE_BY_NAME:
            raise ValueError(f"Unknown train type: {raw_value}")
        selected.add(value)

    if not selected:
        selected.update(DEFAULT_TRAIN_TYPES)
    return tuple(name for name in TRAIN_TYPE_ORDER if name in selected)


def train_type_codes(train_types: Sequence[str] | None) -> tuple[str, ...]:
    codes: list[str] = []
    seen: set[str] = set()
    for name in normalize_train_types(train_types):
        code = TRAIN_TYPE_CODE_BY_NAME[name]
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return tuple(codes)

# Station list (from live recon of Korail popup)
STATIONS = [
    "서울",
    "용산",
    "광명",
    "수서",
    "영등포",
    "수원",
    "평택",
    "천안아산",
    "천안",
    "오송",
    "조치원",
    "대전",
    "서대전",
    "김천구미",
    "구미",
    "동대구",
    "대구",
    "경주",
    "울산(통도사)",
    "포항",
    "경산",
    "밀양",
    "부산",
    "구포",
    "창원중앙",
    "평창",
    "진부(오대산)",
    "강릉",
    "익산",
    "전주",
    "광주송정",
    "목포",
    "순천",
    "청량리",
    "정동진",
]
DEFAULT_DEPARTURE = "서울"
DEFAULT_ARRIVAL = "부산"
DEFAULT_VISIBLE_STATIONS = [
    "서울",
    "용산",
    "광명",
    "수원",
    "대전",
    "동대구",
    "부산",
]
