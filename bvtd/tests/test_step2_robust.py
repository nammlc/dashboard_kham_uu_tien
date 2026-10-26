"""Bước 2 / ghi Sheet: từng ca độc lập, STT trùng/trống, ít lần gọi Google, backoff 429, so sánh ô đọc lại."""
import datetime as dt
import unittest
from unittest import mock

import pandas as pd

from tests.helpers import (A, AppTestCase, FakeSheet, ApiError, sh, sheet_row, TK, VL, DA_KHAM, CHUA_KHAM)
from tests.test_reconcile_routes import result, TOK
import services.reconcile as R

V = {"NGÀY ĐK": "20/10/2026 08:19"}


class Step2Robust(AppTestCase):
    def seed(self, *results):
        R.store_save(TOK, "rec", {"results": list(results), "ran_at": "x", "n_auto": 0})
        with self.client.session_transaction() as s:
            s["rec_token"] = TOK

    def apply(self, rows, tab="tk"):
        return self.post_json("/doi-chieu/cap-nhat", {"row": [str(r) for r in rows], "confirm": "1", "tab": tab}).get_json()

    def test_one_bad_row_does_not_block_the_others(self):
        self.install_sheet(FakeSheet([sheet_row(1, "NGUYỄN THỊ TỈNH"), sheet_row(2, "LÊ THỊ HOA"), sheet_row(3, "PHẠM VĂN DŨNG")]))
        self.seed(*[result(i + 2, i + 1, n, TK, "attended_sure", V, "2026-10-20")
                    for i, n in enumerate(["NGUYỄN THỊ TỈNH", "LÊ THỊ HOA", "PHẠM VĂN DŨNG"])])
        self.sheet.grid[2][self.sheet.grid[0].index("1. HỌ VÀ TÊN BỆNH NHÂN")] = "NGƯỜI KHÁC"   # row 3 bị đổi
        self.sheet.grid[2][self.sheet.grid[0].index("STT")] = "77"
        r = self.apply([2, 3, 4])
        self.assertTrue(r["ok"] and r["count"] == 2 and r["level"] == "warning", r)
        self.assertEqual(len(r["problems"]), 1)
        self.assertIn("LÊ THỊ HOA", r["problems"][0])
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), DA_KHAM)
        self.assertEqual(self.sheet.cell(3, "TRẠNG THÁI"), CHUA_KHAM)
        self.assertEqual(self.sheet.cell(4, "TRẠNG THÁI"), DA_KHAM)
        done = {e["name"]: e["done"] for e in R.store_load(TOK, "rec")["results"]}
        self.assertEqual(done, {"NGUYỄN THỊ TỈNH": True, "LÊ THỊ HOA": False, "PHẠM VĂN DŨNG": True})

    def test_all_rows_bad_is_an_error_with_reason(self):
        self.install_sheet(FakeSheet([sheet_row(1, "KHÁC HẲN")]))
        self.seed(result(2, 1, "NGUYỄN THỊ TỈNH", TK, "attended_sure", V, "2026-10-20"))
        r = self.apply([2])
        self.assertFalse(r["ok"])
        self.assertIn("không còn là bệnh nhân", r["message"])

    def test_duplicate_stt_on_sheet_resolves_by_name(self):
        self.install_sheet(FakeSheet([sheet_row(5, "AAA BBB"), sheet_row(5, "TRẦN THỊ MAI", source=VL)]))
        self.seed(result(3, 5, "TRẦN THỊ MAI", VL, "attended_sure", V, "2026-10-20"))
        r = self.apply([3], "vl")
        self.assertTrue(r["ok"] and r["count"] == 1, r)
        self.assertEqual(self.sheet.cell(3, "TRẠNG THÁI"), DA_KHAM)
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), CHUA_KHAM)

    def test_form_registrant_with_blank_stt_and_blank_source(self):
        self.install_sheet(FakeSheet([sheet_row("", "HOÀNG VĂN NAM", source="", status="")]))
        self.seed(result(2, "", "HOÀNG VĂN NAM", "", "attended_sure", {"NGÀY ĐK": "21/10/2026 09:05"}, None))
        r = self.apply([2], "vl")
        self.assertTrue(r["ok"] and r["count"] == 1, r)
        self.assertEqual(self.sheet.cell(2, "NGUỒN BỆNH NHÂN"), VL)
        self.assertEqual(self.sheet.cell(2, "NGÀY THỰC KHÁM"), "21/10/2026")

    def test_google_call_count_does_not_grow_with_patients(self):
        n = 12
        self.install_sheet(FakeSheet([sheet_row(i, f"BỆNH NHÂN SỐ {chr(65 + i)}") for i in range(1, n + 1)]))
        self.seed(*[result(i + 1, i, f"BỆNH NHÂN SỐ {chr(65 + i)}", TK, "attended_sure", V, "2026-10-20") for i in range(1, n + 1)])
        r = self.apply(range(2, n + 2))
        self.assertTrue(r["ok"] and r["count"] == n, r)
        c = self.sheet.calls
        self.assertEqual((c["get_all_values"], c["batch_update"], c["batch_get"]), (1, 1, 1))
        self.assertEqual((c["row_values"], c["col_values"]), (0, 0))

    def test_429_backs_off_longer_than_other_errors(self):
        self.install_sheet(FakeSheet([sheet_row(1, "NGUYỄN THỊ TỈNH")]))
        self.sheet.faults["batch_update"] = [ApiError(429), ApiError(429)]
        delays = []
        with A.app.app_context(), mock.patch.object(sh.time, "sleep", side_effect=delays.append):
            self.assertEqual(sh.write_cells([{"sheet_row": 2, "stt": "1", "name": "NGUYỄN THỊ TỈNH",
                                              "values": {"TRẠNG THÁI": DA_KHAM}}]), (1, None))
        self.assertEqual(delays, [5.0, 10.0])


