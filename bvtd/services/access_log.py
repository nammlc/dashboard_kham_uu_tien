"""
services/access_log.py — nhật ký truy cập vào trang web (bảng access_log trong PostgreSQL).

Ghi các sự kiện: đăng nhập / đăng nhập sai / đăng xuất / hết phiên, admin xem trang hoặc thao tác ghi dữ liệu,
bệnh nhân vào / gửi trang đăng ký. KHÔNG ghi: mật khẩu, nội dung form, tham số tìm kiếm (?q=...) — chỉ ghi đường dẫn.
"""
import re
import threading
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import db

VN = ZoneInfo("Asia/Ho_Chi_Minh")

# Nhãn hiển thị cho từng loại sự kiện
EVENTS = {
    "login_ok":        "Đăng nhập",
    "login_fail":      "Đăng nhập sai",
    "otp_setup":       "Bật xác thực 2 bước",
    "logout":          "Đăng xuất",
    "session_expired": "Hết phiên (không thao tác)",
    "page":            "Xem trang",
    "action":          "Thao tác ghi dữ liệu",
    "register_view":   "Bệnh nhân vào trang đăng ký",
    "register_submit": "Bệnh nhân gửi đăng ký",
}


class AccessLog:
    def __init__(self, url: str, retention_days: int = 90):
        self.url = url
        self.retention_days = max(1, int(retention_days))
        self._inserts = 0
        self._lock = threading.Lock()
        self.pool = db.pool(url)                       # đồng thời tạo bảng nếu chưa có
        self.purge()

    # ── ghi ────────────────────────────────────────────────────────────────
    def add(self, event, *, email="", ip="", ua="", method="", path="", status=None, detail=""):
        with self.pool.connection() as c:
            c.execute("INSERT INTO access_log (event, email, ip, ua, method, path, status, detail) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                      (event, (email or "")[:120], (ip or "")[:64], (ua or "")[:300],
                       (method or "")[:10], (path or "")[:300], status, (detail or "")[:300]))
        with self._lock:
            self._inserts += 1
            purge_now = self._inserts % 200 == 0
        if purge_now:
            self.purge()

    def purge(self):
        """Xoá bản ghi cũ hơn `retention_days` ngày."""
        with self.pool.connection() as c:
            c.execute("DELETE FROM access_log WHERE ts < now() - make_interval(days => %s)", (self.retention_days,))

    # ── đọc ────────────────────────────────────────────────────────────────
    @staticmethod
    def _where(q="", event="", d_from=None, d_to=None):
        sql, args = [], []
        if event in EVENTS:
            sql.append("event = %s"); args.append(event)
        if d_from:
            sql.append("ts >= %s"); args.append(datetime.combine(d_from, dtime.min, VN))
        if d_to:
            sql.append("ts < %s");  args.append(datetime.combine(d_to + timedelta(days=1), dtime.min, VN))
        q = (q or "").strip()[:80]
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql.append("(email ILIKE %s ESCAPE '\\' OR ip ILIKE %s ESCAPE '\\' OR path ILIKE %s ESCAPE '\\' "
                       "OR detail ILIKE %s ESCAPE '\\')")
            args += [like] * 4
        return (" WHERE " + " AND ".join(sql)) if sql else "", args

    def query(self, q="", event="", d_from=None, d_to=None, page=1, per_page=50):
        where, args = self._where(q, event, d_from, d_to)
        page = max(1, int(page))
        with self.pool.connection() as c:
            total = c.execute("SELECT count(*) AS n FROM access_log" + where, args).fetchone()["n"]
            rows = c.execute("SELECT * FROM access_log" + where + " ORDER BY ts DESC, id DESC LIMIT %s OFFSET %s",
                             args + [per_page, (page - 1) * per_page]).fetchall()
        return rows, total

    def stats(self):
        """Số liệu nhanh: lượt ghi nhận hôm nay, đăng nhập sai 24 giờ qua, tài khoản hoạt động hôm nay."""
        start_today = datetime.combine(datetime.now(VN).date(), dtime.min, VN)
        with self.pool.connection() as c:
            r = c.execute(
                "SELECT count(*) FILTER (WHERE ts >= %(t)s) AS today, "
                "       count(*) FILTER (WHERE event = 'login_fail' AND ts >= now() - interval '24 hours') AS fails, "
                "       count(DISTINCT email) FILTER (WHERE ts >= %(t)s AND email <> '' "
                "                                      AND event IN ('login_ok','page','action')) AS users "
                "FROM access_log WHERE ts >= least(%(t)s, now() - interval '24 hours')", {"t": start_today}).fetchone()
        return {"today": r["today"], "fails_24h": r["fails"], "users_today": r["users"],
                "retention_days": self.retention_days}


_instances: dict[str, AccessLog] = {}
_inst_lock = threading.Lock()


def get(url: str, retention_days: int = 90) -> AccessLog:
    """Một AccessLog cho mỗi DATABASE_URL (dùng chung giữa các thread)."""
    with _inst_lock:
        inst = _instances.get(url)
    if inst is None:
        inst = AccessLog(url, retention_days)            # có thể ném lỗi kết nối → không lưu vào cache
        with _inst_lock:
            inst = _instances.setdefault(url, inst)
    inst.retention_days = max(1, int(retention_days))
    return inst


# ── hiển thị ─────────────────────────────────────────────────────────────────
def fmt_time(ts) -> str:
    """datetime (có múi giờ) → 'dd/mm/YYYY HH:MM:SS' theo giờ Việt Nam."""
    return ts.astimezone(VN).strftime("%d/%m/%Y %H:%M:%S")


def short_ua(ua: str) -> str:
    """User-Agent → 'Chrome · Windows' (gọn, đủ để nhận ra thiết bị)."""
    u = ua or ""
    if not u:
        return "—"
    if re.search(r"bot|crawl|spider|slurp", u, re.I):
        browser = "Bot"
    elif "Edg/" in u or "EdgA/" in u:
        browser = "Edge"
    elif "OPR/" in u or "Opera" in u:
        browser = "Opera"
    elif "Firefox/" in u:
        browser = "Firefox"
    elif "CriOS/" in u or "Chrome/" in u:
        browser = "Chrome"
    elif "Safari/" in u:
        browser = "Safari"
    else:
        browser = "Khác"
    if "Android" in u:
        osn = "Android"
    elif "iPhone" in u or "iPad" in u or "iPod" in u:
        osn = "iOS"
    elif "Windows" in u:
        osn = "Windows"
    elif "Mac OS X" in u or "Macintosh" in u:
        osn = "macOS"
    elif "Linux" in u:
        osn = "Linux"
    else:
        osn = ""
    return f"{browser} · {osn}" if osn else browser
