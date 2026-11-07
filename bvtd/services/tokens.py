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
DEFAULT_MAX_AGE = 30 * 60


def fingerprint(material) -> str:
    return hashlib.sha256(str(material or "").encode()).hexdigest()[:24]


def _ser(secret_key: str, purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=f"bvtd-{purpose}")


def make(secret_key: str, purpose: str, email: str, material) -> str:
    return _ser(secret_key, purpose).dumps({"e": str(email).lower(), "f": fingerprint(material)})


def read(secret_key: str, purpose: str, token: str, max_age: int | None = None):
    """→ (email, vân_tay) nếu token đúng chữ ký, đúng mục đích, chưa hết hạn; ngược lại None."""
    try:
        d = _ser(secret_key, purpose).loads(str(token or ""), max_age=DEFAULT_MAX_AGE if max_age is None else max_age)
        return str(d["e"]), str(d["f"])
    except (BadData, KeyError, TypeError):
        return None


def matches(fp: str, material) -> bool:
    return hmac.compare_digest(str(fp), fingerprint(material))
