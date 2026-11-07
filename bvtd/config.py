import os

class Config:
    # ── Bảo mật ──────────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-thay-doi-truoc-khi-deploy")

    # ── Google OAuth (cho nút "Đăng nhập bằng Google") ───────────────────────
    GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    # Tuỳ chọn: ép địa chỉ callback (vd https://quanly.example.com/login/google/callback) khi proxy không gửi đúng
    # X-Forwarded-Proto/Host. Để trống = tự lấy theo địa chỉ trang đang mở. PHẢI trùng "Authorized redirect URI" trên Google.
    GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()

    # ── Danh sách email được phép đăng nhập (phân cách bởi dấu phẩy) ─────────
    ALLOWED_EMAILS = set(
        e.strip() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
    )

    # ── Xác minh quyền sở hữu website với công cụ tìm kiếm (tuỳ chọn): chỉ dán phần "content" của thẻ meta ──
    GOOGLE_SITE_VERIFICATION = os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip()
    BING_SITE_VERIFICATION   = os.environ.get("BING_SITE_VERIFICATION", "").strip()

    # ── Xác thực 2 bước (mã OTP 6 số từ Google Authenticator) sau khi đăng nhập bằng mật khẩu / Google ──
    # 1 = bắt buộc với mọi admin (lần đăng nhập đầu sẽ hiện mã QR để quét). 0 = tắt (chỉ dùng khi khẩn cấp).
    TWO_FACTOR_ENABLED = os.environ.get("TWO_FACTOR_ENABLED", "1") != "0"
    TWO_FACTOR_ISSUER  = os.environ.get("TWO_FACTOR_ISSUER", "BVĐK Tâm Đức Cầu Quan").strip() or "BVĐK Tâm Đức Cầu Quan"

    # ── Phiên đăng nhập admin: sau N phút KHÔNG thao tác trên trang web thì tự đăng xuất ──
    SESSION_IDLE_MINUTES = int(os.environ.get("SESSION_IDLE_MINUTES", "15"))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True

    # ── Cơ sở dữ liệu PostgreSQL: bảng admin_users (tài khoản admin) + access_log (nhật ký truy cập) ──
    # Dạng: postgresql://USER:MAT_KHAU@HOST:5432/TEN_DB   (mật khẩu có ký tự đặc biệt phải mã hoá URL, vd @ → %40)
    # Trên Coolify: dùng "Postgres URL (internal)" của database đã tạo. Xem HUONG_DAN_POSTGRES.md.
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    ACCESS_LOG_ENABLED = os.environ.get("ACCESS_LOG_ENABLED", "1") != "0"
    LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "90"))        # nhật ký cũ hơn N ngày tự xoá

    # ── Mật khẩu admin LẦN ĐẦU (chỉ dùng để tạo tài khoản khi bảng admin_users còn trống) ──────────
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

    # ── Google Sheets ─────────────────────────────────────────────────────────
    SHEET_ID   = os.environ.get("SHEET_ID", "1EYiRA3ar41aue8DlbWA7JTKoLL0M2tiLTcZINhdMfTs")
    SHEET_NAME = os.environ.get("SHEET_NAME", "Câu trả lời biểu mẫu 1")

    # ── Trang đăng ký khám công khai (/dang-ky). Đặt REG_ENABLED=0 để tạm đóng ──────────
    REG_ENABLED = os.environ.get("REG_ENABLED", "1") != "0"

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
