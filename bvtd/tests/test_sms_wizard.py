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
        self.assertEqual(h1.count('name="kind"'), 1)                                              # loại tin do tab quyết định: chỉ 1 trường ẩn, không còn nút chọn loại
        h2 = self.post(step="recipients", kind="appointment", body="").get_data(as_text=True)
        self.assertNotIn('type="hidden" name="step"', h2)                                         # tên nút bấm không bị trường ẩn che mất

    def test_full_flow_appointment(self):
        self.enable()
        h2 = self.post(step="recipients", kind="appointment", body="").get_data(as_text=True)              # nội dung rỗng → dùng mẫu mặc định
        self.assertEqual(set(self.ids(h2)), {"p:2", "p:3", "p:4", "p:8"})                          # ngày mai, chưa khám, có SĐT; (5 không SĐT bị disable)
        self.assertIn("không có SĐT di động hợp lệ", h2)
        # bỏ tích người số 3 → chỉ gửi 2, 4 và 8
        h3 = self.post(step="send", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], sel=["p:2", "p:4", "p:8"]).get_data(as_text=True)
        self.assertIn("người nhận", h3); self.assertIn("NGUYEN VAN AN", h3); self.assertIn("450", h3.replace(".", "").replace(",", ""))
        token = re.search(r'name="token" value="(\w+)"', h3).group(1)
        base = dict(step="do_send", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], sel=["p:2", "p:4", "p:8"], token=token)
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
        h = self.post(step="do_send", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], sel=["p:2"], confirm="1", token="abc").get_data(as_text=True)
        self.assertIn("TẮT", h); self.assertEqual(self.msgs(), [])

    def test_filter_by_commune_and_saved_region(self):
        self.post(step="recipients", kind="appointment", body="")
        h = self.post(step="filter", flt="1", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], date_from=D.isoformat(), date_to=D.isoformat(),
                      communes=["Xã Trung Chính"], f_status="chua").get_data(as_text=True)
        self.assertEqual(set(self.ids(h)), {"p:2", "p:4"})
        h = self.post(step="region_save", flt="1", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], date_from=D.isoformat(), date_to=D.isoformat(),
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
        h2 = self.post(step="recipients", kind="mobile", aud="contacts", event_id=str(eid), body="").get_data(as_text=True)
        keys = self.ids(h2); self.assertEqual(len(keys), 2); self.assertTrue(all(k.startswith("c:") for k in keys))
        h2 = self.post(step="filter", flt="1", kind="mobile", aud="contacts", event_id=str(eid), body=SMS.PRESETS["mobile"], communes=["Xã Trung Chính"]).get_data(as_text=True)
        self.assertEqual(len(self.ids(h2)), 1)
        h3 = self.post(step="send", kind="mobile", aud="contacts", event_id=str(eid), body=SMS.PRESETS["mobile"], sel=keys).get_data(as_text=True)
        token = re.search(r'name="token" value="(\w+)"', h3).group(1)
        self.post(step="do_send", kind="mobile", aud="contacts", event_id=str(eid), body=SMS.PRESETS["mobile"], sel=keys, confirm="1", token=token)
        sent = self.msgs(status="sent")
        self.assertEqual(len(sent), 2)
        self.assertIn("tai Tram y te xa Trung Chinh", sent[0]["body"]); self.assertIn("05/12", sent[0]["body"])
        self.assertTrue(all(m["contact_id"] for m in sent))
        # gửi lại đợt này cho cùng danh bạ: không ai nhận lần 2
        h = self.post(step="recipients", kind="mobile", aud="contacts", event_id=str(eid), body="").get_data(as_text=True)
        self.assertIn("đã nhận tin của đợt khám này", h); self.assertEqual(self.ids(h), [])             # cả 2 ô bị khoá, không ai tích được
        # đã có chiến dịch → không xoá được đợt
        self.assertFalse(SMS.delete_event(eid))

    # ── Bố cục 2 tab con ────────────────────────────────────────────────────────────────────────────────────
    def test_two_separate_menu_items_with_correct_highlight(self):
        def active(html):
            return re.findall(r'<a href="([^"]+)" aria-current="page"', html)                       # mục menu trái đang sáng
        pages = {"/sms": "/sms", "/sms/su-kien": "/sms/su-kien", "/sms/gui?kind=appointment": "/sms", "/sms/gui?kind=mobile": "/sms/su-kien"}
        for url, want in pages.items():
            h = self.client.get(url).get_data(as_text=True)
            self.assertIn('href="/sms"', h); self.assertIn('href="/sms/su-kien"', h)                # 2 mục riêng ở menu trái, trang nào cũng thấy
            self.assertIn("Nhắc lịch hẹn khám", h); self.assertIn("Nhắc lịch khám lưu động", h)
            self.assertEqual(active(h), [want], url)                                                  # đúng 1 mục sáng, đúng mục
        a = self.client.get("/sms").get_data(as_text=True); m = self.client.get("/sms/su-kien").get_data(as_text=True)
        self.assertIn("bg-brand-50", a); self.assertIn("bg-slate-50", m)                            # dải tiêu đề phân biệt 2 mục bằng nền xanh nhạt / xám (không dùng màu cầu vồng)
        self.assertIn("Tạo đợt khám mới", m)                                                       # nút tạo đợt nằm ở mục lưu động
        self.assertIn("Sửa mẫu tin", a); self.assertNotIn("Sửa mẫu tin", m)                       # nút sửa mẫu tin nằm ở mục nhắc lịch
        eid = self._event()
        self.assertEqual(active(self.client.get(f"/sms/su-kien/{eid}").get_data(as_text=True)), ["/sms/su-kien"])
        for step_url_kind, want in (("appointment", "/sms"), ("mobile", "/sms/su-kien")):           # đang ở giữa quy trình (trang ?w=…) cũng sáng đúng mục
            r = self.client.post("/sms/gui", data=dict(csrf="tok", step="recipients", kind=step_url_kind, aud="contacts", event_id=str(eid), body=""))
            self.assertEqual(active(self.client.get(r.headers["Location"]).get_data(as_text=True)), [want])

    def test_section_tabs_are_real_pages_always_visible_and_marked(self):
        sel = lambda h: re.findall(r'<a href="([^"]+)" role="tab" aria-selected="true"', h)           # tab đang mở
        eid = self._event()
        cases = {"/sms": "/sms", "/sms/mau-tin": "/sms/mau-tin", "/sms/gui?kind=appointment": "/sms/gui?kind=appointment",
                 "/sms/su-kien": "/sms/su-kien", "/sms/su-kien/moi": "/sms/su-kien/moi", "/sms/gui?kind=mobile": "/sms/gui?kind=mobile",
                 f"/sms/su-kien/{eid}": "/sms/su-kien"}
        for url, want in cases.items():
            self.assertEqual(sel(self.client.get(url).get_data(as_text=True)), [want], url)          # đúng 1 tab được tô, đúng tab
        for url in ("/sms", "/sms/mau-tin", "/sms/gui?kind=appointment"):                            # vào soạn tin / sửa mẫu rồi, các tab vẫn còn đủ
            h = self.client.get(url).get_data(as_text=True)
            for href in ("/sms", "/sms/gui?kind=appointment", "/sms/mau-tin"):
                self.assertIn(f'<a href="{href}" role="tab"', h, url)
            self.assertNotIn('href="/sms/su-kien/moi" role="tab"', h)                                # tab của mục kia không lẫn sang
        for url in ("/sms/su-kien", "/sms/su-kien/moi", "/sms/gui?kind=mobile"):
            h = self.client.get(url).get_data(as_text=True)
            for href in ("/sms/su-kien", "/sms/su-kien/moi", "/sms/gui?kind=mobile"):
                self.assertIn(f'<a href="{href}" role="tab"', h, url)
        r = self.client.post("/sms/gui", data=dict(csrf="tok", step="recipients", kind="appointment", body=""))
        self.assertEqual(sel(self.client.get(r.headers["Location"]).get_data(as_text=True)), ["/sms/gui?kind=appointment"])   # giữa quy trình vẫn đúng tab

    def test_template_tab_is_its_own_page(self):
        h = self.client.get("/sms/mau-tin").get_data(as_text=True)
        self.assertIn("Đang dùng mẫu gốc", h); self.assertIn("Xem thử với bệnh nhân mẫu", h)
        self.assertNotIn("Cài đặt gửi tin", h)                                                      # không lẫn với tab tổng quan
        r = self.post("/sms/mau-tin", body="Xin chao {ho_ten} lich {ngay}").get_data(as_text=True)    # mẫu sai: hiện lại đúng nội dung vừa gõ để sửa
        self.assertIn("Biến không hợp lệ", r); self.assertIn("Xin chao {ho_ten} lich {ngay}", r)
        self.assertEqual(SMS.appointment_template(), SMS.DEFAULT_APPOINTMENT_TEMPLATE)

    def test_new_event_tab_keeps_typed_values_on_error(self):
        h = self.client.get("/sms/su-kien/moi").get_data(as_text=True)
        self.assertIn("Thông tin đợt khám", h); self.assertNotIn("Các đợt khám lưu động</h3>", h)
        r = self.post("/sms/su-kien", name="Khám thử xã A", audience="dan", event_date="2026-12-05", event_time="", location="", note="")
        self.assertEqual(r.status_code, 200); self.assertIn("Khám thử xã A", r.get_data(as_text=True))   # thiếu địa điểm → ở lại tab «Tạo đợt», không mất chữ đã gõ
        self.assertEqual(PD._TEST_POOL.raw.execute("SELECT COUNT(*) FROM sms_events").fetchone()[0], 0)

    def test_campaign_page_highlights_its_own_section(self):
        self.enable()
        eid = self._event()
        self.post(f"/sms/su-kien/{eid}", act="add", consent_source="giay", c_name=["An"], c_phone=["0912345678"], c_commune=[""], c_org=[""])
        self.post(step="do_send", kind="mobile", aud="contacts", event_id=str(eid), body=SMS.PRESETS["mobile"], sel=["c:1"], confirm="1", token="m1")
        self.post(step="do_send", kind="appointment", aud="patients", body="", sel=["p:2"], confirm="1", token="a1")
        with PD._conn() as c:
            ids = {r["kind"]: r["id"] for r in c.execute("SELECT id, kind FROM sms_campaigns").fetchall()}
        for kind, want in (("mobile", "/sms/su-kien"), ("appointment", "/sms")):
            h = self.client.get(f"/sms/chien-dich/{ids[kind]}").get_data(as_text=True)
            self.assertEqual(re.findall(r'<a href="([^"]+)" aria-current="page"', h), [want])

    def test_compose_has_no_kind_picker_and_both_audiences_tickable(self):
        h = self.client.get("/sms/gui?kind=mobile").get_data(as_text=True)
        self.assertNotIn('value="appointment"', h)                                                 # bỏ ô chọn nhắc lịch hẹn khám
        self.assertEqual(h.count('type="checkbox" name="aud"'), 2)                                 # 2 đối tượng: tích được cả 2
        self.assertIn('name="aud" value="contacts" class="mt-1" checked', h)                       # mặc định tích danh bạ
        self.assertEqual(h.count('name="kind"'), 1)
        h = self.client.get("/sms/gui?kind=appointment").get_data(as_text=True)
        self.assertNotIn('type="checkbox" name="aud"', h)                                           # nhắc lịch hẹn: không có ô chọn đối tượng

    def test_mobile_needs_at_least_one_audience(self):
        h = self.post(step="recipients", kind="mobile", event_id="", body="").get_data(as_text=True)
        self.assertIn("ít nhất 1 đối tượng", h)

    # ── Danh bạ nhập theo từng ô ────────────────────────────────────────────────────────────────────────────
    def _event(self):
        self.post("/sms/su-kien", name="Khám tại xã Trung Chính", audience="dan", event_date="2026-12-05", event_time="07:30",
                  location="Trạm y tế xã Trung Chính", note="")
        return int(PD._TEST_POOL.raw.execute("SELECT id FROM sms_events").fetchone()[0])

    def test_contacts_entered_in_separate_fields(self):
        eid = self._event()
        h = self.client.get(f"/sms/su-kien/{eid}").get_data(as_text=True)
        for f in ("c_name", "c_phone", "c_commune", "c_org"):
            self.assertIn(f'name="{f}"', h)                                                        # mỗi thông tin 1 ô riêng
        r = self.post(f"/sms/su-kien/{eid}", act="add", consent_source="giay",
                      c_name=["Nguyễn Văn An", "", "Lê C", "Trần Thị Bình"], c_phone=["0912345678", "", "123", "0987654321"],
                      c_commune=["Trung Chính", "", "", "Minh Nghĩa"], c_org=["Thôn 3", "", "", "Thôn 1"]).get_data(as_text=True)
        self.assertIn("Đã thêm 2 người", r); self.assertIn("1 dòng lỗi", r); self.assertIn("Lê C", r)   # dòng trống bị bỏ qua, SĐT sai được nêu tên
        rows = PD._TEST_POOL.raw.execute("SELECT name, phone, commune, org FROM sms_contacts ORDER BY id").fetchall()
        self.assertEqual([tuple(x) for x in rows], [("Nguyễn Văn An", "0912345678", "Xã Trung Chính", "Thôn 3"),
                                                    ("Trần Thị Bình", "0987654321", "Xã Minh Nghĩa", "Thôn 1")])
        r = self.post(f"/sms/su-kien/{eid}", act="add", consent_source="giay", c_name=[""], c_phone=[""], c_commune=[""], c_org=[""]).get_data(as_text=True)
        self.assertIn("Chưa nhập người nào", r)
        r = self.post(f"/sms/su-kien/{eid}", act="add", consent_source="", c_name=["X"], c_phone=["0911000111"], c_commune=[""], c_org=[""]).get_data(as_text=True)
        self.assertIn("hình thức đồng ý", r)                                                       # vẫn bắt buộc có đồng ý
        self.assertEqual(PD._TEST_POOL.raw.execute("SELECT COUNT(*) FROM sms_contacts").fetchone()[0], 2)

    # ── Tích CẢ 2 đối tượng cùng lúc ────────────────────────────────────────────────────────────────────────
    def test_mobile_both_audiences_together(self):
        self.enable()
        eid = self._event()
        self.post(f"/sms/su-kien/{eid}", act="add", consent_source="giay", c_name=["Nguyễn Văn An", "Trần Thị Bình", "Người Mới"],
                  c_phone=["0912345678", "0987654321", "0955555555"], c_commune=["Trung Chính", "Minh Nghĩa", ""], c_org=["", "", ""])
        h2 = self.post(step="recipients", kind="mobile", aud=["contacts", "patients"], event_id=str(eid), body="").get_data(as_text=True)
        keys = self.ids(h2)
        self.assertTrue(any(k.startswith("c:") for k in keys) and any(k.startswith("p:") for k in keys))        # 2 nhóm trong cùng 1 danh sách
        self.assertEqual(len([k for k in keys if k.startswith("c:")]), 3)
        self.assertNotIn("p:2", keys); self.assertNotIn("p:3", keys)                              # SĐT trùng danh bạ: danh bạ được ưu tiên, bệnh nhân bị loại
        self.assertIn("trùng SĐT với người trước", h2)
        self.assertIn("Danh bạ", h2); self.assertIn("Bệnh nhân", h2)
        h3 = self.post(step="send", kind="mobile", aud=["contacts", "patients"], event_id=str(eid), body=SMS.PRESETS["mobile"], sel=keys).get_data(as_text=True)
        token = re.search(r'name="token" value="(\w+)"', h3).group(1)
        self.post(step="do_send", kind="mobile", aud=["contacts", "patients"], event_id=str(eid), body=SMS.PRESETS["mobile"], sel=keys, confirm="1", token=token)
        sent = self.msgs(status="sent")
        self.assertEqual(len(sent), len(keys))
        self.assertEqual(len({m["phone"] for m in sent}), len(sent))                              # mỗi số chỉ 1 tin
        self.assertTrue(any(m["contact_id"] for m in sent) and any(m["patient_id"] for m in sent))
        # chỉ tích bệnh nhân (bỏ danh bạ) vẫn chạy
        h = self.post(step="recipients", kind="mobile", aud="patients", event_id=str(eid), body="").get_data(as_text=True)
        self.assertTrue(all(k.startswith("p:") for k in self.ids(h)))

    # ── Sửa mẫu tin nhắc lịch ───────────────────────────────────────────────────────────────────────────────
    def test_edit_appointment_template(self):
        self.enable()
        new = "BV Tam Duc nhac {ten}: lich kham {ngay}{gio_text}. LH {hotline}"
        r = self.post("/sms/mau-tin", body=new).get_data(as_text=True)
        self.assertIn("Đã lưu mẫu tin", r); self.assertIn("Đang dùng mẫu đã sửa", r)
        self.assertIn("BV Tam Duc nhac NGUYEN VAN AN: lich kham", r)                               # tin mẫu trên trang dùng mẫu mới
        self.assertEqual(SMS.appointment_template(), new)
        h2 = self.post(step="content", kind="appointment", body="", sel=["p:2"]).get_data(as_text=True)
        self.assertIn("BV Tam Duc nhac", h2)                                                       # bước soạn nội dung lấy mẫu mới
        self.post(step="do_send", kind="appointment", aud="patients", body="", sel=["p:2"], confirm="1", token="t1")
        self.assertIn("BV Tam Duc nhac", self.msgs(status="sent")[0]["body"])
        r = self.post("/sms/mau-tin", body="Xin chao {ho_ten}").get_data(as_text=True)
        self.assertIn("Biến không hợp lệ", r); self.assertEqual(SMS.appointment_template(), new)   # mẫu sai: không lưu, mẫu cũ còn nguyên
        r = self.post("/sms/mau-tin", reset="1").get_data(as_text=True)
        self.assertIn("Đã khôi phục", r); self.assertEqual(SMS.appointment_template(), SMS.DEFAULT_APPOINTMENT_TEMPLATE)

    def test_template_save_needs_admin_and_csrf(self):
        self.assertEqual(self.client.post("/sms/mau-tin", data={"body": "x {ten}"}).status_code, 400)
        A.app.config["SMS_ADMIN_EMAILS"] = "other@x.vn"
        self.assertEqual(self.post("/sms/mau-tin", body="x {ten}").status_code, 200)                # bị chuyển về trang chủ, không lưu
        self.assertEqual(SMS.appointment_template(), SMS.DEFAULT_APPOINTMENT_TEMPLATE)


    # ── Nút Back / F5 của trình duyệt: mỗi bước là trang GET, không còn «Confirm Form Resubmission» ──────────────────
    def test_steps_redirect_to_get_pages_so_browser_back_works(self):
        for step, extra in (("recipients", {"kind": "appointment", "body": ""}), ("filter", {"kind": "appointment", "flt": "1", "body": SMS.PRESETS["appointment"]})):
            r = self.client.post("/sms/gui", data=dict(extra, csrf="tok", step=step))
            self.assertEqual(r.status_code, 302)
            self.assertRegex(r.headers["Location"], r"/sms/gui\?w=[0-9a-f]{24}$")                # kết quả nằm ở URL GET
            g1 = self.client.get(r.headers["Location"]); g2 = self.client.get(r.headers["Location"])
            self.assertEqual(g1.status_code, 200)
            self.assertEqual(self.ids(g1.get_data(as_text=True)), self.ids(g2.get_data(as_text=True)))   # tải lại / Back: kết quả y hệt
        h = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn('action="/sms/gui"', h)                                                      # form POST về URL sạch, không dính ?w=
        self.assertEqual(self.client.get("/sms/gui?w=" + "0" * 24).status_code, 302)               # mã không có / hết hạn → về trang SMS
        self.assertEqual(self.client.get("/sms/gui?w=abc").status_code, 302)
        self.assertNotIn("Phiên soạn tin", self.client.get("/sms/gui?w=" + "0" * 24, follow_redirects=False).get_data(as_text=True))

    def test_send_step_is_not_replayable_by_get(self):
        self.enable()
        w = self.post(step="do_send", kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], sel=["p:2"], confirm="1", token="tk1")
        n = len(self.msgs(status="sent"))
        from werkzeug.datastructures import MultiDict
        wid = SMS.wiz_save(MultiDict([("step", "do_send"), ("kind", "appointment"), ("aud", "patients"), ("sel", "p:3"), ("confirm", "1"), ("token", "zz9")]))
        self.client.get(f"/sms/gui?w={wid}")
        self.assertEqual(len(self.msgs(status="sent")), n)                                         # mở trang không bao giờ gửi thêm tin

    def test_drafts_do_not_pollute_settings_and_expire(self):
        from werkzeug.datastructures import MultiDict
        wid = SMS.wiz_save(MultiDict([("step", "1"), ("sel", "p:2"), ("sel", "p:3")]))
        self.assertEqual(SMS.wiz_load(wid).getlist("sel"), ["p:2", "p:3"])
        self.assertFalse([k for k in SMS.get_settings() if k.startswith("wiz:")])                  # nháp không lẫn vào cài đặt
        PD._TEST_POOL.raw.execute("UPDATE sms_settings SET updated_at = '2000-01-01 00:00:00' WHERE key LIKE 'wiz:%'")
        SMS.wiz_save(MultiDict([("step", "0")]))
        self.assertIsNone(SMS.wiz_load(wid))                                                       # nháp quá 1 ngày bị dọn

    def test_region_save_and_delete_redirect_and_keep_selection(self):
        H = dict(kind="appointment", aud="patients", body=SMS.PRESETS["appointment"], date_from=D.isoformat(), date_to=D.isoformat())
        r = self.client.post("/sms/gui", data=dict(H, csrf="tok", step="region_save", flt="1", region_name="Vùng thử", communes=["Xã Trung Chính"], sel=["p:2", "p:4"]))
        self.assertEqual(r.status_code, 302)                                                       # ghi xong → trang GET, Back không lỗi
        h = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Vùng thử", h); self.assertEqual(sorted(self.ids(h)), ["p:2", "p:4"])
        with PD._conn() as c: rid = SMS.list_regions(c)[0]["id"]
        r = self.client.post("/sms/gui", data=dict(H, csrf="tok", step="region_del", flt="1", region_id=str(rid), sel=["p:2"]))
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("Vùng thử", self.client.get(r.headers["Location"]).get_data(as_text=True))

    # ── Nút lọc theo xã / vùng hiện rõ ở bước 2 ───────────────────────────────────────────────────────────────────
    def test_commune_and_region_filter_is_visible_not_collapsed(self):
        h = self.post(step="recipients", kind="appointment", body="").get_data(as_text=True)
        self.assertIn("Xã / phường &amp; vùng", h); self.assertIn("Lọc theo xã đã tích", h)
        self.assertIn("Chọn hết xã", h); self.assertIn("Bỏ chọn xã", h); self.assertIn('class="commune-cb"', h)
        self.assertNotIn("<details", h)                                                            # không còn gập lại như trước
        SMS.save_region("Bắc Nông Cống", ["Xã Trung Chính"], "t")
        h = self.post(step="recipients", kind="appointment", body="").get_data(as_text=True)
        self.assertIn("Bắc Nông Cống (1)", h); self.assertIn("Vùng đã lưu", h)

    # ── Quy trình riêng cho từng loại tin ──────────────────────────────────────────────────────────────────────
    def test_each_kind_has_its_own_steps(self):
        a1 = self.client.get("/sms/gui?kind=appointment").get_data(as_text=True)
        self.assertIn("Bước 1/3", a1); self.assertIn("Chọn người nhận tin", a1)                  # nhắc lịch: người nhận → nội dung → gửi (3 bước)
        a2 = self.post(step="content", kind="appointment", body="", sel=["p:2"]).get_data(as_text=True)
        self.assertIn("Bước 2/3", a2); self.assertIn("Soạn nội dung tin nhắn", a2)
        a3 = self.post(step="send", kind="appointment", body="", sel=["p:2"]).get_data(as_text=True)
        self.assertIn("Bước 3/3", a3); self.assertIn("Kiểm tra lần cuối", a3); self.assertIn("Gửi đồng loạt", a3)
        m1 = self.client.get("/sms/gui?kind=mobile").get_data(as_text=True)
        self.assertIn("Bước 1/4", m1); self.assertIn("Chọn đợt khám và đối tượng", m1)           # khám lưu động: đợt & đối tượng → người nhận → nội dung → gửi (4 bước)
        eid = self._event()
        m2 = self.post(step="recipients", kind="mobile", aud="contacts", event_id=str(eid), body="").get_data(as_text=True)
        self.assertIn("Bước 2/4", m2)
        m3 = self.post(step="content", kind="mobile", aud="contacts", event_id=str(eid), body="", sel=["c:1"]).get_data(as_text=True)
        self.assertIn("Bước 3/4", m3); self.assertIn("{dia_diem}", m3)                           # biến địa điểm chỉ có ở khám lưu động
        self.assertNotIn("{dia_diem}</code>", a2)
        m4 = self.post(step="send", kind="mobile", aud="contacts", event_id=str(eid), body="", sel=["c:1"]).get_data(as_text=True)
        self.assertIn("Bước 4/4", m4)
        for h in (a1, a2, a3, m1, m2, m3, m4):
            self.assertIn("aria-current=\"step\"", h)                                           # thanh tiến trình luôn đánh dấu bước hiện tại

    def test_cannot_skip_steps(self):
        h = self.post(step="content", kind="appointment", body="").get_data(as_text=True)         # chưa chọn ai mà đòi soạn tin
        self.assertIn("Chưa tích chọn ai", h); self.assertIn("Bước 1/3", h)
        h = self.post(step="send", kind="appointment", body="Xin chao {ho_ten}", sel=["p:2"]).get_data(as_text=True)   # nội dung sai mà đòi gửi
        self.assertIn("Biến không hợp lệ", h); self.assertIn("Bước 2/3", h)
        h = self.post(step="content", kind="mobile", aud="contacts", event_id="", body="", sel=["c:1"]).get_data(as_text=True)   # chưa chọn đợt
        self.assertIn("Chọn đợt khám", h); self.assertIn("Bước 1/4", h)
        self.assertEqual(self.post(step="do_send", kind="appointment", body="", sel=[], confirm="1").get_data(as_text=True).count("Chưa tích chọn ai"), 1)
        self.assertEqual(self.msgs(), [])

    def test_selection_kept_when_going_back_but_reset_when_arriving(self):
        fresh = self.post(step="recipients", kind="appointment", body="").get_data(as_text=True)
        self.assertEqual(set(self.ids(fresh)), {"p:2", "p:3", "p:4", "p:8"})                      # tới bước này lần đầu: chọn sẵn mọi người gửi được
        back = self.post(step="recipients", kind="appointment", body="", flt="1", kept="1", sel=["p:2"], date_from=D.isoformat(), date_to=D.isoformat())
        self.assertEqual(re.findall(r'name="sel" value="(p:\d+)" checked', back.get_data(as_text=True)), ["p:2"])     # quay lại từ bước sau: giữ ô đã tích
        refilter = self.post(step="filter", kind="appointment", body="", flt="1", kept="1", sel=["p:2"], date_from=D.isoformat(), date_to=D.isoformat())
        self.assertEqual(len(re.findall(r'name="sel" value="p:\d+" checked', refilter.get_data(as_text=True))), 4)    # bấm Lọc: chọn lại từ đầu

    def test_mobile_requires_event(self):
        h = self.post(step="recipients", kind="mobile", aud="contacts", body="").get_data(as_text=True)
        self.assertIn("Chọn đợt khám lưu động", h)

    def test_bad_template_is_caught_before_sending(self):
        h = self.post(step="send", kind="appointment", body="Xin chao {ho_ten}", sel=["p:2"]).get_data(as_text=True)
        self.assertIn("Biến không hợp lệ", h); self.assertIn("Bước 2/3", h)                       # bị trả về bước soạn nội dung
        self.enable()
        h = self.post(step="do_send", kind="appointment", body="Xin chao {ho_ten}", sel=["p:2"], confirm="1", token="x1").get_data(as_text=True)
        self.assertIn("Biến không hợp lệ", h); self.assertEqual(self.msgs(), [])                  # gửi thẳng cũng không lọt


if __name__ == "__main__":
    unittest.main()
