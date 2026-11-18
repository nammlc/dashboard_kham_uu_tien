"""
services/sms.py — gửi SMS nhắc lịch (Brandname). ĐỘC LẬP nhà cung cấp: chỉ cần viết 1 lớp `Provider` cho nhà cung cấp đã chọn.

Luồng:  chọn người nhận → tạo CHIẾN DỊCH (mỗi người 1 dòng sms_messages, trạng thái queued) → process_queue() gửi dần.
An toàn (mặc định đóng):
  • công tắc tổng `enabled` mặc định TẮT; không có nhà cung cấp thật thì chỉ chạy chế độ THỬ (sandbox, không gửi gì ra ngoài);
  • không gửi 22:00–07:00; hạn mức tin/ngày; mỗi SĐT tối đa 1 tin/đợt; tin đã gửi cho 1 lịch hẹn thì không gửi lại;
  • SĐT đã từ chối nhận tin (sms_optout) bị loại; tin lỗi không rõ kết quả (unknown) KHÔNG tự gửi lại để tránh gửi đôi.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from typing import NamedTuple

from flask import current_app, has_app_context

from services import patient_db as PD, sheets as S

DEFAULT_APPOINTMENT_TEMPLATE = ("BVDK Tam Duc Cau Quan nhac: {ten} co lich kham ngay {ngay}{gio_text}. "
                                "Can doi lich xin goi {hotline}.")
VARIABLES = {"ten", "ngay", "gio", "gio_text", "hotline", "ten_bv", "dia_diem"}
MAX_ATTEMPTS = 3
STUCK_MINUTES = 15
LIVE_STATUSES = ("queued", "sending", "sent", "delivered", "unknown")     # đã/đang có tin cho lịch hẹn này → không tạo thêm
KINDS = {"appointment": "Nhắc lịch hẹn khám", "mobile": "Nhắc khám lưu động", "ksk": "Nhắc khám sức khỏe (KSK)", "test": "Gửi thử"}


# ══════════════════════════════════════════════════════════════════════════════
# Cấu hình
# ══════════════════════════════════════════════════════════════════════════════
def cfg(name: str, default=""):
    import os
    if has_app_context() and name in current_app.config:
        return current_app.config[name]
    return os.environ.get(name, default)


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in str(cfg("SMS_ADMIN_EMAILS", "")).split(",") if e.strip()}


def is_sms_admin(email) -> bool:
    """CHỈ email nằm trong SMS_ADMIN_EMAILS mới được gửi/cài đặt. Để trống = không ai được gửi (an toàn)."""
    return bool(email) and str(email).strip().lower() in admin_emails()


def validate_brand(name: str):
    """Brandname: chữ + số không dấu, không dấu cách, tối đa 11 ký tự."""
    if not re.fullmatch(r"[A-Za-z0-9]{1,11}", name or ""):
        return False, ("Brandname chỉ gồm chữ và số không dấu, không khoảng trắng, tối đa 11 ký tự "
                       f"(«{name}» có {len(name or '')} ký tự).")
    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# Nội dung tin
# ══════════════════════════════════════════════════════════════════════════════
def strip_accents(text: str) -> str:
    t = unicodedata.normalize("NFD", str(text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def count_segments(text: str) -> int:
    """Số tin tính phí: ASCII 160 ký tự (153 nếu dài), có dấu/Unicode 70 (67 nếu dài)."""
    n = len(text)
    if n == 0:
        return 0
    if text.isascii():
        return 1 if n <= 160 else math.ceil(n / 153)
    return 1 if n <= 70 else math.ceil(n / 67)


def template_errors(body: str) -> list[str]:
    errs = []
    used = set(re.findall(r"\{(\w+)\}", body or ""))
    if not (body or "").strip():
        errs.append("Nội dung tin không được để trống.")
    if used - VARIABLES:
        errs.append("Biến không hợp lệ: " + ", ".join("{%s}" % v for v in sorted(used - VARIABLES))
                    + " — chỉ dùng: " + ", ".join("{%s}" % v for v in sorted(VARIABLES)))
    if re.search(r"\{[^}\w]|[^{\w]\}|\{\w*$", body or "") and not used - VARIABLES:
        errs.append("Dấu { } không đúng — biến phải viết dạng {ten}.")
    return errs


def render(body: str, ctx: dict, strip: bool | None = None) -> str:
    errs = template_errors(body)
    if errs:
        raise ValueError(" ".join(errs))
    out = re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), "")), body)
    out = re.sub(r"\s+", " ", out).replace(" .", ".").replace(" ,", ",").strip()
    if strip is None:
        strip = str(cfg("SMS_STRIP_ACCENTS", "1")).lower() not in ("0", "false", "no", "")
    return strip_accents(out) if strip else out


def appointment_context(name: str, exam_date: date, exam_time: str = "") -> dict:
    t = str(exam_time or "").strip()
    t = t[:5] if re.match(r"^\d{1,2}:\d{2}", t) else ""
    return {"ten": strip_accents(name).upper() if name else "Quy khach", "ngay": exam_date.strftime("%d/%m"),
            "gio": t, "gio_text": f" luc {t}" if t else "", "hotline": str(cfg("SMS_HOTLINE", "")),
            "ten_bv": "BVDK Tam Duc Cau Quan", "dia_diem": ""}


# ══════════════════════════════════════════════════════════════════════════════
# Nhà cung cấp
# ══════════════════════════════════════════════════════════════════════════════
class SendResult(NamedTuple):
    ok: bool
    msg_id: str = ""
    error: str = ""
    retryable: bool = False


class Provider:
    """Lớp cha. Nhà cung cấp thật: kế thừa, viết send(), đăng ký bằng register_provider('ten', lambda: MyProvider())."""
    name = "base"
    real = True

    def send(self, phone: str, text: str, brand: str, ref: str) -> SendResult:       # pragma: no cover
        raise NotImplementedError


class SandboxProvider(Provider):
    """Chế độ THỬ: không gửi gì ra ngoài. Số kết thúc 9999 → lỗi vĩnh viễn, 8888 → lỗi tạm (thử lại được)."""
    name = "sandbox"
    real = False
    calls: list = []

    def send(self, phone, text, brand, ref):
        SandboxProvider.calls.append((phone, text, brand, ref))
        if phone.endswith("9999"):
            return SendResult(False, error="sandbox: số bị từ chối")
        if phone.endswith("8888") and not ref.endswith("#retry"):
            return SendResult(False, error="sandbox: lỗi tạm", retryable=True)
        return SendResult(True, msg_id=f"sandbox-{ref}")


_PROVIDERS = {"sandbox": lambda: SandboxProvider()}


def register_provider(name: str, factory):
    _PROVIDERS[name.lower()] = factory


def get_provider() -> Provider:
    name = str(cfg("SMS_PROVIDER", "sandbox") or "sandbox").lower()
    if name not in _PROVIDERS:
        raise RuntimeError(f"Chưa có nhà cung cấp SMS «{name}». Hiện hỗ trợ: {', '.join(sorted(_PROVIDERS))}.")
    return _PROVIDERS[name]()


# ══════════════════════════════════════════════════════════════════════════════
# Cài đặt (lưu trong DB, chỉnh trên web)
# ══════════════════════════════════════════════════════════════════════════════
WIZ_PREFIX = "wiz:"                                      # dòng nháp của trang soạn tin (không phải cài đặt thật)
SETTING_DEFAULTS = {"enabled": "0", "auto_reminder": "0", "daily_cap": "500", "tpl_appointment": ""}   # tpl_appointment rỗng = mẫu gốc


def get_settings(c=None) -> dict:
    def _get(cc):
        rows = cc.execute("SELECT key, value FROM sms_settings").fetchall()
        d = dict(SETTING_DEFAULTS); d.update({r["key"]: r["value"] for r in rows if not r["key"].startswith(WIZ_PREFIX)})
        return d
    if c is not None:
        return _get(c)
    with PD._conn() as cc:
        return _get(cc)


def set_setting(key: str, value: str, by: str):
    if key not in SETTING_DEFAULTS:
        raise ValueError("Cài đặt không hợp lệ.")
    if key == "daily_cap" and not (str(value).isdigit() and 0 <= int(value) <= 100000):
        raise ValueError("Hạn mức mỗi ngày phải là số từ 0 đến 100000.")
    if key == "tpl_appointment" and str(value).strip():
        value = str(value).strip()
        errs = template_errors(value)
        if len(value) > 320:
            errs.append("Mẫu tin quá dài (tối đa 320 ký tự).")
        if errs:
            raise ValueError(" ".join(errs))
    now = PD._now()
    with PD._conn() as c:
        if c.execute("SELECT 1 AS x FROM sms_settings WHERE key = %s", (key,)).fetchone():
            c.execute("UPDATE sms_settings SET value = %s, updated_by = %s, updated_at = %s WHERE key = %s", (str(value), by, now, key))
        else:
            c.execute("INSERT INTO sms_settings (key, value, updated_by, updated_at) VALUES (%s, %s, %s, %s)", (key, str(value), by, now))


def wiz_save(form, by: str = "") -> str:
    """Lưu lại dữ liệu 1 bước của trang soạn tin để hiện bằng trang GET (nút Back / F5 của trình duyệt chạy bình thường,
    không báo «Confirm Form Resubmission»). Nháp tự xoá sau 1 ngày."""
    data = {k: form.getlist(k) for k in form.keys() if k != "csrf"}
    wid = uuid.uuid4().hex[:24]
    now = PD._now()
    with PD._conn() as c:
        c.execute("DELETE FROM sms_settings WHERE key LIKE %s AND updated_at < %s", (WIZ_PREFIX + "%", now - timedelta(days=1)))
        c.execute("INSERT INTO sms_settings (key, value, updated_by, updated_at) VALUES (%s, %s, %s, %s)",
                  (WIZ_PREFIX + wid, json.dumps(data, ensure_ascii=False), by, now))
    return wid


def wiz_load(wid: str):
    """→ MultiDict dữ liệu của bước đã lưu, hoặc None nếu mã sai / đã hết hạn."""
    from werkzeug.datastructures import MultiDict
    if not re.fullmatch(r"[0-9a-f]{24}", wid or ""):
        return None
    with PD._conn() as c:
        r = c.execute("SELECT value FROM sms_settings WHERE key = %s", (WIZ_PREFIX + wid,)).fetchone()
    if not r:
        return None
    try:
        data = json.loads(r["value"])
    except ValueError:
        return None
    md = MultiDict()
    for k, vals in data.items():
        for v in (vals if isinstance(vals, list) else [vals]):
            md.add(k, v)
    return md


def appointment_template(c=None) -> str:
    """Mẫu tin nhắc lịch hẹn đang dùng: bản nhân viên đã sửa trên web, chưa sửa thì dùng mẫu gốc."""
    st = get_settings(c)
    t = (st.get("tpl_appointment") or "").strip()
    return t if t and not template_errors(t) else DEFAULT_APPOINTMENT_TEMPLATE


def in_quiet_hours(now: datetime | None = None) -> bool:
    now = now or S.vn_now()
    start, end = str(cfg("SMS_QUIET_START", "22:00")), str(cfg("SMS_QUIET_END", "07:00"))
    hm = now.strftime("%H:%M")
    return (hm >= start or hm < end) if start > end else (start <= hm < end)


# ══════════════════════════════════════════════════════════════════════════════
# Người nhận
# ══════════════════════════════════════════════════════════════════════════════
MOBILE_RE = re.compile(r"0(?:3|5|7|8|9)\d{8}")


def sms_phone(raw) -> str:
    """SĐT di động 10 số hợp lệ ('' nếu không gửi được SMS)."""
    from services import reconcile as R
    p = R.valid_phone(raw)
    return p if MOBILE_RE.fullmatch(p) else ""


def optout_set(c) -> set[str]:
    return {r["phone"] for r in c.execute("SELECT phone FROM sms_optout").fetchall()}


def build_appointment_recipients(df, exam_date: date, c, only_ids: set[int] | None = None):
    """Bệnh nhân có lịch hẹn đúng `exam_date` → (người nhận, bị loại kèm lý do). Mỗi SĐT tối đa 1 tin."""
    import pandas as pd
    eligible, skipped, seen_phone = [], [], set()
    if df is None or df.empty or "_date" not in df.columns:
        return eligible, skipped
    opt = optout_set(c)
    done_rows = c.execute(
        "SELECT m.patient_id, m.phone FROM sms_messages m JOIN sms_campaigns k ON k.id = m.campaign_id "
        "WHERE k.kind = 'appointment' AND m.exam_date = %s AND m.status IN ('queued','sending','sent','delivered','unknown')",
        (exam_date,)).fetchall()
    ids_done = {r["patient_id"] for r in done_rows}                  # lịch hẹn đã có tin nhắc
    phones_done = {r["phone"] for r in done_rows}                    # SĐT đã nhận tin cho ngày này (người nhà dùng chung số)
    day = df[df["_date"] == pd.Timestamp(exam_date)]
    for idx, row in day.iterrows():
        pid = int(idx) + 2
        if only_ids is not None and pid not in only_ids:
            continue
        name = str(row.get(S.COL_NAME, "")).strip()
        rec = {"patient_id": pid, "name": name, "phone_raw": str(row.get(S.COL_PHONE, "")), "exam_date": exam_date,
               "exam_time": str(row.get(S.COL_EXAM_TIME, ""))}
        phone = sms_phone(rec["phone_raw"])
        reason = ""
        if S.status_key(row.get(S.COL_STATUS, "")) == S.status_key(S.STATUS_ATTENDED):
            reason = "đã khám rồi"
        elif pid in ids_done:
            reason = "đã có tin nhắc cho lịch này"
        elif phone and phone in phones_done:
            reason = "SĐT này đã nhận tin nhắc cho ngày đó"
        elif not phone:
            reason = "không có SĐT di động hợp lệ"
        elif phone in opt:
            reason = "đã từ chối nhận tin"
        elif phone in seen_phone:
            reason = "trùng SĐT với người trước (1 tin/số)"
        if reason:
            skipped.append(dict(rec, phone=phone or "", reason=reason))
            continue
        seen_phone.add(phone)
        eligible.append(dict(rec, phone=phone))
    return eligible, skipped


# ══════════════════════════════════════════════════════════════════════════════
# Chiến dịch + hàng đợi
# ══════════════════════════════════════════════════════════════════════════════
def create_campaign(c, *, kind, name, body, eligible, skipped, created_by, key=None, filters=None, mode=None, event_id=None):
    """Tạo chiến dịch + 1 dòng/ người nhận. Trả về (campaign_id, đã_tồn_tại). `key` trùng → không tạo lần 2."""
    if key:
        ex = c.execute("SELECT id FROM sms_campaigns WHERE campaign_key = %s", (key,)).fetchone()
        if ex:
            return ex["id"], True
    errs = template_errors(body)
    if errs:
        raise ValueError(" ".join(errs))
    mode = mode or ("live" if get_provider().real else "sandbox")
    now = PD._now()
    cid = c.execute("INSERT INTO sms_campaigns (kind, name, campaign_key, event_id, body, filters, mode, status, created_by, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s) RETURNING id",
                    (kind, name, key, event_id, body, json.dumps(filters or {}, ensure_ascii=False, default=str), mode, created_by, now)).fetchone()["id"]
    for r in eligible:
        text = render(body, r.get("ctx") or appointment_context(r["name"], r["exam_date"], r.get("exam_time", "")))
        c.execute("INSERT INTO sms_messages (campaign_id, patient_id, contact_id, exam_date, phone, recipient_name, body, segments, status, queued_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s)",
                  (cid, r.get("patient_id"), r.get("contact_id"), r.get("exam_date"), r["phone"], r["name"], text, count_segments(text), now))
    for r in skipped:
        c.execute("INSERT INTO sms_messages (campaign_id, patient_id, contact_id, exam_date, phone, recipient_name, body, segments, status, skip_reason, queued_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, '', 0, 'skipped', %s, %s)",
                  (cid, r.get("patient_id"), r.get("contact_id"), r.get("exam_date"), r.get("phone", ""), r["name"], r["reason"], now))
    if not eligible:
        c.execute("UPDATE sms_campaigns SET status = 'done', finished_at = %s WHERE id = %s", (now, cid))
    return cid, False


def preview_appointments(df, exam_date: date) -> dict:
    """Xem trước người sẽ nhận (không ghi gì)."""
    with PD._conn() as c:
        eligible, skipped = build_appointment_recipients(df, exam_date, c)
        tpl = appointment_template(c)
    reasons: dict[str, int] = {}
    for s in skipped:
        reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    segs = sum(count_segments(render(tpl, appointment_context(r["name"], r["exam_date"], r["exam_time"])))
               for r in eligible) if eligible and cfg("SMS_HOTLINE", "") else None
    return {"date": exam_date, "eligible": eligible, "skipped": skipped, "reasons": reasons, "segments": segs}


def _today_start():
    n = S.vn_now()
    return datetime(n.year, n.month, n.day)


def sent_today(c) -> int:
    return c.execute("SELECT COUNT(*) AS n FROM sms_messages WHERE status IN ('sending','sent','delivered') AND sent_at >= %s",
                     (_today_start(),)).fetchone()["n"]


def _try_lock(c) -> bool:
    if getattr(c, "is_sqlite", False):
        return True
    return bool(c.execute("SELECT pg_try_advisory_lock(%s) AS ok", (727276,)).fetchone()["ok"])


def _unlock(c):
    if not getattr(c, "is_sqlite", False):
        c.execute("SELECT pg_advisory_unlock(%s)", (727276,))


def process_queue(limit: int = 200, now: datetime | None = None) -> dict:
    """Gửi các tin đang chờ. Trả về {sent, failed, requeued, remaining, blocked: lý do | None}."""
    now = now or S.vn_now()
    out = {"sent": 0, "failed": 0, "requeued": 0, "remaining": 0, "blocked": None}
    with PD._conn() as lockc:
        if not _try_lock(lockc):
            out["blocked"] = "Đang có tiến trình gửi khác chạy."
            return out
        try:
            return _process_locked(out, limit, now)
        finally:
            _unlock(lockc)


def _process_locked(out, limit, now):
    stuck_before = PD._now() - timedelta(minutes=STUCK_MINUTES)
    with PD._conn() as c:                                          # tin kẹt ở 'sending' quá lâu: KHÔNG tự gửi lại (có thể đã đi mất)
        c.execute("UPDATE sms_messages SET status = 'unknown', error = 'Không rõ kết quả (tiến trình bị gián đoạn) — kiểm tra với nhà cung cấp trước khi gửi lại' "
                  "WHERE status = 'sending' AND queued_at < %s", (stuck_before,))
        st = get_settings(c)
        queued = c.execute("SELECT COUNT(*) AS n FROM sms_messages WHERE status = 'queued'").fetchone()["n"]
        out["remaining"] = queued
        if not queued:
            _finish_campaigns(c)
            return out
        if st["enabled"] != "1":
            out["blocked"] = "Công tắc gửi SMS đang TẮT."
            return out
        if in_quiet_hours(now):
            out["blocked"] = "Đang trong giờ nghỉ (22:00–07:00) — không gửi."
            return out
        room = int(st["daily_cap"]) - sent_today(c)
        if room <= 0:
            out["blocked"] = f"Đã đạt hạn mức {st['daily_cap']} tin trong ngày."
            return out
        brand = str(cfg("SMS_BRANDNAME", ""))
        provider = get_provider()
        ok_brand, bmsg = validate_brand(brand)
        if provider.real and not ok_brand:
            out["blocked"] = bmsg
            return out
        rows = c.execute("SELECT m.id, m.phone, m.body, m.attempts, k.mode FROM sms_messages m JOIN sms_campaigns k ON k.id = m.campaign_id "
                         "WHERE m.status = 'queued' AND k.status = 'queued' ORDER BY m.id LIMIT %s", (min(limit, room),)).fetchall()
    sandbox = SandboxProvider()
    for r in rows:
        prov = sandbox if r["mode"] == "sandbox" else provider
        if r["mode"] == "live" and not provider.real:               # chiến dịch thật nhưng chưa có nhà cung cấp thật → giữ nguyên hàng đợi
            out["blocked"] = "Chưa cấu hình nhà cung cấp SMS thật (SMS_PROVIDER) — các tin thật vẫn đang chờ."
            continue
        with PD._conn() as c:                                      # giành tin (chống 2 tiến trình cùng gửi)
            n = c.execute("UPDATE sms_messages SET status = 'sending', attempts = attempts + 1 WHERE id = %s AND status = 'queued'",
                          (r["id"],)).rowcount
        if n != 1:
            continue
        ref = f"{r['id']}" + ("#retry" if r["attempts"] >= 1 else "")
        try:
            res = prov.send(r["phone"], r["body"], brand, ref)
        except Exception as e:                                      # không biết đã gửi hay chưa → unknown, KHÔNG tự gửi lại
            res = None
            err = f"{type(e).__name__}: {e}"[:300]
        with PD._conn() as c:
            if res is None:
                c.execute("UPDATE sms_messages SET status = 'unknown', error = %s WHERE id = %s", (err, r["id"]))
                out["failed"] += 1
            elif res.ok:
                c.execute("UPDATE sms_messages SET status = 'sent', provider_msg_id = %s, sent_at = %s, error = '' WHERE id = %s",
                          (res.msg_id, PD._now(), r["id"]))
                out["sent"] += 1
            elif res.retryable and r["attempts"] + 1 < MAX_ATTEMPTS:
                c.execute("UPDATE sms_messages SET status = 'queued', error = %s WHERE id = %s", (res.error[:300], r["id"]))
                out["requeued"] += 1
            else:
                c.execute("UPDATE sms_messages SET status = 'failed', error = %s WHERE id = %s", (res.error[:300], r["id"]))
                out["failed"] += 1
    with PD._conn() as c:
        _finish_campaigns(c)
        out["remaining"] = c.execute("SELECT COUNT(*) AS n FROM sms_messages WHERE status = 'queued'").fetchone()["n"]
    return out


def _finish_campaigns(c):
    for k in c.execute("SELECT id FROM sms_campaigns WHERE status = 'queued'").fetchall():
        left = c.execute("SELECT COUNT(*) AS n FROM sms_messages WHERE campaign_id = %s AND status IN ('queued','sending')", (k["id"],)).fetchone()["n"]
        if left == 0:
            c.execute("UPDATE sms_campaigns SET status = 'done', finished_at = %s WHERE id = %s", (PD._now(), k["id"]))


def retry_failed(campaign_id: int) -> int:
    """Đưa tin 'failed' (không phải 'unknown') về hàng đợi để gửi lại."""
    with PD._conn() as c:
        n = c.execute("UPDATE sms_messages SET status = 'queued', attempts = 0, error = '' WHERE campaign_id = %s AND status = 'failed'",
                      (int(campaign_id),)).rowcount
        if n:
            c.execute("UPDATE sms_campaigns SET status = 'queued', finished_at = NULL WHERE id = %s", (int(campaign_id),))
    return n


# ══════════════════════════════════════════════════════════════════════════════
# Nhắc lịch hẹn TỰ ĐỘNG (lệnh `flask sms-auto-reminders`, đặt cron 18:30)
# ══════════════════════════════════════════════════════════════════════════════
def auto_window_ok(now: datetime | None = None) -> bool:
    """Chỉ chạy tự động trong khung giờ buổi tối (mặc định 17:00–21:30) — chống cron cấu hình nhầm giờ."""
    now = now or S.vn_now()
    lo, hi = str(cfg("SMS_AUTO_WINDOW", "17:00-21:30")).split("-")
    return lo <= now.strftime("%H:%M") <= hi


def run_auto_reminders(df, today: date | None = None, now: datetime | None = None, force: bool = False) -> dict:
    """Tạo chiến dịch nhắc cho lịch hẹn NGÀY MAI (mỗi ngày đúng 1 lần nhờ campaign_key). Chưa gửi — caller gọi process_queue()."""
    now = now or S.vn_now()
    today = today or now.date()
    target = today + timedelta(days=1)
    with PD._conn() as c:
        st = get_settings(c)
        if not force:
            if st["enabled"] != "1" or st["auto_reminder"] != "1":
                return {"status": "off", "target": target, "message": "Nhắc lịch tự động đang TẮT (xem trang Tin nhắn SMS)."}
            if not auto_window_ok(now):
                return {"status": "outside_window", "target": target, "message": "Ngoài khung giờ chạy tự động — bỏ qua."}
        if not str(cfg("SMS_HOTLINE", "")).strip():
            return {"status": "error", "target": target, "message": "Chưa cấu hình SMS_HOTLINE (số điện thoại bệnh viện ghi trong tin)."}
        eligible, skipped = build_appointment_recipients(df, target, c)
        cid, existed = create_campaign(
            c, kind="appointment", name=f"Nhắc lịch hẹn khám ngày {target.strftime('%d/%m/%Y')} (tự động)",
            body=appointment_template(c), eligible=eligible, skipped=skipped, created_by="tu-dong",
            key=f"auto:{target.isoformat()}", filters={"exam_date": target.isoformat(), "auto": True})
    return {"status": "exists" if existed else "created", "campaign_id": cid, "target": target,
            "eligible": 0 if existed else len(eligible), "skipped": 0 if existed else len(skipped)}


# ── Danh sách từ chối ───────────────────────────────────────────────────────────────────────────────────────────
def add_optout(phone_raw: str, reason: str, by: str) -> str:
    phone = sms_phone(phone_raw)
    if not phone:
        raise ValueError("SĐT không hợp lệ (cần số di động 10 số bắt đầu bằng 0).")
    with PD._conn() as c:
        if not c.execute("SELECT 1 AS x FROM sms_optout WHERE phone = %s", (phone,)).fetchone():
            c.execute("INSERT INTO sms_optout (phone, reason, created_by, created_at) VALUES (%s, %s, %s, %s)",
                      (phone, reason.strip()[:200], by, PD._now()))
        n = c.execute("UPDATE sms_messages SET status = 'skipped', skip_reason = 'đã từ chối nhận tin' "
                      "WHERE phone = %s AND status = 'queued'", (phone,)).rowcount     # tin đang chờ của số này bị huỷ ngay
    return phone


def remove_optout(phone: str):
    with PD._conn() as c:
        c.execute("DELETE FROM sms_optout WHERE phone = %s", (phone,))


# ══════════════════════════════════════════════════════════════════════════════
# GIAI ĐOẠN 2–3: trang gửi nhiều bước, khám lưu động, danh bạ có đồng ý, lọc theo vùng
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_MOBILE_TEMPLATE = ("BVDK Tam Duc Cau Quan: Kham luu dong ngay {ngay}{gio_text} tai {dia_diem}. "
                           "Moi {ten} den kham. Lien he {hotline}.")
PRESETS = {"appointment": DEFAULT_APPOINTMENT_TEMPLATE, "mobile": DEFAULT_MOBILE_TEMPLATE}
LIST_CAP = 1000                                   # tối đa số dòng hiển thị để tích chọn (lọc hẹp hơn nếu nhiều hơn)
CONSENT_SOURCES = {"giay": "Phiếu đồng ý (giấy)", "qr": "Quét mã QR / biểu mẫu online",
                   "mieng": "Đồng ý trực tiếp (nhân viên ghi nhận)", "khac": "Hình thức khác"}
AUDIENCES = {"dan": "Người dân", "hocsinh": "Học sinh (phụ huynh)", "congty": "Công ty / đơn vị"}


def max_recipients() -> int:
    try:
        return int(cfg("SMS_MAX_RECIPIENTS", 1000))
    except (TypeError, ValueError):
        return 1000


def price_per_segment() -> int:
    try:
        return int(cfg("SMS_PRICE_PER_SEGMENT", 450))
    except (TypeError, ValueError):
        return 450


def event_context(name: str, ev: dict) -> dict:
    t = str(ev.get("event_time") or "").strip()
    t = t[:5] if re.match(r"^\d{1,2}:\d{2}", t) else t
    loc = str(ev.get("location") or "")
    strip = str(cfg("SMS_STRIP_ACCENTS", "1")).lower() not in ("0", "false", "no", "")
    return {"ten": strip_accents(name).upper() if name else "Quy khach", "ngay": ev["event_date"].strftime("%d/%m"),
            "gio": t, "gio_text": f" luc {t}" if t else "", "dia_diem": strip_accents(loc) if strip else loc,
            "hotline": str(cfg("SMS_HOTLINE", "")), "ten_bv": "BVDK Tam Duc Cau Quan"}


# ── Sự kiện khám lưu động ───────────────────────────────────────────────────────────────────────────────────────
def create_event(name, audience, event_date, event_time, location, note, by) -> int:
    name, location = (name or "").strip(), (location or "").strip()
    if not name:
        raise ValueError("Tên đợt khám không được để trống.")
    if audience not in AUDIENCES:
        raise ValueError("Đối tượng không hợp lệ.")
    if not isinstance(event_date, date):
        raise ValueError("Ngày khám không hợp lệ.")
    if not location:
        raise ValueError("Địa điểm khám không được để trống (dùng để ghi vào tin nhắn).")
    with PD._conn() as c:
        return c.execute("INSERT INTO sms_events (name, audience, event_date, event_time, location, note, created_by, created_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                         (name, audience, event_date, (event_time or "").strip(), location, (note or "").strip(), by, PD._now())).fetchone()["id"]


def get_event(c, event_id) -> dict | None:
    r = c.execute("SELECT * FROM sms_events WHERE id = %s AND deleted_at IS NULL", (int(event_id),)).fetchone() if event_id else None
    if not r:
        return None
    d = PD.dict_row(r)
    d["event_date"] = PD._as_date(d["event_date"])
    return d


def list_events(c) -> list[dict]:
    rows = c.execute("SELECT e.*, (SELECT COUNT(*) FROM sms_contacts x WHERE x.event_id = e.id AND x.deleted_at IS NULL) AS n_contacts, "
                     "(SELECT COUNT(*) FROM sms_campaigns k WHERE k.event_id = e.id) AS n_campaigns "
                     "FROM sms_events e WHERE e.deleted_at IS NULL ORDER BY e.event_date DESC, e.id DESC LIMIT 100").fetchall()
    out = []
    for r in rows:
        d = PD.dict_row(r); d["event_date"] = PD._as_date(d["event_date"]); out.append(d)
    return out


def delete_event(event_id) -> bool:
    """Chỉ xoá (mềm) được khi chưa có chiến dịch nào gửi cho đợt này."""
    with PD._conn() as c:
        if c.execute("SELECT 1 AS x FROM sms_campaigns WHERE event_id = %s", (int(event_id),)).fetchone():
            return False
        n = c.execute("UPDATE sms_events SET deleted_at = %s WHERE id = %s AND deleted_at IS NULL", (PD._now(), int(event_id))).rowcount
        c.execute("UPDATE sms_contacts SET deleted_at = %s WHERE event_id = %s AND deleted_at IS NULL", (PD._now(), int(event_id)))
    return bool(n)


# ── Danh bạ (BẮT BUỘC có đồng ý nhận tin) ───────────────────────────────────────────────────────────────────────
def _contact_row(name, phone_raw, commune_raw="", org=""):
    """1 người trong danh bạ → (dòng hợp lệ | None, lỗi). Dùng chung cho nhập theo ô và dán/CSV."""
    from services import regions as RG
    name, phone_raw, commune_raw, org = (str(x or "").strip() for x in (name, phone_raw, commune_raw, org))
    phone = sms_phone(phone_raw)
    if not phone:
        return None, (f"SĐT không hợp lệ «{phone_raw}» (cần số di động 10 số)" if phone_raw else "thiếu SĐT")
    commune = RG.extract_commune(commune_raw)[0] or (f"Xã {' '.join(w.capitalize() for w in commune_raw.split())}" if commune_raw else "")
    return {"name": name, "phone": phone, "commune": commune, "org": org}, ""


def parse_contact_fields(names, phones, communes, orgs):
    """Danh bạ nhập theo TỪNG Ô (4 cột song song). Dòng trống hoàn toàn thì bỏ qua. Trả về (hợp lệ, lỗi theo dòng)."""
    rows, errors = [], []
    n = max(len(names), len(phones), len(communes), len(orgs))
    get = lambda lst, i: (lst[i] if i < len(lst) else "")
    for i in range(n):
        cells = [get(names, i), get(phones, i), get(communes, i), get(orgs, i)]
        if not any(str(x).strip() for x in cells):
            continue
        row, err = _contact_row(*cells)
        if err:
            who = str(cells[0]).strip() or f"dòng {i + 1}"
            errors.append(f"{who}: {err}")
        else:
            rows.append(row)
    return rows, errors


def parse_contacts_text(text: str):
    """Dán nhiều dòng / file CSV: Họ tên, SĐT, Xã/Thôn, Đơn vị (ngăn bằng Tab, ; hoặc ,). Trả về (hợp lệ, lỗi theo dòng)."""
    rows, errors = [], []
    lines = [l for l in (text or "").replace("\r", "").split("\n")]
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        sep = "\t" if "\t" in line else (";" if ";" in line else ",")
        cols = [x.strip() for x in line.split(sep)]
        if len(cols) < 2:
            errors.append(f"Dòng {n}: thiếu SĐT («{line.strip()[:40]}»)")
            continue
        if not re.search(r"\d{9}", re.sub(r"\D", "", cols[1])) and n == 1:
            continue                                                     # dòng tiêu đề (Họ tên, SĐT, …)
        row, err = _contact_row(cols[0], cols[1], cols[2] if len(cols) > 2 else "", cols[3] if len(cols) > 3 else "")
        if err:
            errors.append(f"Dòng {n}: {err}")
            continue
        rows.append(row)
    return rows, errors


def add_contacts(event_id: int, rows: list[dict], consent_source: str, by: str) -> dict:
    if consent_source not in CONSENT_SOURCES:
        raise ValueError("Phải chọn hình thức đồng ý nhận tin — không nhập danh bạ khi chưa có đồng ý.")
    added = dup = 0
    now = PD._now()
    with PD._conn() as c:
        if get_event(c, event_id) is None:
            raise ValueError("Không tìm thấy đợt khám.")
        have = {r["phone"] for r in c.execute("SELECT phone FROM sms_contacts WHERE event_id = %s AND deleted_at IS NULL", (int(event_id),)).fetchall()}
        for r in rows:
            if r["phone"] in have:
                dup += 1
                continue
            have.add(r["phone"])
            c.execute("INSERT INTO sms_contacts (event_id, name, phone, commune, org, consent_source, consent_at, created_by, created_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                      (int(event_id), r["name"], r["phone"], r.get("commune", ""), r.get("org", ""), consent_source, now, by, now))
            added += 1
    return {"added": added, "duplicate": dup}


def delete_contact(contact_id: int):
    with PD._conn() as c:
        c.execute("UPDATE sms_contacts SET deleted_at = %s WHERE id = %s AND deleted_at IS NULL", (PD._now(), int(contact_id)))


# ── Nhóm vùng đã lưu (vd. «Bắc Nông Cống») ──────────────────────────────────────────────────────────────────────
def list_regions(c) -> list[dict]:
    out = []
    for r in c.execute("SELECT * FROM sms_regions ORDER BY name").fetchall():
        try:
            out.append({"id": r["id"], "name": r["name"], "communes": json.loads(r["communes"] or "[]")})
        except ValueError:
            continue
    return out


def save_region(name: str, communes: list[str], by: str):
    name = (name or "").strip()
    if not name or not communes:
        raise ValueError("Cần đặt tên nhóm và chọn ít nhất 1 xã/phường.")
    with PD._conn() as c:
        js = json.dumps(sorted(set(communes)), ensure_ascii=False)
        if c.execute("SELECT 1 AS x FROM sms_regions WHERE name = %s", (name,)).fetchone():
            c.execute("UPDATE sms_regions SET communes = %s WHERE name = %s", (js, name))
        else:
            c.execute("INSERT INTO sms_regions (name, communes, created_by, created_at) VALUES (%s, %s, %s, %s)", (name, js, by, PD._now()))


def delete_region(region_id: int):
    with PD._conn() as c:
        c.execute("DELETE FROM sms_regions WHERE id = %s", (int(region_id),))


# ── Đối tượng nhận tin: bệnh nhân / danh bạ, có lọc ─────────────────────────────────────────────────────────────
def _with_commune(df):
    """Bảo đảm df có cột XÃ/PHƯỜNG (chế độ Sheet chưa có thì tự tách từ địa chỉ)."""
    import pandas as pd
    from services import regions as RG
    if df is None or df.empty or PD.COMMUNE_HEADER in df.columns:
        return df
    addr = PD.ADDRESS_HEADER if PD.ADDRESS_HEADER in df.columns else (S.COL_ADDRESS if S.COL_ADDRESS in df.columns else None)
    df = df.copy()
    df[PD.COMMUNE_HEADER] = [RG.extract_commune(a)[0] for a in df[addr]] if addr else ""
    return df


def patient_rows(df, f: dict, kind: str) -> tuple[list[dict], bool]:
    """Lọc bệnh nhân theo ngày khám / xã / khoa / nguồn / trạng thái / từ khoá → (dòng, bị cắt bớt?)."""
    import pandas as pd
    from services import regions as RG
    if df is None or df.empty or "_date" not in df.columns:
        return [], False
    df = _with_commune(df)
    d = df
    if f.get("date_from"):
        d = d[d["_date"] >= pd.Timestamp(f["date_from"])]
    if f.get("date_to"):
        d = d[d["_date"] < pd.Timestamp(f["date_to"]) + pd.Timedelta(days=1)]
    d = d[d["_date"].notna()]
    if f.get("status", "chua") == "chua":
        d = d[d[S.COL_STATUS].map(S.status_key) != S.status_key(S.STATUS_ATTENDED)]
    if f.get("communes"):
        want = {RG.commune_key(x if x != RG.UNKNOWN else "") for x in f["communes"]}
        d = d[d[PD.COMMUNE_HEADER].map(RG.commune_key).isin(want)]
    if f.get("khoa"):
        k = RG.commune_key(f["khoa"])
        d = d[d[S.COL_KHOA].map(RG.commune_key).str.contains(re.escape(k), na=False)]
    if f.get("source") == "tai_kham":
        d = d[d[S.COL_SOURCE].astype(str).str.contains(S._TK_RE, case=False, na=False)]
    elif f.get("source") == "khac":
        d = d[~d[S.COL_SOURCE].astype(str).str.contains(S._TK_RE, case=False, na=False)]
    if f.get("q"):
        q = RG.commune_key(f["q"])
        hay = (d[S.COL_NAME].map(RG.commune_key) + " " + d[S.COL_PHONE].astype(str))
        d = d[hay.str.contains(re.escape(q), na=False)]
    if kind == "mobile":                                                 # không hẹn theo ngày: mỗi SĐT chỉ giữ lần đăng ký gần nhất
        d = d.sort_values("_date", ascending=False)
        ph = d[S.COL_PHONE].map(sms_phone)
        d = d[(ph == "") | ~ph.duplicated(keep="first")]
    else:
        d = d.sort_values(["_date", S.COL_NAME], kind="stable")
    truncated = len(d) > LIST_CAP
    rows = []
    for idx, r in d.head(LIST_CAP).iterrows():
        pid = int(idx) + 2
        rows.append({"key": f"p:{pid}", "patient_id": pid, "name": str(r[S.COL_NAME]).strip(), "phone_raw": str(r[S.COL_PHONE]),
                     "exam_date": r["_date"].date(), "exam_time": str(r.get(S.COL_EXAM_TIME, "")), "khoa": str(r.get(S.COL_KHOA, "")),
                     "source": str(r.get(S.COL_SOURCE, "")), "status": str(r.get(S.COL_STATUS, "")),
                     "commune": str(r.get(PD.COMMUNE_HEADER, "")) or RG.UNKNOWN})
    return rows, truncated


def contact_rows(c, event_id: int, f: dict) -> tuple[list[dict], bool]:
    from services import regions as RG
    rows = []
    for r in c.execute("SELECT * FROM sms_contacts WHERE event_id = %s AND deleted_at IS NULL ORDER BY commune, org, name LIMIT %s",
                       (int(event_id), LIST_CAP + 1)).fetchall():
        com = r["commune"] or RG.UNKNOWN
        if f.get("communes") and com not in f["communes"]:
            continue
        if f.get("q") and RG.commune_key(f["q"]) not in RG.commune_key(f"{r['name']} {r['phone']} {r['org']}"):
            continue
        rows.append({"key": f"c:{r['id']}", "contact_id": r["id"], "name": r["name"], "phone_raw": r["phone"], "exam_date": None,
                     "exam_time": "", "khoa": "", "source": "", "status": "", "commune": com, "org": r["org"]})
    return rows[:LIST_CAP], len(rows) > LIST_CAP


def classify(rows: list[dict], c, kind: str, event_id: int | None = None) -> list[dict]:
    """Gắn cho từng dòng: ok (có gửi được không), reason (vì sao không), phone (đã chuẩn hoá). Cùng 1 hàm cho danh sách tích chọn VÀ lúc gửi."""
    opt = optout_set(c)
    done_ids, done_phones = set(), set()
    if kind == "appointment":
        for r in c.execute("SELECT m.patient_id, m.phone, m.exam_date FROM sms_messages m JOIN sms_campaigns k ON k.id = m.campaign_id "
                           "WHERE k.kind = 'appointment' AND m.status IN ('queued','sending','sent','delivered','unknown')").fetchall():
            d = PD._as_date(r["exam_date"])
            done_ids.add((r["patient_id"], d)); done_phones.add((r["phone"], d))
    elif event_id:
        for r in c.execute("SELECT m.phone FROM sms_messages m JOIN sms_campaigns k ON k.id = m.campaign_id "
                           "WHERE k.kind = 'mobile' AND k.event_id = %s AND m.status IN ('queued','sending','sent','delivered','unknown')",
                           (int(event_id),)).fetchall():
            done_phones.add((r["phone"], None))
    seen, out = set(), []
    for r in rows:
        phone = sms_phone(r.get("phone_raw", ""))
        pk = (phone, r.get("exam_date") if kind == "appointment" else None)
        reason = ""
        if kind == "appointment" and S.status_key(r.get("status", "")) == S.status_key(S.STATUS_ATTENDED):
            reason = "đã khám rồi"
        elif kind == "appointment" and (r.get("patient_id"), r.get("exam_date")) in done_ids:
            reason = "đã có tin nhắc cho lịch này"
        elif phone and pk in done_phones:
            reason = "SĐT này đã nhận tin nhắc cho ngày đó" if kind == "appointment" else "SĐT này đã nhận tin của đợt khám này"
        elif not phone:
            reason = "không có SĐT di động hợp lệ"
        elif phone in opt:
            reason = "đã từ chối nhận tin"
        elif pk in seen:
            reason = "trùng SĐT với người trước (1 tin/số)"
        if not reason:
            seen.add(pk)
        out.append(dict(r, phone=phone, ok=not reason, reason=reason))
    return out


def _sources(source) -> list[str]:
    """Nhóm đối tượng: 'contacts' (danh bạ của đợt) và/hoặc 'patients' (bệnh nhân cũ theo vùng). Nhận chuỗi hoặc danh sách."""
    items = [source] if isinstance(source, str) else list(source or [])
    out = [x for x in ("contacts", "patients") if x in items]          # danh bạ đứng trước: trùng SĐT thì ưu tiên người trong danh bạ
    return out or ["patients"]


def audience(df, c, kind: str, source, event: dict | None, f: dict) -> dict:
    """Danh sách để hiện ở bước 2: các dòng đã gắn ok/reason + danh sách xã để lọc. `source` có thể chọn CẢ 2 nhóm."""
    from services import regions as RG
    base, trunc, all_comm = [], False, []
    for src in _sources(source):
        if src == "contacts":
            b, t = contact_rows(c, event["id"], f) if event else ([], False)
            all_comm += [r["commune"] for r in (contact_rows(c, event["id"], {})[0] if event else [])]
        else:
            b, t = patient_rows(df, dict(f, communes=f.get("communes")), kind)
            all_comm += [r["commune"] for r in patient_rows(df, dict(f, communes=[], q="", khoa="", source=""), kind)[0]]
        base += b
        trunc = trunc or t
    rows = classify(base, c, kind, event["id"] if event else None)
    return {"rows": rows, "truncated": trunc, "communes": RG.commune_options(all_comm)}


def resolve_selection(df, c, kind: str, source, event: dict | None, selected: list[str]):
    """Từ danh sách khoá đã tích (p:12, c:5) dựng lại người nhận bằng DỮ LIỆU MỚI NHẤT → (gửi được, bị loại)."""
    keys = [k for k in dict.fromkeys(selected) if re.fullmatch(r"[pc]:\d+", k)]
    rows, srcs = [], _sources(source)
    if "contacts" in srcs:
        ids = [int(k[2:]) for k in keys if k[0] == "c"]
        for r in (contact_rows(c, event["id"], {})[0] if event else []):
            if r["contact_id"] in ids:
                rows.append(r)
    if "patients" in srcs:
        import pandas as pd
        df = _with_commune(df)
        for k in keys:
            if k[0] != "p":
                continue
            idx = int(k[2:]) - 2
            if df is None or idx not in df.index:
                continue
            r = df.loc[idx]
            if pd.isna(r.get("_date")):
                continue
            rows.append({"key": k, "patient_id": int(k[2:]), "name": str(r[S.COL_NAME]).strip(), "phone_raw": str(r[S.COL_PHONE]),
                         "exam_date": r["_date"].date(), "exam_time": str(r.get(S.COL_EXAM_TIME, "")), "khoa": str(r.get(S.COL_KHOA, "")),
                         "source": str(r.get(S.COL_SOURCE, "")), "status": str(r.get(S.COL_STATUS, "")), "commune": str(r.get(PD.COMMUNE_HEADER, ""))})
    done = classify(rows, c, kind, event["id"] if event else None)
    for r in done:
        if kind == "mobile":
            r["exam_date"] = event["event_date"] if event else None
            r["ctx"] = event_context(r["name"], event) if event else {}
        else:
            r["ctx"] = appointment_context(r["name"], r["exam_date"], r.get("exam_time", ""))
    return [r for r in done if r["ok"]], [r for r in done if not r["ok"]]


def preview_messages(body: str, eligible: list[dict], n: int = 3) -> dict:
    """Số tin tính phí, tiền ước tính, vài tin mẫu — lỗi nội dung trả về trong `error`."""
    errs = template_errors(body)
    if errs:
        return {"error": " ".join(errs), "samples": [], "segments": 0, "cost": 0, "long": 0}
    segs, samples, long_ = 0, [], 0
    for r in eligible:
        text = render(body, r["ctx"])
        k = count_segments(text); segs += k; long_ += 1 if k > 1 else 0
        if len(samples) < n:
            samples.append({"to": r["name"], "text": text, "segments": k})
    return {"error": "", "samples": samples, "segments": segs, "cost": segs * price_per_segment(), "long": long_}
