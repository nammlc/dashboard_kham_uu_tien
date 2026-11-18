"""services/mailer.py — gửi email qua SMTP (chỉ dùng thư viện chuẩn smtplib).

Cấu hình bằng biến môi trường (xem env.example): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_SECURITY.
SMTP_SECURITY: "starttls" (cổng 587, mặc định) · "ssl" (cổng 465) · "none" (chỉ dùng trong mạng nội bộ).
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

TIMEOUT_S = 10


class MailError(RuntimeError):
    """Gửi email không thành công (lý do đã được viết gọn, không chứa mật khẩu)."""


def _is_gmail(host: str) -> bool:
    return str(host).lower().endswith(("gmail.com", "googlemail.com"))


def sender(cfg) -> str:
    return (cfg.get("SMTP_FROM") or cfg.get("SMTP_USER") or "").strip()


def configured(cfg) -> bool:
    return bool((cfg.get("SMTP_HOST") or "").strip() and "@" in parseaddr(sender(cfg))[1])


def send(cfg, to: str, subject: str, body: str) -> None:
    """Gửi 1 email văn bản thuần (UTF-8). Lỗi → MailError."""
    if not configured(cfg):
        raise MailError("Chưa cấu hình SMTP_HOST / SMTP_FROM.")
    msg = EmailMessage()
    name, addr = parseaddr(sender(cfg))
    msg["From"] = formataddr((name or cfg.get("TWO_FACTOR_ISSUER", "") or "", addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    host, security = cfg["SMTP_HOST"].strip(), (cfg.get("SMTP_SECURITY") or "starttls").lower()
    port = int(cfg.get("SMTP_PORT") or (465 if security == "ssl" else 587))
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_S, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_S)
        with server:
            if security == "starttls":
                server.starttls(context=ctx)
            if cfg.get("SMTP_USER"):
                server.login(cfg["SMTP_USER"], cfg.get("SMTP_PASSWORD", ""))
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        if _is_gmail(host):
            raise MailError("Gmail từ chối đăng nhập. Hãy dùng “Mật khẩu ứng dụng” 16 ký tự (không phải mật khẩu Gmail): "
                            "bật xác minh 2 bước cho tài khoản Google rồi tạo tại myaccount.google.com/apppasswords. "
                            "GMAIL_USER phải đúng là địa chỉ Gmail đó.") from None
        raise MailError("Máy chủ email từ chối tên đăng nhập / mật khẩu SMTP.") from None
    except (smtplib.SMTPException, OSError, ValueError) as e:
        hint = (" Nếu máy chủ chặn cổng 587, thử SMTP_SECURITY=ssl và SMTP_PORT=465."
                if _is_gmail(host) and isinstance(e, (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)) else "")
        raise MailError(f"Không gửi được email ({type(e).__name__}: {e}).{hint}") from None
