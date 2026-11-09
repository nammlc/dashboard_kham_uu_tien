"""Khoảng ngày: Import chỉ nhận ngày hẹn trong 30 ngày kể từ hôm nay; Đối chiếu lấy danh sách chưa khám trong 14 ngày trước tính từ hôm nay.
Và các câu chú thích đã được gỡ khỏi giao diện."""
import datetime as dt
import io
import re
import unittest

from services import importer as IM, reconcile as R
from tests.helpers import A, AppTestCase, FakeSheet, TK, sh, sheet_row
from tests.make_xlsx import build_04_4

TODAY = dt.date(2026, 10, 21)
text = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h))
fmt = lambda d: d.strftime("%d/%m/%Y")


class SplitByWindowTests(unittest.TestCase):
    def rec(self, hen):
        return {"HỌ TÊN": "A", "NGÀY HẸN": hen}

    def test_boundaries_are_inclusive_and_cover_30_days(self):
        lo, hi = IM.import_window(TODAY)
        self.assertEqual((lo, hi), (TODAY, TODAY + dt.timedelta(days=30)))
        cases = {"20/10/2026": False, "21/10/2026": True, "20/11/2026": True, "21/11/2026": False, "5/11/2026": True, "2026-11-05": True}
        for hen, kept in cases.items():
            keep, drop = IM.split_by_window([self.rec(hen)], TODAY)
            self.assertEqual(bool(keep), kept, hen)
            self.assertEqual(bool(drop), not kept, hen)

    def test_unreadable_dates_are_kept_not_lost_silently(self):
        keep, drop = IM.split_by_window([self.rec(""), self.rec("không phải ngày"), self.rec(None), {"HỌ TÊN": "B"}], TODAY)
        self.assertEqual((len(keep), len(drop)), (4, 0))

    def test_year_end_and_leap_day(self):
        keep, _ = IM.split_by_window([self.rec("15/01/2027")], dt.date(2026, 12, 20))
        self.assertEqual(len(keep), 1)
        keep, drop = IM.split_by_window([self.rec("29/02/2028"), self.rec("01/03/2028")], dt.date(2028, 2, 20))
        self.assertEqual((len(keep), len(drop)), (2, 0))


class ImportWindowFlow(AppTestCase):
    TODAY = TODAY

    def upload(self, content):
        return self.client.post("/import", data={"excel_file": (io.BytesIO(content), "04-4.xlsx")},
                                content_type="multipart/form-data", follow_redirects=True)

    def test_import_page_shows_the_window_and_no_mapping_box(self):
        t = text(self.client.get("/import").get_data(as_text=True))
        self.assertIn("ngày hẹn từ 21/10/2026 (hôm nay) đến 20/11/2026", t)
        self.assertIn("30 ngày kể từ hôm nay", t)
        self.assertNotIn("Quy tắc mapping", t)
        self.assertNotIn("Họ tên → HỌ VÀ TÊN", t)
        self.assertIn("Đúng file cần upload", t)                                   # ô cảnh báo file 04-4 vẫn còn

    def test_out_of_window_appointments_are_dropped_reported_and_never_written(self):
        # NGUYỄN THỊ PHƯƠNG hôm qua · TRẦN VĂN BÌNH hôm nay · LÊ HOÀNG NAM +30 · PHẠM THỊ HOA +31 · ĐỖ THỊ NGA +15
        r = self.upload(build_04_4(TODAY, {1: -1, 2: 0, 3: 30, 4: 31, 5: 15}))
        t = text(r.get_data(as_text=True))
        self.assertIn("Đọc được 3 bệnh nhân", t)
        self.assertIn("Đã bỏ qua 2 lịch hẹn", t)
        self.assertIn("ngoài khoảng 21/10/2026 – 20/11/2026", t)
        self.assertIn("NGUYỄN THỊ PHƯƠNG", t)                                       # người dùng thấy ai bị bỏ
        self.assertIn("PHẠM THỊ HOA", t)
        laps = [d for d in re.findall(r'name="lap" value="([^"]+)"', r.get_data(as_text=True))]
        res = self.post_json("/import/confirm", {"lap": sorted(set(laps))}).get_json()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["count"], 3)
        self.assertEqual(set(self.sheet.names()), {"TRẦN VĂN BÌNH", "LÊ HOÀNG NAM", "ĐỖ THỊ NGA"})

    def test_nothing_in_window_explains_and_keeps_the_page_clean(self):
        r = self.upload(build_04_4(TODAY, {i: 60 for i in range(1, 6)}))
        t = text(r.get_data(as_text=True))
        self.assertIn("File có 5 lịch hẹn nhưng không có lịch nào có ngày hẹn trong khoảng 21/10/2026 – 20/11/2026", t)
        self.assertNotIn("Xem trước import", t)
        self.assertEqual(self.client.get("/import/preview").status_code, 302)       # không có file chờ xử lý
        self.assertEqual(self.sheet.names(), [])

    def test_everything_in_window_shows_no_dropped_notice(self):
        t = text(self.upload(build_04_4(TODAY)).get_data(as_text=True))
        self.assertIn("Đọc được 5 bệnh nhân", t)
        self.assertNotIn("Đã bỏ qua", t)


