"""Đăng ký khám online do bệnh nhân TỰ điền (thay cho Google Form) — ghi thẳng vào Sheet chính.

Nguyên tắc:
* Máy chủ là nơi kiểm tra cuối cùng (JS ở trình duyệt chỉ để gợi ý cho dễ dùng).
* Dữ liệu từ người lạ ghi vào Sheet bằng USER_ENTERED → mọi ô chữ phải được vô hiệu hoá công thức (=, +, -, @ đứng đầu).
* Cột NGUỒN BỆNH NHÂN / TRẠNG THÁI / KHOA / TUỔI để TRỐNG, giống hệt dòng do Google Form tạo ra, để luồng đối chiếu
  ("đăng ký qua form" = nguồn trống) vẫn hoạt động như cũ.
"""
import hmac
import re
import secrets
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

# ── Danh mục (giữ NGUYÊN chuỗi của Google Form để thống kê trên Sheet không bị lệch) ─────────────────────────────
# (giá trị ghi vào Sheet, nhãn hiển thị cho người lớn tuổi dễ đọc, nhãn bác sĩ phù hợp)
SPECIALTIES = [
    ("Khám Sản - Phụ Khoa",                          "Khám Sản - Phụ khoa",                              "san"),
    ("Khám Nội tổng hợp",                            "Khám Nội tổng hợp",                                ""),
    ("Khám Y học cổ truyền",                         "Khám Y học cổ truyền",                             ""),
    ("Khám Ngoại",                                   "Khám Ngoại",                                       "ngoai"),
    ("Khám Nhi",                                     "Khám Nhi (trẻ em)",                                "nhi"),
    ("Khám Nội Tiết (Huyết áp, tiểu đương, COPD)",   "Khám Nội tiết (huyết áp, tiểu đường, COPD)",       "noitiet"),
    ("khám Mắt",                                     "Khám Mắt",                                         "mat"),
]
OTHER = "__khac__"

# (giá trị ghi vào Sheet, tên hiển thị, chi tiết hiển thị, nhãn chuyên khoa)
DOCTORS = [
    ("PGĐ - BS Lê Tiến Dũng - Chuyên Khoa Nội Tiết - Pk số 2", "PGĐ - BS Lê Tiến Dũng",  "Nội tiết · Phòng khám số 2", "noitiet"),
    ("BS Lê Thị Chuyên - Chuyên Khoa Nội Tiết - Pk số 5",      "BS Lê Thị Chuyên",      "Nội tiết · Phòng khám số 5", "noitiet"),
    ("BS Nguyễn Thị Lý - Chuyên Khoa Nội Tiết - Pk số 3",      "BS Nguyễn Thị Lý",      "Nội tiết · Phòng khám số 3", "noitiet"),
    ("BS Nguyễn Thị Linh - Chuyên Khoa Mắt - Pk số 1",         "BS Nguyễn Thị Linh",    "Mắt · Phòng khám số 1",      "mat"),
    ("BS Bùi Văn Hùng - Chuyên Khoa Ngoại - Pk số 6",          "BS Bùi Văn Hùng",       "Ngoại · Phòng khám số 6",    "ngoai"),
    ("BS Lê Đức Linh - Chuyên Khoa Sản - Pk sản",              "BS Lê Đức Linh",        "Sản · Phòng khám sản",       "san"),
    ("BS Nguyễn Doãn Khoa - Chuyên Khoa Nhi - Pk nhi",         "BS Nguyễn Doãn Khoa",   "Nhi · Phòng khám nhi",       "nhi"),
]

GENDERS = ["NAM", "NỮ", "KHÁC"]


def _slots(start_h, start_m, end_h, end_m, step=30):
    t, end, out = start_h * 60 + start_m, end_h * 60 + end_m, []
    while t <= end:
        out.append(f"{t // 60:02d}:{t % 60:02d}")
        t += step
    return out


# Khung giờ nhận đăng ký (sửa ở đây nếu bệnh viện đổi giờ làm việc)
SLOTS_MORNING = _slots(7, 0, 11, 30)
SLOTS_AFTERNOON = _slots(13, 30, 16, 30)
ALL_SLOTS = SLOTS_MORNING + SLOTS_AFTERNOON
MAX_DAYS_AHEAD = 30

