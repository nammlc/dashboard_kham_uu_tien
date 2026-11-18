"""SMS giai đoạn 2–3: trang gửi nhiều bước, lọc theo ngày/xã, tích chọn từng người, đợt khám lưu động, danh bạ có đồng ý, nhóm vùng."""
import datetime as dt
import re
import time
import unittest
from unittest import mock

import pandas as pd

from tests.helpers import A, AppTestCase
from services import patient_db as PD, regions as RG, sheets as S, sms as SMS
from tests.sqlite_pool import Pool
from tests.test_sms import Base, NOW_EVENING, TOMORROW

ADDR = PD.ADDRESS_HEADER


def make_df(rows):
    """rows: (tên, SĐT, trạng thái, giờ, ngày, địa chỉ, khoa, nguồn)"""
    df = pd.DataFrame([{S.COL_NAME: n, S.COL_PHONE: ph, S.COL_STATUS: st, S.COL_EXAM_TIME: t, "NGÀY KHÁM": d.strftime("%d/%m/%Y"),
                        ADDR: a, S.COL_KHOA: k, S.COL_SOURCE: src} for n, ph, st, t, d, a, k, src in rows])
    df["_date"] = pd.to_datetime(df["NGÀY KHÁM"], format="%d/%m/%Y")
    return df


TK = "BỆNH NHÂN ĐIỀU TRỊ NỘI KHOA TÁI KHÁM"
D = TOMORROW
ROWS = [
    ("Nguyễn Văn An",  "0912345678", "", "08:30", D, "Phú Quý Xã Trung Chính, , Tỉnh Thanh Hóa", "KHOA KHÁM BỆNH", TK),   # id 2
    ("Trần Thị Bình",  "0987654321", "", "",      D, "Xã Minh Nghĩa, Huyện Nông Cống", "KHOA NỘI", ""),                      # 3
    ("Lê Văn Cường",   "0911111111", "", "",      D, "Xã Trung Chính, , Tỉnh Thanh Hóa", "KHOA KHÁM BỆNH", ""),              # 4
    ("Phạm Thị Dung",  "N/A",        "", "",      D, "Xã Tế Nông", "KHOA KHÁM BỆNH", ""),                                    # 5
    ("Vũ Thị Giang",   "0922222222", S.STATUS_ATTENDED, "", D, "Xã Trung Chính", "KHOA KHÁM BỆNH", ""),                    # 6
    ("Đỗ Văn Hải",     "0933333333", "", "",      D + dt.timedelta(days=2), "Xã Minh Nghĩa", "KHOA SẢN", ""),             # 7 (ngày khác)
    ("Ngô Thị Lan",    "0944444444", "", "",      D, "Thôn 5", "KHOA KHÁM BỆNH", ""),                                        # 8 (không rõ xã)
]


