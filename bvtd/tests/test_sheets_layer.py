"""Tầng Google Sheets: ghi có thử lại / đọc lại xác nhận / ngân sách thời gian, đọc ngày linh hoạt, khoá trùng."""
import datetime as dt
import unittest

import pandas as pd

from tests.helpers import (A, AppTestCase, FakeClient, FakeSheet, ApiError, ReadTimeout, sh, sheet_row,
                           TK, DA_KHAM, CHUA_KHAM)

U = lambda stt, name, row, **vals: {"sheet_row": row, "stt": str(stt), "name": name,
                                    "values": vals or {"TRẠNG THÁI": DA_KHAM, "NGUỒN BỆNH NHÂN": "BỆNH NHÂN VÃNG LAI"}}


class WriteCells(AppTestCase):
    def setUp(self):
        super().setUp()
        self.install_sheet(FakeSheet([sheet_row(10, "NGUYỄN THỊ TỈNH", hen="21/10/2026"),
                                      sheet_row(11, "TRẦN VĂN BÌNH", hen="21/10/2026")]))
        self.ctx = A.app.app_context(); self.ctx.push(); self.addCleanup(self.ctx.pop)

    def test_writes_and_reuses_worksheet(self):
        self.assertEqual(sh.write_cells([U(10, "NGUYỄN THỊ TỈNH", 2)]), (1, None))
        self.assertEqual(sh.write_cells([U(11, "TRẦN VĂN BÌNH", 3)]), (1, None))
        self.assertEqual(self.sheet.cell(2, "TRẠNG THÁI"), DA_KHAM)
        self.assertEqual(self.sheet.cell(3, "NGUỒN BỆNH NHÂN"), "BỆNH NHÂN VÃNG LAI")
        self.assertEqual(FakeClient.opened, 1, "2 thao tác chỉ mở Sheet 1 lần")

    def test_unknown_columns_are_skipped(self):
        n, err = sh.write_cells([U(10, "NGUYỄN THỊ TỈNH", 2, **{"TRẠNG THÁI": DA_KHAM, "CỘT KHÔNG CÓ": "x"})])
        self.assertEqual((n, err), (1, None))

    def test_retries_transient_errors(self):
        self.sheet.faults["batch_update"] = [ApiError(429), ApiError(503)]
        self.assertEqual(sh.write_cells([U(10, "NGUYỄN THỊ TỈNH", 2)]), (1, None))
        self.assertEqual(self.sheet.calls["batch_update"], 3)

    def test_gives_up_with_friendly_messages(self):
        cases = [([ApiError(429)] * 5, "giới hạn", 3), ([ReadTimeout("t")] * 5, "quá chậm", 3), ([ApiError(403)] * 5, "Editor", 1)]
        for faults, needle, calls in cases:
            self.install_sheet(FakeSheet([sheet_row(10, "NGUYỄN THỊ TỈNH", hen="21/10/2026")]))
            self.sheet.faults["batch_update"] = list(faults)
            n, err = sh.write_cells([U(10, "NGUYỄN THỊ TỈNH", 2)])
            self.assertEqual(n, 0)
            self.assertIn(needle, err)
            self.assertEqual(self.sheet.calls["batch_update"], calls, needle)

    def test_read_back_detects_ignored_writes(self):
        self.sheet.drop_writes = True
        n, err = sh.write_cells([U(10, "NGUYỄN THỊ TỈNH", 2)])
        self.assertEqual(n, 0)
        self.assertIn("đọc lại thấy", err)

    def test_failed_read_back_is_not_an_error(self):
        self.sheet.fail_reads_after_write = True
        self.assertEqual(sh.write_cells([U(10, "NGUYỄN THỊ TỈNH", 2)]), (1, None))

    def test_wrong_person_on_row_is_refused(self):
        n, err = sh.write_cells([U(11, "NGUYỄN THỊ TỈNH", 3)])             # STT 11 là TRẦN VĂN BÌNH
        self.assertEqual(n, 0)
        self.assertIn("không còn là bệnh nhân", err)
        self.assertEqual(self.sheet.calls["batch_update"], 0)

    def test_time_budget_stops_slow_operations(self):
        old = sh.WRITE_BUDGET_S
        sh.WRITE_BUDGET_S = -1                                              # hết ngân sách ngay
        self.addCleanup(setattr, sh, "WRITE_BUDGET_S", old)
        n, err = sh.write_cells([U(10, "NGUYỄN THỊ TỈNH", 2)])
        self.assertEqual(n, 0)
        self.assertIn("quá chậm", err)
        self.assertEqual(self.sheet.calls["batch_update"], 0)

    def test_delete_rows_is_never_retried(self):
        self.sheet.faults["batch_update"] = [ApiError(503), ApiError(503)]
        n, err = sh.delete_rows([{"sheet_row": 2, "stt": "10", "name": "NGUYỄN THỊ TỈNH"}])
        self.assertEqual(n, 0)
        self.assertEqual(self.sheet.calls["batch_update"], 1)
        self.assertEqual(len(self.sheet.names()), 2)