# Chống spam / bấm đúp
MIN_FILL_SECONDS = 8          # điền nhanh hơn thế → chắc chắn là máy
MAX_AGE_SECONDS = 6 * 3600    # mở form quá 6 giờ mới gửi → bắt tải lại
IP_LIMIT, IP_WINDOW = 15, 600  # mỗi IP tối đa 15 đơn HỢP LỆ / 10 phút (bệnh viện có thể dùng chung 1 wifi)
GLOBAL_LIMIT, GLOBAL_WINDOW = 120, 600
DUP_WINDOW = 600              # cùng (tên, SĐT, ngày khám) trong 10 phút = bấm đúp

_lock = threading.Lock()
_hits: dict[str, deque] = {}
_global_hits: deque = deque()
_recent: dict[tuple, float] = {}


# ── Làm sạch ───────────────────────────────────────────────────────────────────────────────────────────────────
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028\u2029\ufeff]")


def clean_text(v, limit=500) -> str:
    """Bỏ ký tự điều khiển/ký tự vô hình, gộp khoảng trắng, cắt độ dài."""
    t = _CTRL.sub("", str(v if v is not None else ""))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def sheet_safe(v: str) -> str:
    """Vô hiệu công thức: Sheets (USER_ENTERED) coi chuỗi bắt đầu bằng = + - @ là công thức → thêm dấu ' để ép thành chữ."""
    return "'" + v if v and v[0] in "=+-@\t\r" else v


def as_text_cell(v: str) -> str:
    """Số điện thoại / CCCD: luôn ép thành chữ để Sheet không bỏ số 0 đầu hay đổi sang số khoa học."""
    return "'" + v if v else v


def normalize_phone(raw) -> str:
    d = re.sub(r"[\s.\-()]", "", str(raw or ""))
    if d.startswith("+84"):
        d = "0" + d[3:]
    elif d.startswith("84") and len(d) == 11:
        d = "0" + d[2:]
    return d


def vn_now() -> datetime:
    from services.sheets import vn_now as _vn
    return _vn()


# ── Kiểm tra dữ liệu ───────────────────────────────────────────────────────────────────────────────────────────
FIELD_STEP = {"full_name": 1, "birth_year": 1, "gender": 1,
              "address": 2, "phone": 2, "cccd": 2,
              "symptoms": 3, "specialty": 3, "specialty_other": 3, "doctor": 3,
              "exam_date": 4, "exam_time": 4,
              "agree_truth": 5, "agree_terms": 5}


