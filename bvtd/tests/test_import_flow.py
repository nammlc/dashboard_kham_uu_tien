"""Import lịch hẹn tái khám (file 04-4): kiểm tra trùng, ghi Sheet, pop-up/JSON, các tình huống lỗi."""
import datetime as dt
import io
import re
import unittest

from tests.helpers import (A, AppTestCase, FakeSheet, ReadTimeout, sh, sheet_row, TK, VL, DA_KHAM)
from tests.make_xlsx import build_04_4

TODAY = dt.date(2026, 10, 21)
LAP_TODAY, LAP_YDAY, LAP_2D = "21/10/2026", "20/10/2026", "19/10/2026"
text = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h))      # bỏ thẻ, KHÔNG chèn khoảng trắng (giữ nguyên "2/2")


class ImportFlow(AppTestCase):
    TODAY = TODAY

    def upload(self, content=None, name="04-4.xlsx"):
        return self.client.post("/import", data={"excel_file": (io.BytesIO(content or build_04_4(TODAY)), name)},
                                content_type="multipart/form-data", follow_redirects=True)

    def preview(self, *laps):
        return text(self.client.get("/import/preview", query_string={"sel": 1, "lap": list(laps)}).get_data(as_text=True))

    def confirm(self, *laps):
        return self.post_json("/import/confirm", {"lap": list(laps)}).get_json()

    # ── upload ──
    def test_upload_rejects_wrong_files(self):
        r = self.client.post("/import", data={"excel_file": (io.BytesIO(b"x"), "a.csv")},
                             content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("file .xlsx hợp lệ", r.get_data(as_text=True))
        import openpyxl
        wb = openpyxl.Workbook(); wb.active["A1"] = "abc"; buf = io.BytesIO(); wb.save(buf)
        self.assertIn("Không tìm thấy hàng tiêu đề", self.upload(buf.getvalue()).get_data(as_text=True))

    # ── kiểm tra trùng ──
    def test_duplicates_found_whatever_the_sheet_format(self):
        """Sheet đã có BN nhưng: mất số 0 đầu SĐT, tên mất dấu/khác hoa thường, ngày không đệm số 0."""
        self.install_sheet(FakeSheet([
            sheet_row(1, "Trần Van Binh", phone="912345678", hen="27/10/2026"),
            sheet_row(2, "Đỗ Thị Nga", phone="", hen="5/11/2026"),
        ]))
        self.upload()
        t = self.preview(LAP_TODAY)
        self.assertIn("Cảnh báo: có lịch hẹn đã nhập trước đó", t)
        self.assertIn("2 bệnh nhân đã có sẵn", t)           # BÌNH + NGA đã có
        self.assertIn("Còn 1 bệnh nhân mới", t)               # chỉ còn PHƯƠNG
        self.assertRegex(t, r"Đã so khớp với 2/2 dòng")       # người dùng thấy việc so khớp có thật sự đọc Sheet

    def test_dates_shown_as_serial_numbers_still_match(self):
        serial = (dt.date(2026, 10, 27) - dt.date(1899, 12, 30)).days
        self.install_sheet(FakeSheet([sheet_row(1, "TRẦN VĂN BÌNH", phone="0912345678", hen=str(serial))]))
        self.upload()
        self.assertIn("1 bệnh nhân đã có sẵn", self.preview(LAP_TODAY))

    def test_reupload_after_import_says_already_imported(self):
        self.upload()
        first = self.confirm(LAP_TODAY)
        self.assertTrue(first["ok"] and first["count"] == 3, first)       # PHƯƠNG, BÌNH, NGA lập hôm nay
        self.assertEqual(first["redirect"], "/import")
        self.assertEqual(set(first["names"]), {"ĐỖ THỊ NGA", "NGUYỄN THỊ PHƯƠNG", "TRẦN VĂN BÌNH"})
        self.upload()                                                      # upload lại đúng file đó
        t = self.preview(LAP_TODAY)
        self.assertIn("ĐÃ ĐƯỢC NHẬP trước đó", t)
        self.assertIn("Toàn bộ 3 bệnh nhân", t)
        again = self.confirm(LAP_TODAY)                                    # ép ghi → vẫn không ghi thêm
        self.assertTrue(again["ok"] and again["count"] == 0 and again["already"] == 3, again)
        self.assertEqual(len(self.sheet.names()), 3)

    def test_choosing_more_laps_only_adds_missing_people(self):
        self.upload()
        self.confirm(LAP_TODAY)
        self.upload()
        res = self.confirm(LAP_TODAY, LAP_YDAY, LAP_2D)
        self.assertEqual(res["count"], 2)                                  # NAM (lập 20/10) + HOA (lập 19/10)
        self.assertEqual(len(self.sheet.names()), 5)

    # ── an toàn khi dữ liệu cache cũ / lỗi giữa chừng ──
    def test_stale_cache_cannot_cause_double_write(self):
        self.upload()
        self.preview(LAP_TODAY)                                            # nạp cache
        self.sheet.append_rows([sheet_row("", "NGUYỄN THỊ PHƯƠNG", phone="0983123456", hen="26/10/2026")])  # worker khác ghi
        res = self.confirm(LAP_TODAY)                                      # confirm luôn đọc Sheet mới
        self.assertEqual(res["count"], 2)
        self.assertEqual(self.sheet.names().count("NGUYỄN THỊ PHƯƠNG"), 1)

    def test_preview_snapshot_expires_quickly(self):
        self.upload()
        self.preview(LAP_TODAY)
        self.sheet.append_rows([sheet_row("", "NGUYỄN THỊ PHƯƠNG", phone="0983123456", hen="26/10/2026")])
        A.cache.set("main_df_ts", 0)                                       # bản đọc đã quá 20s
        self.assertIn("1 bệnh nhân đã có sẵn", self.preview(LAP_TODAY))

    def test_timeout_after_write_then_retry_never_duplicates(self):
        self.upload()
        real = self.sheet.append_rows

        def write_then_timeout(*a, **k):
            real(*a, **k)
            raise ReadTimeout("mất kết nối sau khi Google đã ghi xong")
        self.sheet.append_rows = write_then_timeout
        first = self.confirm(LAP_TODAY)
        self.assertFalse(first["ok"])
        self.assertIn("CÓ THỂ", first["message"])
        self.assertEqual(len(self.sheet.names()), 3)                       # Google đã ghi
        self.sheet.append_rows = real
        retry = self.confirm(LAP_TODAY)                                    # người dùng bấm "Thử lại"
        self.assertTrue(retry["ok"] and retry["count"] == 0 and retry["already"] == 3, retry)
        self.assertEqual(len(self.sheet.names()), 3)

    def test_partial_acknowledgement_is_reported_as_error(self):
        self.upload()
        real = self.sheet.append_rows
        self.sheet.append_rows = lambda *a, **k: (real(*a, **k), {"updates": {"updatedRows": 1}})[1]
        res = self.confirm(LAP_TODAY)
        self.assertFalse(res["ok"])
        self.assertIn("1/3", res["message"])

    def test_append_is_never_retried_automatically(self):
        self.upload()
        self.sheet.faults["append_rows"] = [ReadTimeout("t"), ReadTimeout("t")]
        res = self.confirm(LAP_TODAY)
        self.assertFalse(res["ok"])
        self.assertEqual(self.sheet.calls["append_rows"], 1)

    def test_unreadable_sheet_blocks_import(self):
        rows = [sheet_row(i, f"BN {i}", hen="không phải ngày") for i in range(1, 4)]
        self.install_sheet(FakeSheet(rows))
        self.upload()
        t = self.preview(LAP_TODAY)
        self.assertIn("Không kiểm tra được trùng", t)
        res = self.confirm(LAP_TODAY)
        self.assertFalse(res["ok"])
        self.assertIn("Không kiểm tra được trùng", res["message"])
        self.assertEqual(self.sheet.calls["append_rows"], 0)

    def test_empty_sheet_is_not_treated_as_blind(self):
        self.upload()
        res = self.confirm(LAP_TODAY)
        self.assertTrue(res["ok"] and res["count"] == 3)

    # ── giao diện / phiên ──
    def test_no_js_fallback_redirects_with_flash(self):
        self.upload()
        r = self.client.post("/import/confirm", data={"lap": [LAP_TODAY]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.sheet.names()), 3)

    def test_expired_session_on_confirm(self):
        res = self.confirm(LAP_TODAY)
        self.assertFalse(res["ok"])
        self.assertIn("hết hạn", res["message"])
        self.assertEqual(res["redirect"], "/import")

    def test_confirm_form_is_wired_for_background_submit(self):
        self.upload()
        html = self.client.get("/import/preview", query_string={"sel": 1, "lap": [LAP_TODAY]}).get_data(as_text=True)
        self.assertIn('id="rec-modal"', html)
        self.assertRegex(html, r'<form[^>]*action="/import/confirm"[^>]*class="js-rec"')
        self.assertNotIn("return confirm(", html)


if __name__ == "__main__":
    unittest.main()
