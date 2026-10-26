"""Cột NGÀY TẠO LỊCH HẸN KHÁM: cảnh báo trùng theo ngày lập, điền bổ sung dòng cũ, chẩn đoán Sheet.
Kèm kiểm tra các lỗi người dùng gặp: ngày đã import mà vẫn báo "chưa", không hiện thông báo "đã có"."""
import datetime as dt
import io
import re
import unittest

from tests.helpers import (A, AppTestCase, FakeSheet, HEADERS, sh, sheet_row, TK, DA_KHAM)
from tests.make_xlsx import build_04_4

TODAY = dt.date(2026, 10, 21)
LAP_TODAY, LAP_YDAY, LAP_2D = "21/10/2026", "20/10/2026", "19/10/2026"
CREATED = "NGÀY TẠO LỊCH HẸN KHÁM"
text = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h))


class _Base(AppTestCase):
    TODAY = TODAY

    def upload(self, content=None):
        return self.client.post("/import", data={"excel_file": (io.BytesIO(content or build_04_4(TODAY)), "04-4.xlsx")},
                                content_type="multipart/form-data", follow_redirects=True)

    def preview_html(self, *laps):
        return self.client.get("/import/preview", query_string={"sel": 1, "lap": list(laps)}).get_data(as_text=True)

    def preview(self, *laps):
        return text(self.preview_html(*laps))

    def confirm(self, *laps):
        return self.post_json("/import/confirm", {"lap": list(laps)}).get_json()

    def seed_import(self, records):
        from services.importer import save_import
        tok = "d" * 32
        save_import(tok, records, "x.xlsx")
        with self.client.session_transaction() as s:
            s["import_token"] = tok


def rec(name, hen, lap, phone="N/A"):
    return {"HỌ TÊN": name, "NGÀY HẸN": hen, "NGÀY LẬP": lap, "SỐ ĐIỆN THOẠI": phone, "KHOA HẸN": "Khoa Nội",
            "TUỔI": "50 tuổi", "NĂM SINH (ước tính)": "1976", "GIỚI TÍNH": "Nam", "ĐỊA CHỈ": "Xã A"}


class ImportWritesCreatedDate(_Base):
    def test_import_writes_ngay_lap_into_new_column(self):
        self.upload()
        res = self.confirm(LAP_TODAY)
        self.assertTrue(res["ok"] and res["count"] == 3, res)
        for row in (2, 3, 4):
            self.assertEqual(self.sheet.cell(row, CREATED), LAP_TODAY)          # chữ dd/mm/yyyy, không có dấu ' đứng đầu
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), "BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM")

    def test_missing_column_warns_but_import_still_works(self):
        headers = [h for h in HEADERS if h != CREATED]
        self.install_sheet(FakeSheet([], headers=headers))
        self.upload()
        t = self.preview(LAP_TODAY)
        self.assertIn('chưa có cột "NGÀY TẠO LỊCH HẸN KHÁM"', t)
        res = self.confirm(LAP_TODAY)
        self.assertTrue(res["ok"] and res["count"] == 3)
        self.assertIn("chưa có cột", res["message"])
        self.assertNotIn(CREATED, self.sheet.grid[0])


