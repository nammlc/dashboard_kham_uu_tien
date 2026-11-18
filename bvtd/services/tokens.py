"""services/tokens.py — liên kết đặt lại (mật khẩu / xác thực 2 bước) gửi qua email.

Token do itsdangerous ký bằng SECRET_KEY, KHÔNG lưu trong DB. Mỗi token ghi kèm "vân tay" của dữ liệu sẽ bị đổi khi dùng xong
(băm mật khẩu hoặc mã bí mật 2 bước) → dùng 1 lần là các liên kết cũ tự hết hiệu lực, không cần bảng theo dõi.
Mỗi mục đích (purpose) có "muối" riêng nên liên kết đặt mật khẩu không thể dùng cho đặt lại 2 bước và ngược lại.
"""
import hashlib
import hmac

from itsdangerous import BadData, URLSafeTimedSerializer

PASSWORD_RESET = "pw-reset"
TWOFA_RESET = "2fa-reset"
EMAIL_CHANGE = "email-change"
DEFAULT_MAX_AGE = 30 * 60


def fingerprint(material) -> str:
    return hashlib.sha256(str(material or "").encode()).hexdigest()[:24]


def _ser(secret_key: str, purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=f"bvtd-{purpose}")


def make(secret_key: str, purpose: str, email: str, material, extra: str = "") -> str:
    """extra: dữ liệu đi kèm được ký luôn (vd. email mới khi đổi email) — người nhận không sửa được."""
    return _ser(secret_key, purpose).dumps({"e": str(email).lower(), "f": fingerprint(material), "x": str(extra)})


def read_ex(secret_key: str, purpose: str, token: str, max_age: int | None = None):
    """→ (email, vân_tay, extra) nếu token đúng chữ ký, đúng mục đích, chưa hết hạn; ngược lại None."""
    try:
        d = _ser(secret_key, purpose).loads(str(token or ""), max_age=DEFAULT_MAX_AGE if max_age is None else max_age)
        return str(d["e"]), str(d["f"]), str(d.get("x", ""))
    except (BadData, KeyError, TypeError, AttributeError):
        return None


def read(secret_key: str, purpose: str, token: str, max_age: int | None = None):
    """→ (email, vân_tay) nếu token đúng chữ ký, đúng mục đích, chưa hết hạn; ngược lại None."""
    r = read_ex(secret_key, purpose, token, max_age)
    return (r[0], r[1]) if r else None


def matches(fp: str, material) -> bool:
    return hmac.compare_digest(str(fp), fingerprint(material))