class ReconcileWindowTests(AppTestCase):
    TODAY = TODAY

    def test_lookback_is_14_days(self):
        self.assertEqual(R.RECONCILE_LOOKBACK_DAYS, 14)

    def test_scope_covers_exactly_the_last_14_days(self):
        rows = [sheet_row(i + 1, f"BỆNH NHÂN {n}", source=TK, hen=fmt(TODAY + dt.timedelta(days=off)))
                for i, (n, off) in enumerate((("MỚI", 0), ("14 NGÀY", -14), ("15 NGÀY", -15), ("20 NGÀY", -20), ("14 SAU", 3)))]
        self.install_sheet(FakeSheet(rows))
        with A.app.app_context():
            df = sh.load_dataframe()
        pts, _ = R.build_reconcile_scope(df, TODAY)
        names = {p["name"] for p in pts}
        self.assertIn("BỆNH NHÂN MỚI", names)
        self.assertIn("BỆNH NHÂN 14 NGÀY", names)                                   # ngày thứ 14 vẫn tính
        self.assertNotIn("BỆNH NHÂN 15 NGÀY", names)
        self.assertNotIn("BỆNH NHÂN 20 NGÀY", names)

    def test_guidance_names_the_exact_14_day_range(self):
        self.install_sheet(FakeSheet([]))
        t = text(self.client.get("/doi-chieu").get_data(as_text=True))
        self.assertIn("từ 07/10/2026 đến 21/10/2026", t)
        self.assertIn("14 ngày trước tính từ hôm nay", t)

    def test_duplicate_section_has_no_explanatory_box_but_keeps_its_button(self):
        self.install_sheet(FakeSheet([]))
        html = self.client.get("/doi-chieu").get_data(as_text=True)
        t = text(html)
        for gone in ("Quét BN đã có lịch tái khám", "xoá HẲN khỏi Google Sheet, luôn giữ dòng tái khám", "tránh đếm 1 lượt khám thành 2"):
            self.assertNotIn(gone, t)
        self.assertIn("Đối chiếu trùng", t)                                        # tiêu đề mục + nút quét vẫn còn
        self.assertIn("Quét trùng Tái khám", t)


class AccountPageTextTests(AppTestCase):
    def test_phone_caption_removed_button_kept(self):
        t = text(self.client.get("/tai-khoan").get_data(as_text=True))
        self.assertNotIn("Đổi sang điện thoại mới khi vẫn còn dùng được điện thoại cũ", t)
        self.assertNotIn("hãy đăng xuất rồi chọn", t)


if __name__ == "__main__":
    unittest.main()