class DuplicateByCreatedDate(_Base):
    def test_already_imported_lap_detected_even_if_exam_date_was_edited(self):
        """Lịch hẹn đã nhập (ngày tạo = 21/10) nhưng nhân viên đã sửa NGÀY KHÁM trên Sheet → vẫn phải báo 'đã có'."""
        self.install_sheet(FakeSheet([sheet_row(1, "Nguyễn Thị Phương", phone="0983123456", hen="30/10/2026", created=LAP_TODAY)]))
        self.upload()
        t = self.preview(LAP_TODAY)
        self.assertIn("Cảnh báo: có lịch hẹn đã nhập trước đó", t)
        self.assertIn("1 bệnh nhân đã có sẵn", t)
        self.assertIn("Còn 2 bệnh nhân mới", t)
        self.assertIn("Ngày tạo lịch hẹn", t)                    # cột "Khớp theo" trong bảng chi tiết
        res = self.confirm(LAP_TODAY)
        self.assertEqual(res["count"], 2)
        self.assertEqual(self.sheet.names().count("Nguyễn Thị Phương"), 1)
        self.assertNotIn("NGUYỄN THỊ PHƯƠNG", self.sheet.names())

    def test_every_lap_chip_shows_how_many_are_already_on_sheet(self):
        self.install_sheet(FakeSheet([
            sheet_row(1, "NGUYỄN THỊ PHƯƠNG", phone="0983123456", hen="26/10/2026", created=LAP_TODAY),
            sheet_row(2, "TRẦN VĂN BÌNH", phone="0912345678", hen="27/10/2026", created=LAP_TODAY),
            sheet_row(3, "ĐỖ THỊ NGA", phone="0372984602", hen="05/11/2026", created=LAP_TODAY),
            sheet_row(4, "LÊ HOÀNG NAM", phone="", hen="26/10/2026", created=LAP_YDAY),
        ]))
        self.upload()
        html = self.preview_html()                               # CHƯA tích ngày nào: chip vẫn phải cho biết tình trạng
        t = text(html)
        self.assertRegex(t, r"📅 21/10/2026 \(hôm nay\) · 3 BN · ✔ đã nhập đủ")
        self.assertRegex(t, r"📅 20/10/2026 · 1 BN · ✔ đã nhập đủ")
        self.assertNotRegex(t, r"📅 19/10/2026 · 1 BN · [✔◐]")        # ngày 19/10 chưa nhập
        t = self.preview(LAP_TODAY)                              # tích lại ngày đã nhập → báo rõ "ĐÃ NHẬP ĐỦ"
        self.assertIn("ĐÃ ĐƯỢC NHẬP trước đó", t)
        self.assertIn("ĐÃ NHẬP ĐỦ", t)
        self.assertIn("Toàn bộ 3 bệnh nhân", t)

    def test_partial_lap_status(self):
        self.install_sheet(FakeSheet([sheet_row(1, "ĐỖ THỊ NGA", phone="0372984602", hen="05/11/2026", created=LAP_TODAY)]))
        self.upload()
        t = self.preview(LAP_TODAY)
        self.assertRegex(t, r"đã nhập 1/3")
        self.assertIn("Đã nhập một phần", t)

    def test_same_name_same_day_but_different_phone_is_a_different_person(self):
        """Tên trùng + cùng ngày hẹn nhưng SĐT khác hẳn → 2 người khác nhau, KHÔNG được loại."""
        self.install_sheet(FakeSheet([sheet_row(1, "NGUYỄN THỊ PHƯƠNG", phone="0900000000", hen="26/10/2026")]))
        self.upload()
        t = self.preview(LAP_TODAY)
        self.assertIn("Không có bệnh nhân nào trùng", t)
        self.assertIn("cả 3 bệnh nhân đều mới", t)

    def test_legacy_rows_without_created_date_still_detected(self):
        self.install_sheet(FakeSheet([sheet_row(1, "Trần Van Binh", phone="912345678", hen="27/10/2026")]))
        self.upload()
        self.assertIn("1 bệnh nhân đã có sẵn", self.preview(LAP_TODAY))

    def test_unreadable_exam_dates_on_sheet_are_reported(self):
        self.install_sheet(FakeSheet([sheet_row(1, "AI ĐÓ", hen="ngày lạ"),
                                      sheet_row(2, "NGUYỄN THỊ PHƯƠNG", phone="0983123456", hen="26/10/2026")]))
        self.upload()
        t = self.preview(LAP_TODAY)
        self.assertIn("NGÀY KHÁM không đọc được", t)
        self.assertIn("«ngày lạ»", t)

    def test_two_digit_year_dates_on_sheet_are_understood(self):
        self.install_sheet(FakeSheet([sheet_row(1, "TRẦN VĂN BÌNH", phone="0912345678", hen="27/10/26")]))
        self.upload()
        self.assertIn("1 bệnh nhân đã có sẵn", self.preview(LAP_TODAY))

    def test_rows_duplicated_inside_the_file_are_written_once(self):
        self.seed_import([rec("LÊ VĂN AN", "30/10/2026", LAP_TODAY, "0901111111"),
                          rec("Lê Văn An", "30/10/2026", LAP_TODAY, "0901111111"),
                          rec("LÊ VĂN AN", "30/10/2026", LAP_TODAY, "0902222222"),     # SĐT khác → người khác
                          rec("PHẠM THỊ BÍCH", "30/10/2026", LAP_TODAY)])
        t = self.preview(LAP_TODAY)
        self.assertIn("Bỏ qua 1 dòng trùng ngay trong file", t)
        res = self.confirm(LAP_TODAY)
        self.assertEqual(res["count"], 3)
        self.assertEqual(len(self.sheet.names()), 3)

    def test_dup_check_of_other_lap_is_not_affected_by_selection_order(self):
        """Cùng 1 người xuất hiện ở 2 ngày lập: chỉ chọn ngày lập sau thì KHÔNG được coi là 'trùng trong file'."""
        self.seed_import([rec("LÊ VĂN AN", "30/10/2026", LAP_YDAY, "0901111111"),
                          rec("LÊ VĂN AN", "30/10/2026", LAP_TODAY, "0901111111")])
        res = self.confirm(LAP_TODAY)
        self.assertTrue(res["ok"] and res["count"] == 1, res)


