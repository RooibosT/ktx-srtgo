"""Configuration constants for KTXgo."""

from pathlib import Path

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
