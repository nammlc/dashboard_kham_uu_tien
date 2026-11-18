"""Xác thực 2 bước (TOTP — mã 6 số của Google Authenticator / Microsoft Authenticator / Authy…) cho tài khoản admin.

* Chuẩn RFC 6238: mã đổi mỗi 30 giây, chấp nhận lệch ±1 bước (đồng hồ điện thoại chậm/nhanh tối đa ~30 giây).
* Mã bí mật (secret) lưu trong DB ở dạng MÃ HOÁ (Fernet, khoá dẫn xuất từ SECRET_KEY) — lộ DB cũng không tự sinh được mã.
  Đổi SECRET_KEY thì mọi mã bí mật cũ giải mã không được → mỗi admin thiết lập lại 1 lần (an toàn hơn là tin mã hỏng).
* Chống dùng lại: mỗi mã chỉ dùng được 1 lần (nhớ bước thời gian đã dùng gần nhất, so sánh-và-ghi nguyên tử trong DB).
* Chống dò mã: giới hạn số lần sai theo từng email (bộ đếm ở đây) + theo từng lượt đăng nhập (ở app.py).
"""
import base64
import hashlib
import hmac
import re
import threading
import time
from collections import deque

import pyotp
import qrcode
import qrcode.image.svg
from cryptography.fernet import Fernet, InvalidToken

STEP_SECONDS = 30
WINDOW = 1                       # chấp nhận mã của bước trước / bước sau
DEFAULT_ISSUER = "BVĐK Tâm Đức Cầu Quan"


# ── Mã hoá mã bí mật ────────────────────────────────────────────────────────────────────────────────────────────
def _fernet(secret_key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"bvtd-totp|" + str(secret_key).encode()).digest()))


def encrypt(secret: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt(secret.encode()).decode()


def decrypt(token: str, secret_key: str) -> str | None:
    """Giải mã; rỗng / hỏng / khác khoá → None (coi như chưa thiết lập)."""
    if not token:
        return None
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# ── TOTP ────────────────────────────────────────────────────────────────────────────────────────────────────────
def new_secret() -> str:
    return pyotp.random_base32()                       # 32 ký tự base32 = 160 bit


def provisioning_uri(secret: str, email: str, issuer: str = DEFAULT_ISSUER) -> str:
    return pyotp.TOTP(secret, interval=STEP_SECONDS).provisioning_uri(name=email, issuer_name=issuer)


def normalize_code(code) -> str | None:
    """'123 456' / '123-456' → '123456'; không đúng 6 chữ số → None."""
    c = re.sub(r"[\s\-]", "", str(code or ""))
    return c if re.fullmatch(r"[0-9]{6}", c) else None          # CHỈ chữ số 0-9 (\d của Python còn nhận cả chữ số Ả Rập/Devanagari…)


def verify(secret: str, code, last_step: int = 0, now: float | None = None) -> int | None:
    """Mã đúng (trong cửa sổ ±WINDOW bước) VÀ chưa từng dùng → trả số bước thời gian của mã; ngược lại None."""
    c = normalize_code(code)
    if not c or not secret:
        return None
    now = time.time() if now is None else now
    cur = int(now // STEP_SECONDS)
    totp = pyotp.TOTP(secret, interval=STEP_SECONDS)
    hit = None
    for off in range(-WINDOW, WINDOW + 1):             # so sánh đủ cả 3 mã (không thoát sớm) để thời gian không lộ mã đúng
        step = cur + off
        if step >= 0 and hmac.compare_digest(totp.at(step * STEP_SECONDS), c):
            hit = step
    return hit if (hit is not None and hit > int(last_step or 0)) else None


def qr_svg(uri: str) -> str:
    """Mã QR dạng SVG nhúng thẳng vào trang (không cần tải ảnh/thư viện ngoài)."""
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    svg = img.to_string(encoding="unicode")
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)          # bỏ kích thước cố định (mm) → co giãn theo CSS
    return svg[svg.index("<svg"):]


def format_secret(secret: str) -> str:
    """'ABCDEFGH…' → 'ABCD EFGH …' để dễ nhập tay."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ── Giới hạn số lần nhập sai theo email ────────────────────────────────────────────────────────────────────────
class FailLimiter:
    """Quá `limit` lần sai trong `window` giây → khoá. Bộ nhớ trong tiến trình (đủ cho 1 container; sai thì chỉ mất bộ đếm)."""

    def __init__(self, limit=10, window=900):
        self.limit, self.window = limit, window
        self._d: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _trim(self, q, now):
        while q and now - q[0] > self.window:
            q.popleft()

    def locked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            q = self._d.get(key)
            if not q:
                return False
            self._trim(q, now)
            return len(q) >= self.limit

    def fail(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            q = self._d.setdefault(key, deque())
            self._trim(q, now)
            q.append(now)
            if len(self._d) > 2000:                                       # dọn bộ nhớ
                for k in [k for k, v in self._d.items() if not v or now - v[-1] > self.window]:
                    self._d.pop(k, None)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._d.clear()
            else:
                self._d.pop(key, None)