def validate(form, now: datetime | None = None):
    """form: dict-like (request.form). Trả về (clean, errors). errors rỗng = hợp lệ."""
    now = now or vn_now()
    today = now.date()
    g = lambda k, n=500: clean_text(form.get(k, ""), n)
    errors, c = {}, {}

    # 1) Họ tên — viết HOA như các dòng khác trên Sheet; cần ít nhất 2 chữ
    name = g("full_name", 80).upper()
    words = name.split(" ") if name else []
    if not name:
        errors["full_name"] = "Vui lòng nhập họ và tên."
    elif len(words) < 2:
        errors["full_name"] = "Vui lòng nhập đầy đủ họ và tên (ví dụ: NGUYỄN VĂN AN)."
    elif not all(ch.isalpha() or ch in " .'-" for ch in name):
        errors["full_name"] = "Họ tên chỉ được có chữ cái. Vui lòng kiểm tra lại."
    c["full_name"] = name

    # 2) Năm sinh
    by = g("birth_year", 10)
    if not re.fullmatch(r"\d{4}", by) or not (1900 <= int(by) <= today.year):
        errors["birth_year"] = f"Vui lòng nhập năm sinh gồm 4 chữ số (ví dụ: 1958)."
    c["birth_year"] = by

    # 3) Giới tính
    gender = g("gender", 10).upper()
    if gender not in GENDERS:
        errors["gender"] = "Vui lòng chọn giới tính."
    c["gender"] = gender

    # 4) Địa chỉ
    addr = g("address", 120)
    if len(addr) < 2:
        errors["address"] = "Vui lòng nhập địa chỉ (thôn/xã)."
    c["address"] = addr

    # 5) SĐT: 10 số bắt đầu bằng 0
    phone = normalize_phone(form.get("phone", ""))
    if not re.fullmatch(r"0\d{9}", phone):
        errors["phone"] = "Số điện thoại phải gồm 10 chữ số, bắt đầu bằng số 0 (ví dụ: 0912345678)."
    c["phone"] = phone

    # 6) CCCD (12 số) / CMND (9 số)
    cccd = re.sub(r"[\s.\-]", "", str(form.get("cccd", "") or ""))
    if not re.fullmatch(r"\d{9}|\d{12}", cccd):
        errors["cccd"] = "Số căn cước công dân gồm 12 chữ số (hoặc chứng minh thư cũ gồm 9 chữ số)."
    c["cccd"] = cccd

    # 7) Triệu chứng
    sym = g("symptoms", 300)
    if len(sym) < 2:
        errors["symptoms"] = "Vui lòng cho biết lý do khám / triệu chứng chính."
    c["symptoms"] = sym

    # 8) Chuyên khoa (không bắt buộc) — "Khác" thì lấy chữ người dùng ghi, như Google Form
    sp = g("specialty", 120)
    valid_sp = {v for v, _, _ in SPECIALTIES}
    if sp == OTHER:
        other = g("specialty_other", 80)
        if len(other) < 2:
            errors["specialty_other"] = "Vui lòng ghi rõ chuyên khoa muốn khám."
        c["specialty"] = "Other: " + other if other else ""
    elif sp and sp not in valid_sp:
        errors["specialty"] = "Chuyên khoa không hợp lệ. Vui lòng chọn lại."
        c["specialty"] = ""
    else:
        c["specialty"] = sp
    c["specialty_tag"] = next((t for v, _, t in SPECIALTIES if v == sp), "")

    # 9) Bác sĩ (không bắt buộc)
    doc = g("doctor", 120)
    if doc and doc not in {v for v, _, _, _ in DOCTORS}:
        errors["doctor"] = "Bác sĩ không hợp lệ. Vui lòng chọn lại."
        doc = ""
    c["doctor"] = doc

    # 10) Ngày khám: từ hôm nay đến +MAX_DAYS_AHEAD
    d = _parse_date(g("exam_date", 12))
    if d is None:
        errors["exam_date"] = "Vui lòng chọn ngày khám."
    elif d < today:
        errors["exam_date"] = "Ngày khám đã qua. Vui lòng chọn từ hôm nay trở đi."
    elif d > today + timedelta(days=MAX_DAYS_AHEAD):
        errors["exam_date"] = f"Chỉ nhận đăng ký trong vòng {MAX_DAYS_AHEAD} ngày tới."
    c["exam_date"] = d

    # 11) Giờ khám: phải là một khung giờ có sẵn; hôm nay thì không được là giờ đã qua
    t = g("exam_time", 6)
    if t not in ALL_SLOTS:
        errors["exam_time"] = "Vui lòng chọn giờ khám dự kiến."
    elif d == today and datetime.combine(today, datetime.strptime(t, "%H:%M").time()) < now.replace(tzinfo=None):
        errors["exam_time"] = "Giờ này đã qua. Vui lòng chọn giờ khác hoặc ngày khác."
    c["exam_time"] = t

    # 12) Hai cam kết bắt buộc
    if form.get("agree_truth") != "1":
        errors["agree_truth"] = "Cần tích vào ô cam kết thông tin đúng sự thật."
    if form.get("agree_terms") != "1":
        errors["agree_terms"] = "Cần tích vào ô đồng ý điều khoản đặt lịch."
    return c, errors


def _parse_date(s: str):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def first_error_step(errors: dict) -> int:
    return min((FIELD_STEP.get(k, 5) for k in errors), default=1)


# ── Ghi Sheet ─────────────────────────────────────────────────────────────────────────────────────────────────
def build_field_map(c: dict, now: datetime) -> dict:
    from services import sheets as S
    return {
        S.COL_TIMESTAMP:   now.strftime("%d/%m/%Y %H:%M:%S"),
        S.COL_NAME:        c["full_name"],
        S._COL_ADDRESS_FORM: sheet_safe(c["address"]),
        S.COL_BIRTH_YEAR:  c["birth_year"],
        S.COL_GENDER:      c["gender"],
        S.COL_CCCD:        as_text_cell(c["cccd"]),
        S.COL_PHONE:       as_text_cell(c["phone"]),
        S.COL_SYMPTOM:     sheet_safe(c["symptoms"]),
        S.COL_SPECIALTY:   sheet_safe(c["specialty"]),
        S.COL_DOCTOR:      sheet_safe(c["doctor"]),
        S.COL_EXAM_DATE:   c["exam_date"].strftime("%d/%m/%Y"),    # append_field_maps đổi sang serial
        S.COL_EXAM_TIME:   c["exam_time"],
        S.COL_CONSENT_1:   "CÓ",
        S.COL_CONSENT_2:   "CÓ",
        # NGUỒN / TRẠNG THÁI / KHOA / TUỔI: để trống như dòng do Google Form tạo ra
    }


