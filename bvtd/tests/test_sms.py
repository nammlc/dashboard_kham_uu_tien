"""SMS nhắc lịch: nội dung tin, người nhận, hàng đợi, lớp an toàn, nhắc tự động 18:30, từ chối nhận tin, quyền admin.
Dùng pool SQLite giả + nhà cung cấp sandbox → không cần PostgreSQL và không gửi tin thật."""
import datetime as dt
import time
import unittest
from unittest import mock

import pandas as pd

from tests.helpers import A, AppTestCase
from services import patient_db as PD, sheets as S, sms as SMS
from tests.sqlite_pool import Pool

TOMORROW = dt.date(2026, 11, 18)
NOW_EVENING = dt.datetime(2026, 11, 17, 18, 30, 0)
NOW_NIGHT = dt.datetime(2026, 11, 17, 23, 0, 0)


def make_df(rows):
    df = pd.DataFrame([{S.COL_NAME: n, S.COL_PHONE: ph, S.COL_STATUS: st, S.COL_EXAM_TIME: t, "NGÀY KHÁM": d.strftime("%d/%m/%Y")}
                       for n, ph, st, t, d in rows])
    df["_date"] = pd.to_datetime(df["NGÀY KHÁM"], format="%d/%m/%Y")
    return df


ROWS = [
    ("Nguyễn Văn An", "0912345678", "", "08:30:00", TOMORROW),            # nhận tin
    ("Trần Thị Bình", "0987654321", "", "", TOMORROW),                    # nhận tin, không có giờ
    ("Lê Văn Cường", "0912345678", "", "09:00", TOMORROW),                # trùng SĐT với An → loại
    ("Phạm Thị Dung", "N/A", "", "", TOMORROW),                           # không có SĐT
    ("Hoàng Văn Em", "02373851234", "", "", TOMORROW),                    # số bàn 11 số → không nhắn SMS được
    ("Vũ Thị Giang", "0911222333", S.STATUS_ATTENDED, "", TOMORROW),     # đã khám
    ("Đỗ Văn Hải", "0933444555", "", "", dt.date(2026, 11, 20)),          # ngày khác
    ("Ngô Thị Lan", "0944555666", "", "", TOMORROW),                      # sẽ bị từ chối nhận tin
]


class Base(unittest.TestCase):
    def setUp(self):
        PD._TEST_POOL = Pool()
        SMS.SandboxProvider.calls = []
        self.cfgp = mock.patch.dict("os.environ", {"SMS_HOTLINE": "02373 123 456", "SMS_BRANDNAME": "BvdkTamDuc", "SMS_PROVIDER": "sandbox",
                                                   "SMS_ADMIN_EMAILS": "t@x.vn, boss@x.vn"})
        self.cfgp.start()
        self.addCleanup(self.cfgp.stop)
        for p in (mock.patch.object(S, "vn_now", lambda: NOW_EVENING), mock.patch.object(PD, "_now", lambda: NOW_EVENING)):
            p.start(); self.addCleanup(p.stop)
        self.addCleanup(setattr, PD, "_TEST_POOL", None)

    def enable(self, cap="500"):
        SMS.set_setting("enabled", "1", "t"); SMS.set_setting("daily_cap", cap, "t")

    def msgs(self, **where):
        q = "SELECT * FROM sms_messages" + ("".join(f" {'WHERE' if i == 0 else 'AND'} {k} = ?" for i, k in enumerate(where)))
        return [dict(r) for r in PD._TEST_POOL.raw.execute(q + " ORDER BY id", tuple(where.values())).fetchall()]


