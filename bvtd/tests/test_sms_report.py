"""Báo cáo tỉ lệ đến của đợt khám lưu động: danh sách đã được nhắc → đối chiếu nhật ký khám → tỉ lệ → Excel."""
import datetime as dt
import io
import re
import time
import unittest
from unittest import mock

from tests.helpers import A, AppTestCase
from services import patient_db as PD, reconcile as R, sheets as S, sms as SMS, sms_report as SR
from tests.test_sms import Base, NOW_EVENING
from tests.test_sms_wizard import ROWS, make_df

TODAY = NOW_EVENING.date()                       # 17/11/2026
EVENT_DAY = dt.date(2026, 11, 14)                # đợt khám đã diễn ra (3 ngày tính đến: 14, 15, 16/11)
TOKEN = "ab" * 16


def visit(name, phone, d, birth="", khoa="KHOA KHÁM BỆNH"):
    return {"HỌ TÊN": name, "SỐ ĐIỆN THOẠI": phone, "NĂM SINH": birth, "NGÀY ĐK": d, "KHOA ĐK": khoa, "GIỚI TÍNH": ""}


VISITS = [
    visit("Nguyễn Văn An", "0912345678", "14/11/2026"),        # tên + SĐT, đúng ngày           → đã đến
    visit("Lê Văn Cường", "0911111111", "16/11/2026"),         # ngày thứ 3 của đợt             → đã đến (nếu tính 3 ngày)
    visit("Ngô Thị Lan", "", "15/11/2026"),                    # chỉ khớp tên (không SĐT)       → cần xác nhận
    visit("Trần Thị Bình", "0987654321", "17/11/2026"),        # ngày thứ 4: ngoài cửa sổ       → chưa đến
]


def rec(name, phone, group="Bệnh nhân", commune="Xã Trung Chính", **kw):
    return dict({"phone": phone, "name": name, "group": group, "commune": commune, "org": "", "birth_year": "", "age": "", "source": "",
                 "khoa": "", "mode": "live", "sent_at": None, "patient_id": None, "contact_id": None}, **kw)