class Regions(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(RG.extract_commune("Phú Quý Xã Trung Chính, , Tỉnh Thanh Hóa"), ("Xã Trung Chính", "Phú Quý"))
        self.assertEqual(RG.extract_commune("Thôn 3, xã minh nghĩa, Huyện Nông Cống"), ("Xã Minh Nghĩa", "Thôn 3"))
        self.assertEqual(RG.extract_commune("TT Nông Cống")[0], "Thị trấn Nông Cống")
        self.assertEqual(RG.extract_commune("Thôn 5"), ("", "")); self.assertEqual(RG.extract_commune("Xã N/A"), ("", ""))
        self.assertEqual(RG.commune_key("Xã Tế Nông"), RG.commune_key("xa te nong"))


class Audience(Base):
    def rows(self, f, kind="appointment", df=None):
        return SMS.patient_rows(make_df(ROWS) if df is None else df, dict({"status": "chua"}, **f), kind)[0]

    def test_filters(self):
        self.assertEqual(sorted(r["patient_id"] for r in self.rows({"date_from": D, "date_to": D})), [2, 3, 4, 5, 8])  # đã khám + ngày khác: loại
        self.assertEqual(sorted(r["patient_id"] for r in self.rows({"date_from": D, "date_to": D, "communes": ["Xã Trung Chính"]})), [2, 4])
        self.assertEqual(sorted(r["patient_id"] for r in self.rows({"communes": ["xa minh nghia"]})), [3, 7])               # không phân biệt dấu/hoa
        self.assertEqual(sorted(r["patient_id"] for r in self.rows({"communes": [RG.UNKNOWN]})), [8])                       # nhóm «Chưa rõ xã»
        self.assertEqual(sorted(r["patient_id"] for r in self.rows({"khoa": "sản"})), [7])
        self.assertEqual(sorted(r["patient_id"] for r in self.rows({"source": "tai_kham"})), [2])
        self.assertEqual(sorted(r["patient_id"] for r in self.rows({"q": "binh"})), [3])
        self.assertEqual(len(self.rows({"status": "all"})), 7)

    def test_classify_reasons_and_history(self):
        SMS.add_optout("0987654321", "x", "t")
        with PD._conn() as c:
            res = SMS.classify(self.rows({"date_from": D, "date_to": D}), c, "appointment")
        why = {r["patient_id"]: r["reason"] for r in res}
        self.assertEqual((why[2], why[3], why[4]), ("", "đã từ chối nhận tin", ""))
        self.assertEqual(why[5], "không có SĐT di động hợp lệ")

    def test_mobile_keeps_latest_registration_per_phone(self):
        df = make_df([("An cũ", "0912345678", "", "", D - dt.timedelta(days=90), "Xã Trung Chính", "", ""),
                      ("An mới", "0912345678", "", "", D, "Xã Trung Chính", "", ""),
                      ("Bình", "0987654321", "", "", D, "Xã Trung Chính", "", "")])
        names = [r["name"] for r in SMS.patient_rows(df, {"status": "all"}, "mobile")[0]]
        self.assertEqual(sorted(names), ["An mới", "Bình"])


class Contacts(Base):
    def event(self):
        return SMS.create_event("Khám tại xã Trung Chính", "dan", dt.date(2026, 12, 5), "07:30", "Trạm y tế xã Trung Chính", "", "t")

    def test_parse(self):
        rows, errs = SMS.parse_contacts_text("Họ tên, SĐT, Xã\nNguyễn Văn An, 0912 345 678, Trung Chính, Thôn 3\n"
                                             "Trần Thị B\t098 765 4321\tXã Minh Nghĩa\nLê C, 12345\n\nPhạm D;0911222333")
        self.assertEqual([(r["name"], r["phone"], r["commune"]) for r in rows],
                         [("Nguyễn Văn An", "0912345678", "Xã Trung Chính"), ("Trần Thị B", "0987654321", "Xã Minh Nghĩa"), ("Phạm D", "0911222333", "")])
        self.assertEqual(rows[0]["org"], "Thôn 3"); self.assertEqual(len(errs), 1); self.assertIn("không hợp lệ", errs[0])

    def test_consent_is_mandatory_and_duplicates_skipped(self):
        eid = self.event()
        rows, _ = SMS.parse_contacts_text("An, 0912345678\nAn again, 0912345678\nBình, 0987654321")
        with self.assertRaises(ValueError):
            SMS.add_contacts(eid, rows, "", "t")                                                  # không có đồng ý → từ chối
        self.assertEqual(SMS.add_contacts(eid, rows, "giay", "t"), {"added": 2, "duplicate": 1})
        with PD._conn() as c:
            r = c.execute("SELECT consent_source, consent_at FROM sms_contacts LIMIT 1").fetchone()
        self.assertEqual(r["consent_source"], "giay"); self.assertTrue(r["consent_at"])

    def test_event_rules(self):
        with self.assertRaises(ValueError): SMS.create_event("", "dan", dt.date(2026, 12, 5), "", "x", "", "t")
        with self.assertRaises(ValueError): SMS.create_event("a", "dan", dt.date(2026, 12, 5), "", "", "", "t")        # thiếu địa điểm
        eid = self.event()
        with PD._conn() as c:
            ev = SMS.get_event(c, eid)
            res = SMS.classify([{"key": "c:1", "name": "An", "phone_raw": "0912345678", "contact_id": 1}], c, "mobile", eid)
        self.assertTrue(res[0]["ok"])
        self.assertTrue(SMS.delete_event(eid) is True)                                           # chưa gửi gì → xoá được

    def test_resolve_selection_uses_fresh_data(self):
        df = make_df(ROWS)
        with PD._conn() as c:
            ok, sk = SMS.resolve_selection(df, c, "appointment", "patients", None, ["p:2", "p:3", "p:5", "p:6", "p:2", "x:1", "p:999", "p:abc"])
        self.assertEqual([r["patient_id"] for r in ok], [2, 3])                                  # 5 không SĐT, 6 đã khám, trùng/giả mạo bị bỏ
        SMS.add_optout("0912345678", "gọi báo", "t")                                              # có người từ chối SAU khi tích chọn
        with PD._conn() as c:
            ok, sk = SMS.resolve_selection(df, c, "appointment", "patients", None, ["p:2", "p:3"])
        self.assertEqual([r["patient_id"] for r in ok], [3])

    def test_mobile_message_content_and_cost(self):
        ev = {"event_date": dt.date(2026, 12, 5), "event_time": "07:30", "location": "Trạm y tế xã Trung Chính"}
        el = [{"name": "Nguyễn Văn An", "ctx": SMS.event_context("Nguyễn Văn An", ev)}]
        pv = SMS.preview_messages(SMS.DEFAULT_MOBILE_TEMPLATE, el)
        self.assertEqual(pv["samples"][0]["text"], "BVDK Tam Duc Cau Quan: Kham luu dong ngay 05/12 luc 07:30 tai Tram y te xa Trung Chinh. "
                                                   "Moi NGUYEN VAN AN den kham. Lien he 02373 123 456.")
        self.assertEqual((pv["segments"], pv["cost"]), (1, 450))
        self.assertTrue(SMS.preview_messages("Xin chao {sai_ten}", el)["error"])

    def test_region_groups(self):
        with self.assertRaises(ValueError): SMS.save_region("", ["Xã A"], "t")
        SMS.save_region("Bắc Nông Cống", ["Xã B", "Xã A"], "t"); SMS.save_region("Bắc Nông Cống", ["Xã C"], "t")   # lưu lại cùng tên = cập nhật
        with PD._conn() as c:
            rg = SMS.list_regions(c)
        self.assertEqual(rg[0]["communes"], ["Xã C"]); SMS.delete_region(rg[0]["id"])
        with PD._conn() as c: self.assertEqual(SMS.list_regions(c), [])


class Wizard(AppTestCase, Base):
    def setUp(self):
        AppTestCase.setUp(self); Base.setUp(self)
        A.app.config.update(SMS_HOTLINE="02373 123 456", SMS_BRANDNAME="BvdkTamDuc", SMS_PROVIDER="sandbox", SMS_ADMIN_EMAILS="t@x.vn")
        with self.client.session_transaction() as s:
            s["csrf"] = "tok"; s["last_active"] = time.time()
        for p in (mock.patch.object(A, "_sms_kick_queue", lambda: SMS.process_queue()), mock.patch.object(A, "_get_df", lambda fresh=False, max_age=None: make_df(ROWS)),
                  mock.patch.object(S, "vn_today", lambda: NOW_EVENING.date())):
            p.start(); self.addCleanup(p.stop)

    def post(self, url="/sms/gui", **data):
        multi = {k: v for k, v in data.items()}
        return self.client.post(url, data=dict(multi, csrf="tok"), follow_redirects=True)

    def enable(self):
        SMS.set_setting("enabled", "1", "t")

    def ids(self, html):
        return re.findall(r'name="sel" value="([pc]:\d+)" (?:checked)?>', html)       # chỉ các ô CÓ THỂ tích (ô bị khoá có "disabled")

    def test_only_admins(self):
        A.app.config["SMS_ADMIN_EMAILS"] = "other@x.vn"
        for u in ("/sms/gui", "/sms/su-kien"):
            self.assertEqual(self.client.get(u).status_code, 302)

    def test_no_duplicate_form_field_names_between_steps(self):
        h1 = self.client.get("/sms/gui").get_data(as_text=True)
        self.assertEqual(h1.count('name="kind"'), 2)                                              # chỉ 2 nút chọn, không có trường ẩn trùng tên
        h2 = self.post(step="1", kind="appointment", body="").get_data(as_text=True)
        self.assertNotIn('type="hidden" name="step"', h2)                                         # tên nút bấm không bị trường ẩn che mất

    def test_full_flow_appointment(self):
        self.enable()
        h2 = self.post(step="1", kind="appointment", body="").get_data(as_text=True)              # nội dung rỗng → dùng mẫu mặc định
        self.assertEqual(set(self.ids(h2)), {"p:2", "p:3", "p:4", "p:8"})                          # ngày mai, chưa khám, có SĐT; (5 không SĐT bị disable)
        self.assertIn("không có SĐT di động hợp lệ", h2)
        # bỏ tích người số 3 → chỉ gửi 2, 4 và 8
        h3 = self.post(step="2", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], sel=["p:2", "p:4", "p:8"]).get_data(as_text=True)
        self.assertIn("người nhận", h3); self.assertIn("NGUYEN VAN AN", h3); self.assertIn("450", h3.replace(".", "").replace(",", ""))
        token = re.search(r'name="token" value="(\w+)"', h3).group(1)
        base = dict(step="3s", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], sel=["p:2", "p:4", "p:8"], token=token)
        self.assertIn("kiểm tra nội dung", self.post(**base).get_data(as_text=True))               # chưa tích xác nhận → không gửi
        self.assertEqual(self.msgs(), [])
        h = self.post(**dict(base, confirm="1")).get_data(as_text=True)
        self.assertIn("Đã đưa 3 tin vào hàng đợi", h)
        self.assertEqual(sorted(m["phone"] for m in self.msgs(status="sent")), ["0911111111", "0912345678", "0944444444"])
        self.assertEqual(len(SMS.SandboxProvider.calls), 3)
        self.post(**dict(base, confirm="1"))                                                       # bấm gửi lần 2 cùng token: không tạo thêm
        self.assertEqual(len(self.msgs(status="sent")), 3)
        self.assertEqual(PD._TEST_POOL.raw.execute("SELECT COUNT(*) FROM sms_campaigns").fetchone()[0], 1)

    def test_send_blocked_when_switch_off(self):
        h = self.post(step="3s", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], sel=["p:2"], confirm="1", token="abc").get_data(as_text=True)
        self.assertIn("TẮT", h); self.assertEqual(self.msgs(), [])

    def test_filter_by_commune_and_saved_region(self):
        self.post(step="1", kind="appointment", body="")
        h = self.post(step="2f", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], date_from=D.isoformat(), date_to=D.isoformat(),
                      communes=["Xã Trung Chính"], f_status="chua").get_data(as_text=True)
        self.assertEqual(set(self.ids(h)), {"p:2", "p:4"})
        h = self.post(step="2s", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], date_from=D.isoformat(), date_to=D.isoformat(),
                      communes=["Xã Trung Chính", "Xã Minh Nghĩa"], region_name="Bắc Nông Cống").get_data(as_text=True)
        self.assertIn("Bắc Nông Cống (2)", h)
        with PD._conn() as c: self.assertEqual(SMS.list_regions(c)[0]["communes"], ["Xã Minh Nghĩa", "Xã Trung Chính"])

    def test_mobile_flow_with_contacts_and_consent(self):
        self.enable()
        r = self.post("/sms/su-kien", name="Khám tại xã Trung Chính", audience="dan", event_date="2026-12-05", event_time="07:30",
                      location="Trạm y tế xã Trung Chính", note="")
        eid = int(re.search(r"/sms/su-kien/(\d+)", PD._TEST_POOL.raw.execute("SELECT '/sms/su-kien/' || id FROM sms_events").fetchone()[0]).group(1))
        txt = "Nguyễn Văn An, 0912345678, Trung Chính, Thôn 3\nTrần Thị Bình, 0987654321, Minh Nghĩa, Thôn 1\nLê C, 123"
        bad = self.post(f"/sms/su-kien/{eid}", act="add", text=txt, consent_source="").get_data(as_text=True)         # không chọn hình thức đồng ý
        self.assertIn("hình thức đồng ý", bad)
        self.assertEqual(PD._TEST_POOL.raw.execute("SELECT COUNT(*) FROM sms_contacts").fetchone()[0], 0)
        ok = self.post(f"/sms/su-kien/{eid}", act="add", text=txt, consent_source="giay").get_data(as_text=True)
        self.assertIn("Đã thêm 2 người", ok); self.assertIn("1 dòng lỗi", ok)
        h2 = self.post(step="1", kind="mobile", aud="contacts", event_id=str(eid), body="").get_data(as_text=True)
        keys = self.ids(h2); self.assertEqual(len(keys), 2); self.assertTrue(all(k.startswith("c:") for k in keys))
        h2 = self.post(step="2f", kind="mobile", aud="contacts", event_id=str(eid), body=SMS.PRESETS["mobile"], communes=["Xã Trung Chính"]).get_data(as_text=True)
        self.assertEqual(len(self.ids(h2)), 1)
        h3 = self.post(step="2", kind="mobile", aud="contacts", event_id=str(eid), body=SMS.PRESETS["mobile"], sel=keys).get_data(as_text=True)
        token = re.search(r'name="token" value="(\w+)"', h3).group(1)
        self.post(step="3s", kind="mobile", aud="contacts", event_id=str(eid), body=SMS.PRESETS["mobile"], sel=keys, confirm="1", token=token)
        sent = self.msgs(status="sent")
        self.assertEqual(len(sent), 2)
        self.assertIn("tai Tram y te xa Trung Chinh", sent[0]["body"]); self.assertIn("05/12", sent[0]["body"])
        self.assertTrue(all(m["contact_id"] for m in sent))
        # gửi lại đợt này cho cùng danh bạ: không ai nhận lần 2
        h = self.post(step="1", kind="mobile", aud="contacts", event_id=str(eid), body="").get_data(as_text=True)
        self.assertIn("đã nhận tin của đợt khám này", h); self.assertEqual(self.ids(h), [])             # cả 2 ô bị khoá, không ai tích được
        # đã có chiến dịch → không xoá được đợt
        self.assertFalse(SMS.delete_event(eid))

    def test_mobile_requires_event(self):
        h = self.post(step="1", kind="mobile", aud="contacts", body="").get_data(as_text=True)
        self.assertIn("Chọn đợt khám lưu động", h)

    def test_bad_template_is_caught_at_step_1(self):
        h = self.post(step="1", kind="appointment", body="Xin chao {ho_ten}").get_data(as_text=True)
        self.assertIn("Biến không hợp lệ", h)


if __name__ == "__main__":
    unittest.main()