class Content(Base):
    def test_segments_and_accents(self):
        self.assertEqual(SMS.strip_accents("Đỗ Khắc Triều"), "Do Khac Trieu")
        self.assertEqual((SMS.count_segments("a" * 160), SMS.count_segments("a" * 161)), (1, 2))
        self.assertEqual((SMS.count_segments("ă" * 70), SMS.count_segments("ă" * 71)), (1, 2))          # có dấu: 70 ký tự/tin

    def test_default_template_and_variables(self):
        t = SMS.render(SMS.DEFAULT_APPOINTMENT_TEMPLATE, SMS.appointment_context("Nguyễn Văn An", TOMORROW, "08:30:00"))
        self.assertEqual(t, "BVDK Tam Duc Cau Quan nhac: NGUYEN VAN AN co lich kham ngay 18/11 luc 08:30. Can doi lich xin goi 02373 123 456.")
        self.assertLessEqual(len(t), 160); self.assertTrue(t.isascii())
        t2 = SMS.render(SMS.DEFAULT_APPOINTMENT_TEMPLATE, SMS.appointment_context("An", TOMORROW, ""))
        self.assertNotIn("luc", t2)                                                                      # không có giờ → bỏ cụm "luc …"
        with self.assertRaises(ValueError):
            SMS.render("Xin chao {ho_ten}", {})
        self.assertTrue(SMS.template_errors("Xin chao {ten"))

    def test_brandname_rule(self):
        self.assertFalse(SMS.validate_brand("BvdkTamDucCQ")[0])                                          # 12 ký tự: quá dài
        self.assertTrue(SMS.validate_brand("BvdkTamDuc")[0])
        self.assertFalse(SMS.validate_brand("Bvdk TamDuc")[0]); self.assertFalse(SMS.validate_brand("BệnhViện")[0])

    def test_admin_list_default_is_nobody(self):
        self.assertTrue(SMS.is_sms_admin("T@X.vn")); self.assertFalse(SMS.is_sms_admin("lt@x.vn"))
        with mock.patch.dict("os.environ", {"SMS_ADMIN_EMAILS": ""}):
            self.assertFalse(SMS.is_sms_admin("t@x.vn"))


class Recipients(Base):
    def test_eligibility_and_reasons(self):
        df = make_df(ROWS)
        SMS.add_optout("0944555666", "gọi báo không nhận", "t")
        with PD._conn() as c:
            ok, skipped = SMS.build_appointment_recipients(df, TOMORROW, c)
        self.assertEqual([r["name"] for r in ok], ["Nguyễn Văn An", "Trần Thị Bình"])
        why = {r["name"]: r["reason"] for r in skipped}
        self.assertIn("trùng SĐT", why["Lê Văn Cường"]); self.assertIn("không có SĐT", why["Phạm Thị Dung"])
        self.assertIn("không có SĐT di động", why["Hoàng Văn Em"]); self.assertEqual(why["Vũ Thị Giang"], "đã khám rồi")
        self.assertEqual(why["Ngô Thị Lan"], "đã từ chối nhận tin")
        self.assertNotIn("Đỗ Văn Hải", why)                                                              # ngày khác: không xét


