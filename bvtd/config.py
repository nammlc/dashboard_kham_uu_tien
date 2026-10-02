import os

class Config:
    # ── Bảo mật ──────────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-thay-doi-truoc-khi-deploy")

    # ── Google OAuth (cho nút "Đăng nhập bằng Google") ───────────────────────
    GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    # ── Danh sách email được phép đăng nhập (phân cách bởi dấu phẩy) ─────────
    ALLOWED_EMAILS = set(
        e.strip() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
    )

    # ── Mật khẩu admin cho đăng nhập bằng email ──────────────────────────────
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

    # ── Google Sheets ─────────────────────────────────────────────────────────
    SHEET_ID   = os.environ.get("SHEET_ID", "1EYiRA3ar41aue8DlbWA7JTKoLL0M2tiLTcZINhdMfTs")
    SHEET_NAME = os.environ.get("SHEET_NAME", "Câu trả lời biểu mẫu 1")

    # ── Upload (file Excel Minh Lộ) ──────────────────────────────────────────
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024   # tối đa 10MB

    # ── Cache ─────────────────────────────────────────────────────────────────
    CACHE_TYPE           = "SimpleCache"
    CACHE_DEFAULT_TIMEOUT = 300   # 5 phút


class ProductionConfig(Config):
    DEBUG = False


class DevelopmentConfig(Config):
    DEBUG = True
    CACHE_DEFAULT_TIMEOUT = 30