class Logic(unittest.TestCase):
    def test_compute_and_summarize(self):
        recs = [rec("Nguyễn Văn An", "0912345678", "Danh bạ"), rec("Lê Văn Cường", "0911111111"), rec("Ngô Thị Lan", "0944444444", commune="Xã Minh Nghĩa"),
                rec("Trần Thị Bình", "0987654321", "Danh bạ", "Xã Minh Nghĩa"), rec("Người Mới", "0955555555", "Danh bạ"), rec("", "0966666666", "Danh bạ")]
        res = SR.compute(EVENT_DAY, recs, VISITS, TODAY)
        self.assertEqual([r["status"] for r in res], ["attended_sure", "attended_sure", "attended_unsure", "not_attended", "not_attended", "no_name"])
        self.assertEqual(res[0]["match_tier"], 1); self.assertEqual(res[2]["match_tier"], 3)
        sm = SR.summarize(res)
        self.assertEqual((sm["total"], sm["no_name"], sm["n"]), (6, 1, 5))                         # người thiếu họ tên không vào mẫu số
        self.assertEqual((sm["sure"], sm["unsure"], sm["not"]), (2, 1, 2))
        self.assertEqual((sm["rate"], sm["rate_max"]), (40.0, 60.0))                                 # 2/5 chắc chắn; 3/5 nếu tính cả ca cần xác nhận
        grp = {g["name"]: g for g in sm["by_group"]}
        self.assertEqual((grp["Danh bạ"]["n"], grp["Danh bạ"]["sure"]), (3, 1)); self.assertEqual(grp["Bệnh nhân"]["rate"], 50.0)
        com = {g["name"]: g for g in sm["by_commune"]}
        self.assertEqual((com["Xã Trung Chính"]["n"], com["Xã Trung Chính"]["sure"]), (3, 2)); self.assertEqual(com["Xã Minh Nghĩa"]["rate"], 0.0)

    def test_window_and_empty(self):
        res = SR.compute(EVENT_DAY, [rec("Nguyễn Văn An", "0912345678")], [visit("Nguyễn Văn An", "0912345678", "13/11/2026")], TODAY)
        self.assertEqual(res[0]["status"], "not_attended")                                           # khám TRƯỚC ngày khám lưu động: không tính
        self.assertIsNone(SR.summarize([])["rate"])                                                  # không có ai: không chia cho 0
        self.assertEqual(SR.summarize([])["total"], 0)

    def test_window_is_only_2_or_3_days_from_event_day(self):
        self.assertEqual([SR.clamp_days(x) for x in (2, 3, 1, 4, 9, None)], [2, 3, 3, 3, 3, 3])      # chỉ 2 hoặc 3; giá trị lạ → 3
        self.assertEqual(SR.window(EVENT_DAY, 2), (dt.date(2026, 11, 14), dt.date(2026, 11, 15)))
        self.assertEqual(SR.window(EVENT_DAY, 3), (dt.date(2026, 11, 14), dt.date(2026, 11, 16)))      # ngày khám là ngày thứ nhất
        recs = [rec("Lê Văn Cường", "0911111111")]
        for d, days, want in (("14/11/2026", 2, "attended_sure"), ("15/11/2026", 2, "attended_sure"), ("16/11/2026", 2, "not_attended"),
                              ("16/11/2026", 3, "attended_sure"), ("17/11/2026", 3, "not_attended"), ("13/11/2026", 3, "not_attended")):
            res = SR.compute(EVENT_DAY, recs, [visit("Lê Văn Cường", "0911111111", d)], TODAY, days)
            self.assertEqual(res[0]["status"], want, (d, days))

    def test_list_rows_have_same_layout_as_reconcile_export(self):
        res = SR.compute(EVENT_DAY, [rec("Nguyễn Văn An", "0912345678")], VISITS, TODAY)
        row = SR.list_rows(EVENT_DAY, res)[0]
        self.assertEqual(len(row), len(SR.LIST_HEADERS))
        self.assertEqual(SR.LIST_HEADERS[:3], ["STT", "Họ tên", "SĐT"])
        self.assertEqual((row[9], row[10], row[11]), ("Đã đến", "Tên + SĐT", "14/11/2026"))