class Queue(Base):
    def campaign(self, rows=ROWS):
        df = make_df(rows)
        with PD._conn() as c:
            ok, sk = SMS.build_appointment_recipients(df, TOMORROW, c)
            return SMS.create_campaign(c, kind="appointment", name="t", body=SMS.DEFAULT_APPOINTMENT_TEMPLATE, eligible=ok, skipped=sk, created_by="t")[0]

    def test_switch_is_off_by_default(self):
        self.campaign()
        r = SMS.process_queue()
        self.assertIn("TẮT", r["blocked"]); self.assertEqual(SMS.SandboxProvider.calls, [])

    def test_sends_in_sandbox_and_never_outside(self):
        cid = self.campaign(); self.enable()
        r = SMS.process_queue()
        self.assertEqual((r["sent"], r["failed"], r["blocked"]), (3, 0, None))      # An, Bình, Lan
        self.assertEqual(len(SMS.SandboxProvider.calls), 3)
        m = self.msgs(status="sent"); self.assertTrue(all(x["provider_msg_id"].startswith("sandbox-") for x in m))
        self.assertEqual(PD._TEST_POOL.raw.execute("SELECT status, mode FROM sms_campaigns").fetchone()[:], ("done", "sandbox"))

    def test_quiet_hours_block_sending(self):
        self.campaign(); self.enable()
        r = SMS.process_queue(now=NOW_NIGHT)
        self.assertIn("giờ nghỉ", r["blocked"]); self.assertEqual(SMS.SandboxProvider.calls, [])

    def test_daily_cap(self):
        self.campaign(); self.enable(cap="1")
        r = SMS.process_queue()
        self.assertEqual(r["sent"], 1)
        self.assertIn("hạn mức", SMS.process_queue()["blocked"])

    def test_temporary_error_is_retried_permanent_is_not(self):
        rows = [("An", "0912348888", "", "", TOMORROW), ("Bình", "0912349999", "", "", TOMORROW), ("Cường", "0912340001", "", "", TOMORROW)]
        self.campaign(rows); self.enable()
        r1 = SMS.process_queue(); self.assertEqual((r1["sent"], r1["failed"], r1["requeued"]), (1, 1, 1))
        r2 = SMS.process_queue(); self.assertEqual((r2["sent"], r2["failed"]), (1, 0))                   # lỗi tạm → thử lại → thành công
        by = {m["phone"]: m for m in self.msgs()}
        self.assertEqual((by["0912348888"]["status"], by["0912348888"]["attempts"]), ("sent", 2))
        self.assertEqual(by["0912349999"]["status"], "failed")

    def test_unknown_result_is_never_resent_automatically(self):
        class Boom(SMS.Provider):
            name = "boom"; real = True
            def send(self, *a): raise TimeoutError("mất kết nối giữa chừng")
        SMS.register_provider("boom", lambda: Boom())
        cid = self.campaign()
        with mock.patch.dict("os.environ", {"SMS_PROVIDER": "boom"}):
            PD._TEST_POOL.raw.execute("UPDATE sms_campaigns SET mode = 'live'"); self.enable()
            r = SMS.process_queue()
        self.assertEqual(r["failed"], 3)
        self.assertEqual({m["status"] for m in self.msgs() if m["status"] != "skipped"}, {"unknown"})
        self.assertEqual(SMS.retry_failed(cid), 0)                                                     # 'unknown' KHÔNG nằm trong "gửi lại"

    def test_live_campaign_waits_when_no_real_provider(self):
        self.campaign(); self.enable()
        PD._TEST_POOL.raw.execute("UPDATE sms_campaigns SET mode = 'live'")                            # chiến dịch thật, nhưng SMS_PROVIDER=sandbox
        r = SMS.process_queue()
        self.assertIn("nhà cung cấp", r["blocked"]); self.assertEqual(r["sent"], 0)
        self.assertEqual(len(self.msgs(status="queued")), 3)                                           # vẫn chờ, không bị đánh dấu "đã gửi" oan

    def test_stuck_sending_becomes_unknown(self):
        self.campaign(); self.enable()
        PD._TEST_POOL.raw.execute("UPDATE sms_messages SET status = 'sending', queued_at = '2026-11-17 10:00:00' WHERE status = 'queued'")
        SMS.process_queue()
        self.assertEqual(len(self.msgs(status="unknown")), 3)

    def test_optout_cancels_waiting_messages(self):
        self.campaign()
        SMS.add_optout("0912345678", "x", "t")
        self.assertEqual([m["skip_reason"] for m in self.msgs(phone="0912345678", status="skipped")][:1], ["đã từ chối nhận tin"])


class Auto(Base):
    def test_auto_runs_once_per_day(self):
        df = make_df(ROWS)
        off = SMS.run_auto_reminders(df, now=NOW_EVENING); self.assertEqual(off["status"], "off")           # mặc định tắt
        SMS.set_setting("enabled", "1", "t"); SMS.set_setting("auto_reminder", "1", "t")
        r = SMS.run_auto_reminders(df, now=NOW_EVENING)
        self.assertEqual((r["status"], r["target"], r["eligible"]), ("created", TOMORROW, 3))             # An, Bình, Lan (chưa từ chối)
        again = SMS.run_auto_reminders(df, now=NOW_EVENING)
        self.assertEqual(again["status"], "exists")
        self.assertEqual(len(self.msgs(status="queued")), 3)                                              # không tạo thêm tin
        # chiến dịch tay cho cùng ngày: người đã có tin nhắc bị loại, không gửi đôi
        with PD._conn() as c:
            ok, sk = SMS.build_appointment_recipients(df, TOMORROW, c)
        self.assertEqual(ok, [])
        why = {s["name"]: s["reason"] for s in sk}
        self.assertIn("đã có tin nhắc", why["Nguyễn Văn An"])
        self.assertIn("đã nhận tin nhắc", why["Lê Văn Cường"])                                            # người nhà chung số: không gửi lần 2

    def test_outside_evening_window_is_ignored_unless_forced(self):
        df = make_df(ROWS); SMS.set_setting("enabled", "1", "t"); SMS.set_setting("auto_reminder", "1", "t")
        morning = dt.datetime(2026, 11, 17, 6, 0)
        self.assertEqual(SMS.run_auto_reminders(df, now=morning)["status"], "outside_window")
        self.assertEqual(SMS.run_auto_reminders(df, now=morning, force=True)["status"], "created")

    def test_needs_hotline(self):
        with mock.patch.dict("os.environ", {"SMS_HOTLINE": ""}):
            self.assertEqual(SMS.run_auto_reminders(make_df(ROWS), force=True)["status"], "error")


