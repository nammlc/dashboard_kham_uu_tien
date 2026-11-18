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

-- ── Dữ liệu bệnh nhân (thay Google Sheet). 1 dòng = 1 lượt đăng ký, giống 1 dòng trên Sheet cũ ──────────────
CREATE TABLE IF NOT EXISTS patients (
    id            BIGSERIAL   PRIMARY KEY,
    stt           BIGINT,                                         -- STT hiển thị (giữ số cũ trên Sheet, dòng mới = lớn nhất + 1)
    registered_at TIMESTAMP,                                      -- "Dấu thời gian" (giờ Việt Nam)
    source        TEXT        NOT NULL DEFAULT '',                -- NGUỒN BỆNH NHÂN
    status        TEXT,                                           -- TRẠNG THÁI, NULL = chưa có
    exam_date     DATE,                                           -- NGÀY KHÁM (ngày hẹn)
    full_name     TEXT        NOT NULL DEFAULT '',
    phone         TEXT        NOT NULL DEFAULT '',                -- chỉ chữ số, '' = không có
    birth_year    SMALLINT,
    age_text      TEXT        NOT NULL DEFAULT '',                -- cột TUỔI ('38 tuổi', '25 tháng tuổi')
    khoa          TEXT        NOT NULL DEFAULT '',                -- KHOA KHÁM CHỮA BỆNH
    gender        TEXT,                                           -- 'Nam' / 'Nữ' / NULL
    address       TEXT        NOT NULL DEFAULT '',
    commune       TEXT        NOT NULL DEFAULT '',                -- xã/phường (tự tách từ địa chỉ, sửa tay được)
    symptom       TEXT        NOT NULL DEFAULT '',
    cccd          TEXT        NOT NULL DEFAULT '',
    specialty     TEXT        NOT NULL DEFAULT '',
    doctor        TEXT        NOT NULL DEFAULT '',
    exam_time     TEXT        NOT NULL DEFAULT '',
    consent_truth TEXT        NOT NULL DEFAULT '',
    consent_terms TEXT        NOT NULL DEFAULT '',
    actual_date   DATE,                                           -- NGÀY THỰC KHÁM
    created_date  DATE,                                           -- NGÀY TẠO LỊCH HẸN KHÁM
    extra         TEXT        NOT NULL DEFAULT '{}',              -- JSON: cột lạ trên Sheet + giá trị gốc không đọc được
    legacy_row    INTEGER,                                        -- số dòng trên Sheet lúc nhập (để đối soát)
    version       INTEGER     NOT NULL DEFAULT 1,                 -- tăng mỗi lần sửa, chống 2 người ghi đè nhau
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT        NOT NULL DEFAULT '',
    deleted_at    TIMESTAMPTZ,                                    -- xoá mềm: khác NULL = đã xoá
    deleted_by    TEXT        NOT NULL DEFAULT '',
    CONSTRAINT ck_patients_status CHECK (status IS NULL OR status IN ('BỆNH NHÂN ĐÃ KHÁM', 'BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM')),
    CONSTRAINT ck_patients_gender CHECK (gender IS NULL OR gender IN ('Nam', 'Nữ')),
    CONSTRAINT ck_patients_birth  CHECK (birth_year IS NULL OR birth_year BETWEEN 1900 AND 2100)
);
CREATE INDEX IF NOT EXISTS ix_patients_exam_date ON patients (exam_date);
CREATE INDEX IF NOT EXISTS ix_patients_stt       ON patients (stt);
CREATE INDEX IF NOT EXISTS ix_patients_phone     ON patients (phone);
CREATE UNIQUE INDEX IF NOT EXISTS ux_patients_legacy_row ON patients (legacy_row);

CREATE TABLE IF NOT EXISTS patient_history (
    id         BIGSERIAL   PRIMARY KEY,
    patient_id BIGINT      NOT NULL REFERENCES patients (id),
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    by_email   TEXT        NOT NULL DEFAULT '',
    action     TEXT        NOT NULL,                              -- insert, update, delete, restore, import
    changes    TEXT        NOT NULL DEFAULT '{}'                  -- JSON: {cột: [giá trị cũ, giá trị mới]}
);
CREATE INDEX IF NOT EXISTS ix_patient_history_pid ON patient_history (patient_id, id DESC);