class Report(AppTestCase, Base):
    def setUp(self):
        AppTestCase.setUp(self); Base.setUp(self)
        A.app.config.update(SMS_HOTLINE="02373 123 456", SMS_BRANDNAME="BvdkTamDuc", SMS_PROVIDER="sandbox", SMS_ADMIN_EMAILS="t@x.vn")
        with self.client.session_transaction() as s:
            s["csrf"] = "tok"; s["last_active"] = time.time()
        for p in (mock.patch.object(A, "_sms_kick_queue", lambda: SMS.process_queue()), mock.patch.object(A, "_get_df", lambda fresh=False, max_age=None: make_df(ROWS)),
                  mock.patch.object(S, "vn_today", lambda: TODAY)):
            p.start(); self.addCleanup(p.stop)
        SMS.set_setting("enabled", "1", "t")

    def post(self, url, **data):
        return self.client.post(url, data=dict(data, csrf="tok"), follow_redirects=True)

    def make_event(self, d=EVENT_DAY):
        self.post("/sms/su-kien", name="Khám tại xã Trung Chính", audience="dan", event_date=d.isoformat(), event_time="07:30", location="Trạm y tế", note="")
        return int(PD._TEST_POOL.raw.execute("SELECT id FROM sms_events ORDER BY id DESC").fetchone()[0])

    def send_to_everyone(self, eid):
        """Danh bạ 4 người (1 thiếu họ tên) + bệnh nhân cũ theo vùng → gửi đồng loạt (chế độ thử/sandbox)."""
        self.post(f"/sms/su-kien/{eid}", act="add", consent_source="giay", c_name=["Nguyễn Văn An", "Trần Thị Bình", "Người Mới", ""],
                  c_phone=["0912345678", "0987654321", "0955555555", "0966666666"], c_commune=["Trung Chính", "Minh Nghĩa", "Trung Chính", ""], c_org=["", "", "", ""])
        h = self.post("/sms/gui", step="recipients", kind="mobile", aud=["contacts", "patients"], event_id=str(eid), body="").get_data(as_text=True)
        keys = re.findall(r'name="sel" value="([pc]:\d+)" (?:checked)?>', h)
        self.post("/sms/gui", step="do_send", kind="mobile", aud=["contacts", "patients"], event_id=str(eid), body=SMS.PRESETS["mobile"], sel=keys, confirm="1", token=f"r{eid}")
        return keys

    def load_visit(self, records=VISITS):
        with self.client.session_transaction() as s:
            s["rec_token"] = TOKEN
        from services.reconcile import store_save
        store_save(TOKEN, "visit", {"filename": "01-1.xlsx", "records": records, "warn": "", "vmin": "14/11/2026", "vmax": "20/11/2026"})

    # ── danh sách đã được nhắc ──────────────────────────────────────────────────────────────────────────────
    def test_event_page_lists_everyone_reminded(self):
        eid = self.make_event()
        h = self.client.get(f"/sms/su-kien/{eid}").get_data(as_text=True)
        self.assertIn("Đã được nhắc (0", h); self.assertIn("Chưa có ai được nhắc", h)
        self.send_to_everyone(eid)
        h = self.client.get(f"/sms/su-kien/{eid}").get_data(as_text=True)
        self.assertIn("Đã được nhắc (8", h)                                                          # 4 danh bạ + 4 bệnh nhân (2 SĐT trùng danh bạ chỉ tính 1)
        self.assertIn("Lê Văn Cường", h); self.assertIn("Người Mới", h)                               # cả người trong danh bạ lẫn bệnh nhân cũ theo vùng
        self.assertIn("Xem tỉ lệ đến", h); self.assertIn("(thử)", h)
        with PD._conn() as c:
            people = SR.event_recipients(c, make_df(ROWS), eid)
            self.assertEqual(len(people), 8); self.assertEqual(len({p["phone"] for p in people}), 8)
            self.assertEqual(SR.event_recipients(c, make_df(ROWS), eid, real_only=True), [])           # tin gửi thử bị loại khi chỉ tính tin thật
            self.assertEqual(SR.reminded_counts(c), {eid: 8})
        cuong = next(p for p in people if p["name"] == "Lê Văn Cường")
        self.assertEqual((cuong["group"], cuong["commune"], cuong["khoa"]), ("Bệnh nhân", "Xã Trung Chính", "KHOA KHÁM BỆNH"))   # thông tin lấy từ dữ liệu bệnh nhân

    # ── trang báo cáo ───────────────────────────────────────────────────────────────────────────────────────
    def test_report_page_needs_visit_file_then_shows_rates(self):
        eid = self.make_event(); self.send_to_everyone(eid)
        h = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}").get_data(as_text=True)
        self.assertIn("Chưa có file", h); self.assertIn("Hãy tải file nhật ký khám", h); self.assertNotIn("Tỉ lệ đến</p>", h)
        self.load_visit()
        h = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}").get_data(as_text=True)
        self.assertIn("01-1.xlsx", h)
        self.assertIn("28.6%", h)                                                                     # 2 đến chắc chắn / 7 người đối chiếu
        self.assertIn("tối đa 42.9%", h)                                                              # tính cả 1 ca cần xác nhận
        self.assertIn("1 người thiếu họ tên", h)
        for t in ("Đã đến", "Có thể đã đến (cần xác nhận)", "Chưa đến", "Theo xã / phường", "Theo nhóm", "Tải báo cáo Excel"):
            self.assertIn(t, h)
        only = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}&st=attended_sure").get_data(as_text=True)   # lọc theo kết quả
        self.assertIn("Nguyễn Văn An", only); self.assertNotIn("Người Mới", only)
        none = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}&real=1").get_data(as_text=True)
        self.assertIn("chưa có người nào ở trạng thái", none)                                         # chỉ tính tin thật: các tin thử không tính

    def test_choose_2_or_3_days(self):
        eid = self.make_event(); self.send_to_everyone(eid); self.load_visit()
        h3 = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}").get_data(as_text=True)              # mặc định 3 ngày
        self.assertIn("28.6%", h3); self.assertIn("từ 14/11/2026 đến 16/11/2026", h3); self.assertRegex(h3, r'<option value="3" selected>')
        h2 = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}&days=2").get_data(as_text=True)
        self.assertIn("14.3%", h2)                                                                       # chỉ còn An (Cường đến ngày thứ 3 không được tính nữa): 1/7
        self.assertIn("từ 14/11/2026 đến 15/11/2026", h2); self.assertRegex(h2, r'<option value="2" selected>')
        self.assertIn("days=2", h2)                                                                      # số ngày được giữ khi chuyển trang / lọc / tải Excel
        self.assertIn("28.6%", self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}&days=9").get_data(as_text=True))   # giá trị lạ → 3 ngày
        ov = self.client.get("/sms/su-kien/bao-cao?days=2").get_data(as_text=True)
        self.assertIn("14.3%", ov); self.assertIn("tính trong 2 ngày", ov)                                # bảng tổng hợp mọi đợt cũng theo số ngày

    def test_provisional_while_exam_days_still_running(self):
        eid = self.make_event(dt.date(2026, 11, 16)); self.send_to_everyone(eid); self.load_visit()      # hôm nay 17/11 = ngày thứ 2 của đợt 3 ngày
        h = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}&days=3").get_data(as_text=True)
        self.assertIn("tạm tính", h); self.assertIn("16/11/2026", h)
        h = self.client.get(f"/sms/su-kien/bao-cao?event_id={eid}&days=2").get_data(as_text=True)         # 2 ngày: 16 và 17/11, hôm nay vẫn là ngày cuối → vẫn tạm tính
        self.assertIn("tạm tính", h)
        old = self.make_event(); self.send_to_everyone(old)
        self.assertNotIn("tạm tính", self.client.get(f"/sms/su-kien/bao-cao?event_id={old}").get_data(as_text=True))   # đợt đã qua hẳn: số liệu chính thức

    def test_overview_of_all_events_and_pending_event(self):
        past, future = self.make_event(), self.make_event(dt.date(2026, 12, 20))
        self.send_to_everyone(past); self.send_to_everyone(future)
        self.load_visit()
        h = self.client.get("/sms/su-kien/bao-cao").get_data(as_text=True)
        self.assertIn("Tỉ lệ đến theo từng đợt khám", h); self.assertIn("28.6%", h); self.assertIn("Chưa tới ngày khám", h)
        h = self.client.get(f"/sms/su-kien/bao-cao?event_id={future}").get_data(as_text=True)
        self.assertIn("chưa tới ngày", h)                                                             # đợt chưa diễn ra: không tính tỉ lệ

    def test_report_only_for_sms_admins_and_upload_validation(self):
        eid = self.make_event()
        r = self.post("/sms/su-kien/bao-cao/tai-file", event_id=str(eid))
        self.assertIn("chọn file .xlsx", r.get_data(as_text=True))                                    # chưa chọn file
        self.assertEqual(self.client.post("/sms/su-kien/bao-cao/tai-file", data={}).status_code, 400)  # thiếu CSRF
        A.app.config["SMS_ADMIN_EMAILS"] = "other@x.vn"
        for u in ("/sms/su-kien/bao-cao", f"/sms/su-kien/bao-cao/xlsx?event_id={eid}"):
            self.assertEqual(self.client.get(u).status_code, 302)

    # ── Excel ───────────────────────────────────────────────────────────────────────────────────────────────
    def test_excel_export_layout(self):
        from openpyxl import load_workbook
        eid = self.make_event(); self.send_to_everyone(eid)
        r = self.client.get(f"/sms/su-kien/bao-cao/xlsx?event_id={eid}", follow_redirects=True)
        self.assertIn("Chọn đợt khám và tải file nhật ký", r.get_data(as_text=True))                  # chưa có file nhật ký → không xuất
        self.load_visit()
        r2 = self.client.get(f"/sms/su-kien/bao-cao/xlsx?event_id={eid}&days=2")                          # Excel theo số ngày đã chọn
        info2 = {row[0].value: row[1].value for row in load_workbook(io.BytesIO(r2.data))["Tổng hợp"].iter_rows() if row[0].value}
        self.assertEqual(info2["Cửa sổ tính đến"], "2 ngày: từ 14/11/2026 đến 15/11/2026")
        self.assertEqual(info2["Đã đến"], 1)
        r = self.client.get(f"/sms/su-kien/bao-cao/xlsx?event_id={eid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r.headers["Content-Type"]); self.assertIn(f"bao_cao_ti_le_den_dot{eid}_", r.headers["Content-Disposition"])
        wb = load_workbook(io.BytesIO(r.data))
        self.assertEqual(wb.sheetnames, ["Tổng hợp", "Danh sách"])
        ls = wb["Danh sách"]
        self.assertEqual([c.value for c in ls[1]], SR.LIST_HEADERS)                                   # cùng bố cục cột với file đối chiếu tái khám
        rows = [[c.value for c in row] for row in ls.iter_rows(min_row=2)]
        self.assertEqual(len(rows), 8)
        by_name = {x[1]: x for x in rows}
        self.assertEqual(by_name["Nguyễn Văn An"][9], "Đã đến"); self.assertEqual(by_name["Nguyễn Văn An"][10], "Tên + SĐT")
        self.assertEqual(by_name["Nguyễn Văn An"][5], "Danh bạ"); self.assertEqual(by_name["Lê Văn Cường"][5], "Bệnh nhân")
        self.assertEqual(by_name["Ngô Thị Lan"][9], "Có thể đã đến (cần xác nhận)"); self.assertEqual(by_name["Người Mới"][9], "Chưa đến")
        self.assertTrue(any(x[9].startswith("Thiếu họ tên") for x in rows))
        sm = {row[0].value: row[1].value for row in wb["Tổng hợp"].iter_rows() if row[0].value}
        self.assertEqual((sm["Số người được nhắc"], sm["Đã đến"], sm["Chưa đến"]), (8, 2, 4))
        self.assertAlmostEqual(sm["TỈ LỆ ĐẾN (chắc chắn)"], 2 / 7, places=3)
        self.assertEqual(sm["Cửa sổ tính đến"], "3 ngày: từ 14/11/2026 đến 16/11/2026")
        self.assertEqual(wb["Tổng hợp"]["A1"].value, "BÁO CÁO TỈ LỆ ĐẾN — ĐỢT KHÁM LƯU ĐỘNG")

    # ── Tab ─────────────────────────────────────────────────────────────────────────────────────────────────
    def test_tab_order_and_report_tab(self):
        h = self.client.get("/sms/su-kien").get_data(as_text=True)
        order = [h.index(f'href="{u}" role="tab"') for u in ("/sms/su-kien/moi", "/sms/su-kien", "/sms/gui?kind=mobile", "/sms/su-kien/bao-cao")]
        self.assertEqual(order, sorted(order))                                                        # Tạo đợt khám mới đứng trước Các đợt khám
        self.assertIn("Báo cáo tỉ lệ đến", h)
        r = self.client.get("/sms/su-kien/bao-cao").get_data(as_text=True)
        self.assertEqual(re.findall(r'<a href="([^"]+)" role="tab" aria-selected="true"', r), ["/sms/su-kien/bao-cao"])
        self.assertEqual(re.findall(r'<a href="([^"]+)" aria-current="page"', r), ["/sms/su-kien"])  # menu trái vẫn sáng mục khám lưu động


if __name__ == "__main__":
    unittest.main()
