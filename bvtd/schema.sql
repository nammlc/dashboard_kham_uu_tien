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
