"""
services/db.py — kết nối PostgreSQL (DATABASE_URL) + tạo bảng.

DATABASE_URL dạng:  postgresql://USER:MAT_KHAU@HOST:5432/TEN_DB      (cũng nhận postgres://)
Mật khẩu có ký tự đặc biệt (@ : / # ? %) phải mã hoá URL, ví dụ @ → %40.

Bảng được tạo tự động (CREATE TABLE IF NOT EXISTS) ở lần dùng đầu tiên; cùng nội dung với file schema.sql.
"""
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DBError = psycopg.Error            # mọi lỗi từ PostgreSQL / kết nối / hết thời gian chờ đều là lớp con của lớp này
POOL_TIMEOUT = 5                   # giây chờ lấy kết nối trước khi báo lỗi


class NotConfigured(RuntimeError):
    """Chưa đặt biến môi trường DATABASE_URL."""


SCHEMA_SQL = """
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
"""

_pools: dict[str, ConnectionPool] = {}
_schema_ready: set[str] = set()
_lock = threading.Lock()


def pool(url: str) -> ConnectionPool:
    """Một pool kết nối cho mỗi DATABASE_URL (dùng chung giữa các thread) và đảm bảo đã có bảng."""
    if not url:
        raise NotConfigured("Chưa cấu hình DATABASE_URL")
    with _lock:
        p = _pools.get(url)
        if p is None:
            p = ConnectionPool(url, min_size=1, max_size=5, timeout=POOL_TIMEOUT, open=False,
                               check=ConnectionPool.check_connection,          # bỏ kết nối chết (DB vừa khởi động lại)
                               kwargs={"row_factory": dict_row, "connect_timeout": POOL_TIMEOUT})
            p.open()                                                           # không chờ: DB chưa sẵn sàng thì app vẫn khởi động
            _pools[url] = p
    ensure_schema(url, p)
    return p


def ensure_schema(url: str, p: ConnectionPool | None = None) -> None:
    if url in _schema_ready:
        return
    p = p or pool(url)
    with p.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(727274)")                   # nhiều tiến trình khởi động cùng lúc không đạp nhau
        conn.execute(SCHEMA_SQL)
    _schema_ready.add(url)