class DatesAndDuplicates(AppTestCase):
    def test_to_dmy(self):
        f = sh.to_dmy
        for given, want in [(dt.date(2026, 10, 5), "05/10/2026"), ("2026-10-20", "20/10/2026"), ("20/10/2026 08:19", "20/10/2026"),
                            ("5/10/2026", "05/10/2026"), (pd.Timestamp("2026-10-20"), "20/10/2026"), ("", ""), (None, ""), ("abc", "")]:
            self.assertEqual(f(given), want, given)

    def test_parse_sheet_dates_is_lenient_but_keeps_dd_mm_yyyy(self):
        serial = str((dt.date(2026, 10, 27) - dt.date(1899, 12, 30)).days)
        out = sh.parse_sheet_dates(pd.Series(["05/10/2026", "5/10/2026", "05/10/2026 08:00:00", "2026-10-05", serial, "", "abc"]))
        got = [None if pd.isna(v) else v.strftime("%d/%m/%Y") for v in out]
        self.assertEqual(got, ["05/10/2026", "05/10/2026", "05/10/2026", "05/10/2026", "27/10/2026", None, None])

    def test_load_dataframe_keeps_sheet_row_mapping(self):
        self.install_sheet(FakeSheet([sheet_row(1, "A", hen="01/10/2026"), sheet_row(2, "B", hen="bad"), sheet_row(3, "C", hen="03/10/2026")]))
        with A.app.app_context():
            df = sh.load_dataframe()
        self.assertEqual(list(df.index), [0, 1, 2])                          # index + 2 = số dòng trên Sheet
        self.assertEqual(df["_date"].isna().tolist(), [False, True, False])

    def test_duplicate_key_normalisation(self):
        df = pd.DataFrame({"1. HỌ VÀ TÊN BỆNH NHÂN": ["Trần Van Binh"], "5. SỐ ĐIÊN THOẠI": ["912345678"], "STT": ["7"],
                           "_date": [pd.Timestamp("2026-10-05")]})
        cand = [{"HỌ TÊN": "TRẦN VĂN BÌNH", "SỐ ĐIỆN THOẠI": "0912345678", "NGÀY HẸN": "5/10/2026"},
                {"HỌ TÊN": "TRẦN VĂN BÌNH", "SỐ ĐIỆN THOẠI": "0912345678", "NGÀY HẸN": "06/10/2026"},
                {"HỌ TÊN": "AI ĐÓ", "SỐ ĐIỆN THOẠI": "N/A", "NGÀY HẸN": "05/10/2026"}]
        new, dup = sh.check_sheet_duplicates(df, cand)
        self.assertEqual([r["NGÀY HẸN"] for r in dup], ["5/10/2026"])
        self.assertEqual(dup[0]["_existing_stt"], "7")
        self.assertEqual(len(new), 2)

    def test_snapshot_stats(self):
        self.assertEqual(sh.sheet_snapshot_stats(None), {"rows": 0, "valid": 0})
        df = pd.DataFrame({"1. HỌ VÀ TÊN BỆNH NHÂN": ["A", "", "C"], "_date": [pd.Timestamp("2026-10-05"), pd.Timestamp("2026-10-05"), pd.NaT]})
        self.assertEqual(sh.sheet_snapshot_stats(df), {"rows": 3, "valid": 1})


if __name__ == "__main__":
    unittest.main()
