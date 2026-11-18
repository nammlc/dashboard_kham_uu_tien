"""Khoảng ngày: Import hướng dẫn xuất file theo NGÀY VÀO VIỆN lùi 30 ngày (không loại bản ghi nào); Đối chiếu lấy danh sách chưa khám trong 14 ngày trước tính từ hôm nay.
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


class ExportRangeTests(unittest.TestCase):
    def test_range_is_the_30_days_back_from_today_inclusive(self):
        self.assertEqual(IM.EXPORT_LOOKBACK_DAYS, 30)
        self.assertEqual(IM.export_range(TODAY), (dt.date(2026, 9, 21), TODAY))
        self.assertEqual(IM.export_range(dt.date(2027, 1, 10)), (dt.date(2026, 12, 11), dt.date(2027, 1, 10)))      # qua năm
        self.assertEqual(IM.export_range(dt.date(2028, 3, 1)), (dt.date(2028, 1, 31), dt.date(2028, 3, 1)))         # năm nhuận

    def test_the_importer_has_no_record_filter_by_date_any_more(self):
        """Minh Lộ liệt kê theo NGÀY VÀO VIỆN nên ngày lập/ngày hẹn trong file có thể bất kỳ — không được loại bản ghi theo khoảng ngày."""
        self.assertFalse(hasattr(IM, "split_by_window"))


class ImportRangeGuidance(AppTestCase):
    TODAY = TODAY

    def upload(self, content):
        return self.client.post("/import", data={"excel_file": (io.BytesIO(content), "04-4.xlsx")},
                                content_type="multipart/form-data", follow_redirects=True)

    def test_import_page_tells_to_export_by_admission_date_30_days_back(self):
        t = text(self.client.get("/import").get_data(as_text=True))
        self.assertIn("ngày vào viện từ 21/09/2026 đến 21/10/2026 (hôm nay)", t)
        self.assertIn("lùi 30 ngày tính từ hôm nay", t)
        self.assertIn("không theo ngày lập lịch hẹn", t)
        self.assertNotIn("Quy tắc mapping", t)
        self.assertNotIn("Họ tên → HỌ VÀ TÊN", t)
        self.assertIn("Đúng file cần upload", t)                                          # ô cảnh báo file 04-4 vẫn còn

    def test_no_record_is_dropped_whatever_its_lap_or_appointment_date(self):
        """Lập 45 ngày trước, lập hôm nay, hẹn 90 ngày sau, hẹn hôm qua… — tất cả vẫn được đọc và chọn được theo ngày lập."""
        r = self.upload(build_04_4(TODAY, hen={1: -3, 2: 90, 3: 0, 4: 400, 5: 15}, lap={1: -45, 2: 0, 3: -31, 4: -60, 5: -1}))
        raw = r.get_data(as_text=True)
        t = text(raw)
        self.assertIn("Đọc được 5 bệnh nhân", t)
        self.assertNotIn("Đã bỏ qua", t)
        laps = set(re.findall(r'name="lap" value="([^"]+)"', raw))
        self.assertEqual(laps, {fmt(TODAY + dt.timedelta(days=d)) for d in (-45, 0, -31, -60, -1)})
        res = self.post_json("/import/confirm", {"lap": sorted(laps)}).get_json()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["count"], 5)
        self.assertEqual(len(self.sheet.names()), 5)

    def test_default_selection_is_still_todays_lap(self):
        t = text(self.upload(build_04_4(TODAY)).get_data(as_text=True))
        self.assertIn("Đọc được 5 bệnh nhân", t)


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