class BackfillCreatedDate(_Base):
    def setUp(self):
        super().setUp()
        self.install_sheet(FakeSheet([
            sheet_row(1, "TRẦN VĂN BÌNH", phone="0912345678", hen="27/10/2026"),                       # row 2 — dòng cũ
            sheet_row(2, "ĐỖ THỊ NGA", phone="", hen="05/11/2026"),                                   # row 3 — dòng cũ
            sheet_row(3, "NGUYỄN THỊ PHƯƠNG", phone="0983123456", hen="26/10/2026", created="01/10/2026"),  # row 4 — đã có ngày tạo khác
        ]))
        self.upload()

    def test_backfill_offer_and_write(self):
        html = self.preview_html(LAP_TODAY)
        self.assertIn('id="backfill-box"', html)
        self.assertIn("Điền ngày tạo cho 2 dòng", text(html))
        r = self.post_json("/import/backfill", {"lap": [LAP_TODAY]}).get_json()
        self.assertTrue(r["ok"] and r["count"] == 2, r)
        self.assertEqual(self.sheet.cell(2, CREATED), LAP_TODAY)
        self.assertEqual(self.sheet.cell(3, CREATED), LAP_TODAY)
        self.assertEqual(self.sheet.cell(4, CREATED), "01/10/2026", "ô đã có dữ liệu không được bị đè")
        self.assertNotIn('id="backfill-box"', self.preview_html(LAP_TODAY))
        again = self.post_json("/import/backfill", {"lap": [LAP_TODAY]}).get_json()
        self.assertTrue(again["ok"] and again["count"] == 0)

    def test_backfill_never_overwrites_a_cell_filled_meanwhile(self):
        self.preview_html(LAP_TODAY)
        self.sheet.grid[1][self.sheet.grid[0].index(CREATED)] = "15/10/2026"       # người khác vừa điền
        r = self.post_json("/import/backfill", {"lap": [LAP_TODAY]}).get_json()
        self.assertEqual(self.sheet.cell(2, CREATED), "15/10/2026")
        self.assertTrue(r["ok"])

    def test_backfill_needs_the_column(self):
        self.install_sheet(FakeSheet([], headers=[h for h in HEADERS if h != CREATED]))
        r = self.post_json("/import/backfill", {"lap": [LAP_TODAY]}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("chưa có cột", r["message"])


class StatusFilter(AppTestCase):
    def test_both_spellings_of_not_attended_are_one_status(self):
        self.install_sheet(FakeSheet([
            sheet_row(1, "A", hen="21/10/2026", status="BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM"),
            sheet_row(2, "B", hen="21/10/2026", status="BỆNH NHÂN CHƯA KHÁM / BỎ KHÁM"),
            sheet_row(3, "C", hen="21/10/2026", status=DA_KHAM)]))
        for q in ("BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM", "BỆNH NHÂN CHƯA KHÁM / BỎ KHÁM"):
            html = self.client.get("/benh-nhan", query_string={"status": q}).get_data(as_text=True)
            self.assertIn("2 bệnh nhân", text(html), q)


if __name__ == "__main__":
    unittest.main()