-- ── SMS: chiến dịch gửi, từng tin, danh sách từ chối, cài đặt ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sms_campaigns (
    id           BIGSERIAL   PRIMARY KEY,
    kind         TEXT        NOT NULL,                              -- appointment, mobile, ksk, test
    name         TEXT        NOT NULL DEFAULT '',
    campaign_key TEXT,                                              -- khoá chống tạo trùng, vd. auto:2026-11-18
    event_id     BIGINT,                                            -- sự kiện khám lưu động (nếu có)
    body         TEXT        NOT NULL,                              -- mẫu tin (còn biến như {ten}, {ngay})
    filters      TEXT        NOT NULL DEFAULT '{}',                 -- JSON: bộ lọc đã dùng khi chọn người nhận
    mode         TEXT        NOT NULL DEFAULT 'live',               -- live = gửi thật, sandbox = chỉ thử
    status       TEXT        NOT NULL DEFAULT 'queued',             -- queued, done, cancelled
    created_by   TEXT        NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sms_campaigns_key ON sms_campaigns (campaign_key);

CREATE TABLE IF NOT EXISTS sms_messages (
    id              BIGSERIAL   PRIMARY KEY,
    campaign_id     BIGINT      NOT NULL REFERENCES sms_campaigns (id),
    patient_id      BIGINT,                                         -- patients.id (hoặc số dòng khi còn dùng Sheet)
    contact_id      BIGINT,                                         -- sms_contacts.id (danh bạ khám lưu động)
    exam_date       DATE,
    phone           TEXT        NOT NULL,
    recipient_name  TEXT        NOT NULL DEFAULT '',
    body            TEXT        NOT NULL,                           -- nội dung ĐÃ ghép, đúng như gửi đi
    segments        INTEGER     NOT NULL DEFAULT 1,                 -- số tin tính phí
    status          TEXT        NOT NULL DEFAULT 'queued',          -- queued, sending, sent, delivered, failed, skipped, unknown
    skip_reason     TEXT        NOT NULL DEFAULT '',
    provider_msg_id TEXT        NOT NULL DEFAULT '',
    error           TEXT        NOT NULL DEFAULT '',
    attempts        INTEGER     NOT NULL DEFAULT 0,
    queued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    delivered_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_sms_messages_campaign ON sms_messages (campaign_id, status);
CREATE INDEX IF NOT EXISTS ix_sms_messages_patient  ON sms_messages (patient_id, exam_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sms_messages_campaign_phone ON sms_messages (campaign_id, phone) WHERE status <> 'skipped';

CREATE TABLE IF NOT EXISTS sms_optout (
    phone      TEXT        PRIMARY KEY,                             -- SĐT đã chuẩn hoá (0xxxxxxxxx)
    reason     TEXT        NOT NULL DEFAULT '',
    created_by TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sms_settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL DEFAULT '',
    updated_by TEXT        NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── SMS giai đoạn 2–3: sự kiện khám lưu động, danh bạ (kèm đồng ý), nhóm vùng ─────────────────────────────────
CREATE TABLE IF NOT EXISTS sms_events (
    id         BIGSERIAL   PRIMARY KEY,
    name       TEXT        NOT NULL,
    audience   TEXT        NOT NULL DEFAULT 'dan',                  -- dan (người dân), hocsinh, congty
    event_date DATE        NOT NULL,
    event_time TEXT        NOT NULL DEFAULT '',
    location   TEXT        NOT NULL DEFAULT '',
    note       TEXT        NOT NULL DEFAULT '',
    created_by TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sms_contacts (
    id             BIGSERIAL   PRIMARY KEY,
    event_id       BIGINT      NOT NULL REFERENCES sms_events (id),
    name           TEXT        NOT NULL DEFAULT '',
    phone          TEXT        NOT NULL,                            -- đã chuẩn hoá 0xxxxxxxxx
    commune        TEXT        NOT NULL DEFAULT '',
    org            TEXT        NOT NULL DEFAULT '',                 -- trường / lớp / công ty / thôn
    consent_source TEXT        NOT NULL,                            -- giay, qr, mieng, khac — BẮT BUỘC, không có đồng ý thì không nhập
    consent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sms_contacts_event_phone ON sms_contacts (event_id, phone) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS sms_regions (
    id         BIGSERIAL   PRIMARY KEY,
    name       TEXT        NOT NULL UNIQUE,
    communes   TEXT        NOT NULL DEFAULT '[]',                   -- JSON: danh sách xã/phường
    created_by TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cột bổ sung (DB đã tạo từ bản trước: chạy lại không sao)
ALTER TABLE patients      ADD COLUMN IF NOT EXISTS commune    TEXT   NOT NULL DEFAULT '';
ALTER TABLE sms_campaigns ADD COLUMN IF NOT EXISTS event_id   BIGINT;
ALTER TABLE sms_messages  ADD COLUMN IF NOT EXISTS contact_id BIGINT;
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
