-- schema.sql — cấu trúc cơ sở dữ liệu PostgreSQL của hệ thống (bvtd)
-- Ứng dụng TỰ chạy các lệnh này ở lần khởi động đầu tiên. File này để bạn đọc / tạo tay khi cần:
--   psql "$DATABASE_URL" -f schema.sql

CREATE TABLE IF NOT EXISTS admin_users (
    id            BIGSERIAL   PRIMARY KEY,
    email         TEXT        NOT NULL UNIQUE,                 -- luôn chữ thường
    name          TEXT        NOT NULL DEFAULT '',
    password_hash TEXT        NOT NULL DEFAULT '',             -- rỗng = chỉ đăng nhập bằng Google
    picture       TEXT        NOT NULL DEFAULT '',
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- Xác thực 2 bước (TOTP). Chạy lại nhiều lần không sao (IF NOT EXISTS).
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_secret     TEXT        NOT NULL DEFAULT '';   -- mã bí mật đã MÃ HOÁ; rỗng = chưa thiết lập
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_enabled_at TIMESTAMPTZ;                       -- thời điểm thiết lập 2 bước
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_last_step  BIGINT      NOT NULL DEFAULT 0;    -- bước thời gian của mã đã dùng gần nhất (chống dùng lại)

CREATE TABLE IF NOT EXISTS access_log (
    id     BIGSERIAL   PRIMARY KEY,
    ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    event  TEXT        NOT NULL,
    email  TEXT        NOT NULL DEFAULT '',
    ip     TEXT        NOT NULL DEFAULT '',
    ua     TEXT        NOT NULL DEFAULT '',
    method TEXT        NOT NULL DEFAULT '',
    path   TEXT        NOT NULL DEFAULT '',
    status INTEGER,
    detail TEXT        NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_access_log_ts       ON access_log (ts DESC);
CREATE INDEX IF NOT EXISTS ix_access_log_event_ts ON access_log (event, ts DESC);