class Pages(AppTestCase, Base):
    def setUp(self):
        AppTestCase.setUp(self)
        Base.setUp(self)
        A.app.config.update(SMS_HOTLINE="02373 123 456", SMS_BRANDNAME="BvdkTamDuc", SMS_PROVIDER="sandbox", SMS_ADMIN_EMAILS="t@x.vn")
        with self.client.session_transaction() as s:
            s["csrf"] = "tok"; s["last_active"] = time.time()
        p = mock.patch.object(A, "_sms_kick_queue", lambda: SMS.process_queue()); p.start(); self.addCleanup(p.stop)

    def post(self, url, **data):
        return self.client.post(url, data=dict(data, csrf="tok"), follow_redirects=True)

    def test_only_listed_admins_can_open_sms_pages(self):
        A.app.config["SMS_ADMIN_EMAILS"] = "someone-else@x.vn"
        for url in ("/sms", "/sms/tu-choi", "/sms/chien-dich/1"):
            r = self.client.get(url, follow_redirects=False)
            self.assertEqual(r.status_code, 302, url)
        self.assertEqual(self.post("/sms/cai-dat", enabled="1").status_code, 200)               # POST của người không có quyền cũng bị chặn
        self.assertEqual(SMS.get_settings()["enabled"], "0")

    def test_overview_page_and_settings_guard(self):
        r = self.client.get("/sms"); html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200); self.assertIn("Nhắc lịch ngày mai", html); self.assertIn("BVDK Tam Duc Cau Quan nhac", html)
        A.app.config["SMS_HOTLINE"] = ""
        self.assertIn("SMS_HOTLINE", self.post("/sms/cai-dat", enabled="1", daily_cap="100").get_data(as_text=True))
        self.assertEqual(SMS.get_settings()["enabled"], "0")                                      # thiếu hotline → không cho bật
        A.app.config["SMS_HOTLINE"] = "02373 123 456"
        self.post("/sms/cai-dat", enabled="1", auto_reminder="1", daily_cap="120")
        st = SMS.get_settings(); self.assertEqual((st["enabled"], st["auto_reminder"], st["daily_cap"]), ("1", "1", "120"))
        self.assertIn("Hạn mức", self.post("/sms/cai-dat", enabled="1", daily_cap="abc").get_data(as_text=True))

    def test_send_test_and_optout_pages(self):
        self.post("/sms/cai-dat", enabled="1", daily_cap="50")
        r = self.post("/sms/gui-thu", phone="0912345678"); html = r.get_data(as_text=True)
        self.assertIn("Đã gửi thử", html); self.assertIn("BVDK Tam Duc Cau Quan nhac", html)
        self.assertEqual(len(SMS.SandboxProvider.calls), 1)
        self.assertIn("không hợp lệ", self.post("/sms/gui-thu", phone="123").get_data(as_text=True))
        self.post("/sms/tu-choi", phone="0911222333", reason="gọi điện")
        self.assertIn("0911222333", self.client.get("/sms/tu-choi").get_data(as_text=True))
        self.post("/sms/tu-choi", remove="0911222333")
        self.assertNotIn("0911222333", self.client.get("/sms/tu-choi").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
