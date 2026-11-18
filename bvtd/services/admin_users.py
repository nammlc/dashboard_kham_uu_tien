"""
services/admin_users.py — tài khoản admin lưu trong PostgreSQL (bảng admin_users).

• Mật khẩu chỉ lưu dạng băm (werkzeug scrypt) — không bao giờ lưu mật khẩu thô.
• password_hash rỗng = tài khoản chỉ đăng nhập bằng Google, không đăng nhập bằng mật khẩu được.
• Lần chạy đầu (bảng còn trống) tự tạo tài khoản từ biến môi trường ALLOWED_EMAILS + ADMIN_PASSWORD để không gián đoạn;
  sau đó cơ sở dữ liệu là nguồn đúng duy nhất (đổi ADMIN_PASSWORD trong .env không còn tác dụng — dùng `flask admin-passwd`).
"""
import re
import threading

from psycopg import errors as pg_errors
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

# Băm giả để thời gian kiểm tra tương đương khi email không tồn tại (khó dò email nào có tài khoản)
_DUMMY_HASH = generate_password_hash("khong-ton-tai")


def norm_email(e) -> str:
    return str(e or "").strip().lower()


def _check_password_rules(password: str):
    if len(password or "") < 8:
        raise ValueError("Mật khẩu phải có ít nhất 8 ký tự.")


check_password_rules = _check_password_rules                  # dùng ở nơi cần kiểm tra TRƯỚC khi ghi (vd. trước khi tiêu thụ mã OTP)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def check_email(email: str) -> str:
    e = norm_email(email)
    if len(e) > 120 or not _EMAIL_RE.fullmatch(e):
        raise ValueError("Email không hợp lệ.")
    return e


class AdminUsers:
    def __init__(self, url: str):
        self.url = url
        self.pool = db.pool(url)                        # đồng thời tạo bảng nếu chưa có

    def _one(self, sql, args=()):
        with self.pool.connection() as c:
            return c.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        with self.pool.connection() as c:
            return c.execute(sql, args).fetchall()

    def _write(self, sql, args=()) -> int:
        with self.pool.connection() as c:
            return c.execute(sql, args).rowcount

    # ── đọc ────────────────────────────────────────────────────────────────
    def count(self) -> int:
        return self._one("SELECT count(*) AS n FROM admin_users")["n"]

    def get(self, email):
        return self._one("SELECT * FROM admin_users WHERE email = %s", (norm_email(email),))

    def get_active(self, email):
        u = self.get(email)
        return u if u and u["is_active"] else None

    def list(self):
        return self._all("SELECT id, email, name, is_active, created_at, last_login_at, "
                         "(password_hash <> '') AS has_password, (totp_secret <> '') AS has_totp "
                         "FROM admin_users ORDER BY email")

    # ── ghi ────────────────────────────────────────────────────────────────
    def add(self, email, password="", name=""):
        """Thêm tài khoản. password rỗng → chỉ đăng nhập Google. Trả False nếu email đã tồn tại."""
        email = norm_email(email)
        if not email or "@" not in email:
            raise ValueError("Email không hợp lệ.")
        if password:
            _check_password_rules(password)
        return self._write(
            "INSERT INTO admin_users (email, name, password_hash) VALUES (%s,%s,%s) ON CONFLICT (email) DO NOTHING",
            (email, name or email.split("@")[0], generate_password_hash(password) if password else "")) > 0

    def set_password(self, email, password) -> bool:
        _check_password_rules(password)
        return self._write("UPDATE admin_users SET password_hash = %s WHERE email = %s",
                           (generate_password_hash(password), norm_email(email))) > 0

    def set_active(self, email, active: bool) -> bool:
        return self._write("UPDATE admin_users SET is_active = %s WHERE email = %s", (bool(active), norm_email(email))) > 0

    def change_email(self, old, new) -> bool:
        """Đổi email đăng nhập của 1 tài khoản (giữ nguyên mật khẩu, 2 bước, trạng thái). False nếu email mới đã thuộc tài khoản khác
        hoặc không tìm thấy tài khoản cũ. Email không hợp lệ → ValueError."""
        new = check_email(new)
        try:
            return self._write("UPDATE admin_users SET email = %s WHERE email = %s", (new, norm_email(old))) > 0
        except pg_errors.UniqueViolation:
            return False

    def touch_login(self, email, name="", picture=""):
        self._write("UPDATE admin_users SET last_login_at = now(), name = COALESCE(NULLIF(%s, ''), name), "
                    "picture = COALESCE(NULLIF(%s, ''), picture) WHERE email = %s", (name, picture, norm_email(email)))

    # ── xác thực 2 bước (TOTP) ─────────────────────────────────────────────
    def set_totp(self, email, enc_secret: str, step: int, expect_old: str = "") -> bool:
        """Lưu mã bí mật (đã mã hoá) khi thiết lập. CHỈ ghi nếu mã bí mật hiện tại đúng bằng `expect_old` (rỗng = chưa có) —
        không ai có thể ghi đè mã của tài khoản đã thiết lập, kể cả khi hai lượt thiết lập chạy song song."""
        return self._write("UPDATE admin_users SET totp_secret = %s, totp_enabled_at = now(), totp_last_step = %s "
                           "WHERE email = %s AND totp_secret = %s", (enc_secret, int(step), norm_email(email), expect_old)) > 0

    def use_totp_step(self, email, step: int) -> bool:
        """Đánh dấu mã (bước thời gian `step`) đã dùng. Trả False nếu đã có mã mới hơn/bằng được dùng → chặn dùng lại,
        kể cả 2 yêu cầu cùng lúc (so sánh-và-ghi nằm trong 1 câu lệnh SQL)."""
        return self._write("UPDATE admin_users SET totp_last_step = %s WHERE email = %s AND totp_last_step < %s",
                           (int(step), norm_email(email), int(step))) > 0

    def reset_totp(self, email) -> bool:
        """Xoá thiết lập 2 bước (mất điện thoại): lần đăng nhập sau người đó phải quét QR lại."""
        return self._write("UPDATE admin_users SET totp_secret = '', totp_enabled_at = NULL, totp_last_step = 0 "
                           "WHERE email = %s", (norm_email(email),)) > 0

    # ── xác thực ───────────────────────────────────────────────────────────
    def verify(self, email, password):
        """Đúng email + mật khẩu + tài khoản đang bật → trả bản ghi; ngược lại None."""
        u = self.get_active(email)
        stored = (u or {}).get("password_hash") or ""
        ok = check_password_hash(stored or _DUMMY_HASH, password or "")
        return u if (u and stored and ok) else None

    def seed_from_env(self, emails, password) -> int:
        """Bảng còn trống → tạo tài khoản từ ALLOWED_EMAILS (+ ADMIN_PASSWORD nếu có). Trả số tài khoản đã tạo."""
        if self.count() > 0:
            return 0
        ph = generate_password_hash(password) if password else ""     # mật khẩu .env cũ có thể ngắn hơn quy tắc mới → vẫn nhận
        n = 0
        for e in sorted({norm_email(x) for x in emails if "@" in norm_email(x)}):
            n += self._write("INSERT INTO admin_users (email, name, password_hash) VALUES (%s,%s,%s) "
                             "ON CONFLICT (email) DO NOTHING", (e, e.split("@")[0], ph))
        return n


_instances: dict[str, AdminUsers] = {}
_inst_lock = threading.Lock()


def get(url: str) -> AdminUsers:
    with _inst_lock:
        inst = _instances.get(url)
    if inst is None:
        inst = AdminUsers(url)
        with _inst_lock:
            inst = _instances.setdefault(url, inst)
    return inst