def submit(c: dict, now: datetime | None = None):
    """Ghi 1 đăng ký. Trả về (ok, thông báo lỗi | None)."""
    from services.sheets import append_field_maps
    now = now or vn_now()
    n, err = append_field_maps([build_field_map(c, now)])
    return (n == 1 and not err), err


# ── Chống lạm dụng ─────────────────────────────────────────────────────────────────────────────────────────────
def new_form_state() -> dict:
    return {"reg_csrf": secrets.token_urlsafe(24), "reg_t0": int(time.time())}


def check_state(session_state: dict, form, now_ts: float | None = None) -> str | None:
    """Trả về thông báo lỗi nếu phiên/CSRF/honeypot/thời gian điền không hợp lệ."""
    now_ts = now_ts or time.time()
    token = session_state.get("reg_csrf")
    sent = str(form.get("csrf", ""))
    if not token or not hmac.compare_digest(token, sent):
        return "Trang đã hết hạn. Vui lòng tải lại trang rồi điền lại."
    if form.get("website"):                                    # ô ẩn (honeypot) — chỉ máy mới điền
        return "Không thể gửi đăng ký."
    t0 = session_state.get("reg_t0") or 0
    if now_ts - t0 > MAX_AGE_SECONDS:
        return "Trang đã mở quá lâu. Vui lòng tải lại trang rồi điền lại."
    if now_ts - t0 < MIN_FILL_SECONDS:
        return "Vui lòng kiểm tra lại thông tin rồi bấm Gửi."
    return None


def rate_limited(ip: str, now_ts: float | None = None) -> bool:
    """Ghi nhận 1 lượt gửi; True nếu IP này (hoặc toàn hệ thống) gửi quá nhiều."""
    now_ts = now_ts or time.time()
    with _lock:
        q = _hits.setdefault(ip or "?", deque())
        while q and now_ts - q[0] > IP_WINDOW:
            q.popleft()
        while _global_hits and now_ts - _global_hits[0] > GLOBAL_WINDOW:
            _global_hits.popleft()
        if len(q) >= IP_LIMIT or len(_global_hits) >= GLOBAL_LIMIT:
            return True
        q.append(now_ts)
        _global_hits.append(now_ts)
        if len(_hits) > 5000:                                  # dọn bộ nhớ
            for k in [k for k, v in _hits.items() if not v or now_ts - v[-1] > IP_WINDOW]:
                _hits.pop(k, None)
        return False


def dup_key(c: dict) -> tuple:
    from services.sheets import norm_name
    return (norm_name(c["full_name"]), c["phone"], c["exam_date"])


def recently_submitted(c: dict, now_ts: float | None = None) -> bool:
    now_ts = now_ts or time.time()
    with _lock:
        for k in [k for k, t in _recent.items() if now_ts - t > DUP_WINDOW]:
            _recent.pop(k, None)
        return dup_key(c) in _recent


def remember_submitted(c: dict, now_ts: float | None = None):
    with _lock:
        _recent[dup_key(c)] = now_ts or time.time()


def already_on_sheet(df, c: dict) -> bool:
    """Đã có đăng ký cùng Họ tên + SĐT + Ngày khám trên Sheet chưa (kể cả do chính hệ thống này hoặc Google Form ghi)."""
    from services.sheets import check_sheet_duplicates
    cand = {"HỌ TÊN": c["full_name"], "NGÀY HẸN": c["exam_date"].strftime("%d/%m/%Y"),
            "NGÀY LẬP": "", "SỐ ĐIỆN THOẠI": c["phone"]}
    _, dup = check_sheet_duplicates(df, [cand])
    return bool(dup)


def reset_state_for_tests():
    with _lock:
        _hits.clear()
        _global_hits.clear()
        _recent.clear()