class SheetHelpers(AppTestCase):
    def test_to_dmy_more_formats(self):
        for given, want in [("5-10-2026", "05/10/2026"), ("5.10.2026", "05/10/2026"), ("5/10/26", "05/10/2026"), ("2026-10-05", "05/10/2026")]:
            self.assertEqual(sh.to_dmy(given), want, given)

    def test_status_key_unifies_spellings(self):
        self.assertEqual(sh.status_key("BỆNH NHÂN CHƯA KHÁM / BỎ KHÁM"), sh.status_key(" bệnh nhân chưa khám/bỏ khám "))

    def test_same_cell_tolerates_case_spaces_and_apostrophe(self):
        self.assertTrue(sh._same_cell("Bệnh nhân đã khám ", "BỆNH NHÂN ĐÃ KHÁM"))
        self.assertTrue(sh._same_cell("21/10/2026", "'21/10/2026"))
        self.assertFalse(sh._same_cell("20/10/2026", "21/10/2026"))

    def test_lap_key_dup_and_phone_conflict(self):
        df = pd.DataFrame({"1. HỌ VÀ TÊN BỆNH NHÂN": ["AN"], "5. SỐ ĐIÊN THOẠI": ["0901111111"], "STT": ["3"],
                           "_date": [pd.Timestamp("2026-10-30")], "_created": [pd.Timestamp("2026-10-21")]})
        base = {"HỌ TÊN": "An", "NGÀY HẸN": "01/11/2026", "NGÀY LẬP": "21/10/2026", "SỐ ĐIỆN THOẠI": "0901111111"}
        new, dup = sh.check_sheet_duplicates(df, [base, {**base, "SỐ ĐIỆN THOẠI": "0902222222"}, {**base, "NGÀY LẬP": "22/10/2026"}])
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["_match"], "lap")
        self.assertEqual(len(new), 2)

    def test_fill_only_keeps_existing_values(self):
        self.install_sheet(FakeSheet([sheet_row(1, "A B", created="01/10/2026"), sheet_row(2, "C D")]))
        ups = [{"sheet_row": r, "stt": str(r - 1), "name": n, "values": {"NGÀY TẠO LỊCH HẸN KHÁM": "21/10/2026"}}
               for r, n in ((2, "A B"), (3, "C D"))]
        with A.app.app_context():
            done, errs, fatal = sh.write_cells_ex(ups, raw=True, fill_only=True)
        self.assertEqual((done, errs, fatal), ({0, 1}, {}, None))
        self.assertEqual(self.sheet.cell(2, "NGÀY TẠO LỊCH HẸN KHÁM"), "01/10/2026")
        self.assertEqual(self.sheet.cell(3, "NGÀY TẠO LỊCH HẸN KHÁM"), "21/10/2026")


if __name__ == "__main__":
    unittest.main()
