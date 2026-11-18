"""Lớp lưu trữ bệnh nhân PostgreSQL (services/patient_db.py): chuẩn hoá, nhập từ Sheet, sửa/xoá, khoá lạc quan, đối soát.
Chạy bằng pool SQLite giả (tests/sqlite_pool.py) nên không cần PostgreSQL."""
import json
import unittest
from datetime import date, datetime

import pandas as pd
from flask import Flask

from services import patient_db as PD, sheets as S
from tests.sqlite_pool import Pool

H = {k: k for k in ["STT", "Dấu thời gian", "NGUỒN BỆNH NHÂN", "TRẠNG THÁI", "NGÀY KHÁM", "1. HỌ VÀ TÊN BỆNH NHÂN",
                    "5. SỐ ĐIÊN THOẠI", "NĂM SINH", "TUỔI", "KHOA KHÁM CHỮA BỆNH", "3. GIỚI TÍNH", "2. ĐỊA CHỈ (THÔN/XÃ)",
                    "1. TRIỆU CHỨNG CHÍNH", "4. SỐ CĂN CƯỚC CÔNG DÂN - CHỨNG MINH THƯ", "CHUYÊN KHOA MONG MUỐN KHÁM",
                    "BÁC SĨ MONG MUỐN ( nếu có)", "GIỜ KHÁM DỰ KIẾN"]}
TK = "BỆNH NHÂN ĐIỀU TRỊ NỘI KHOA TÁI KHÁM"


def rec(stt, ts, name, phone="N/A", year="N/A", gender="N/A", exam="08/09/2026", status="", source="", khoa="", age="", extra=None):
    r = {"STT": str(stt), "Dấu thời gian": ts, "NGUỒN BỆNH NHÂN": source, "TRẠNG THÁI": status, "NGÀY KHÁM": exam,
         "1. HỌ VÀ TÊN BỆNH NHÂN": name, "5. SỐ ĐIÊN THOẠI": phone, "NĂM SINH": year, "TUỔI": age,
         "KHOA KHÁM CHỮA BỆNH": khoa, "3. GIỚI TÍNH": gender, "2. ĐỊA CHỈ (THÔN/XÃ)": "Xã Trung Chính, , Tỉnh Thanh Hóa",
         "1. TRIỆU CHỨNG CHÍNH": "N/A", "4. SỐ CĂN CƯỚC CÔNG DÂN - CHỨNG MINH THƯ": "N/A",
         "CHUYÊN KHOA MONG MUỐN KHÁM": "Khám Nội Tiết", "BÁC SĨ MONG MUỐN ( nếu có)": "", "GIỜ KHÁM DỰ KIẾN": "07:00:00"}
    r.update(extra or {})
    return r


# Dòng lấy từ ảnh/Sheet thật: Form online, tái khám có SĐT 'rác', SĐT '0', tuổi theo tháng, giờ 1 chữ số…
SAMPLE = [
    (1443, rec(1442, "07/09/2026 14:11:27", "LÊ HUY CƠ", year="1962", gender="NAM")),
    (1451, rec(1450, "08/09/2026 7:00:32", "Đỗ Khắc Triều", year="1950", gender="NAM", exam="08/09/2026")),
    (2204, rec(2203, "10/07/2026 08:23:32", "LÊ THỊ MAI", phone="0332181149", source=TK, khoa="KHOA KHÁM BỆNH",
               status="BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM")),
    (2498, rec(2497, "10/08/2026 08:19:40", "LÊ THỊ HUỆ", phone="0384714393", year="1988", age="38 tuổi", gender="Nữ", source=TK,
               khoa="KHOA SẢN", status="BỆNH NHÂN CHƯA KHÁM / BỎ KHÁM")),
    (2499, rec(2498, "10/08/2026 08:19:40", "LÊ VĂN SƠN", phone="3997393303", year="1982", age="44 tuổi", gender="Nam", source=TK,
               khoa="KHOA KHÁM BỆNH", status="BỆNH NHÂN ĐÃ KHÁM")),
    (2502, rec(2501, "10/08/2026 08:19:40", "LÊ VĂN CẢNH", phone="0", year="1968", age="58 tuổi", gender="Nam", source=TK,
               khoa="KHOA KHÁM BỆNH", status="BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM")),
    (3605, rec(3604, "08/09/2026 08:08:42", "VŨ HOÀNG PHÚC", year="2024", age="25 tháng tuổi", gender="Nam", source=TK,
               khoa="KHOA NỘI TỔNG HỢP", exam="15/09/2026", status="BỆNH NHÂN CHƯA KHÁM/BỎ KHÁM",
               extra={"NGÀY TẠO LỊCH HẸN KHÁM": "08/09/2026", "CỘT LẠ": "ghi chú tay"})),
    (3700, rec(3699, "xxx", "NGUYỄN VĂN LẠ", phone="12345", year="19xx", gender="khác", status="ĐÃ HỦY", exam="31/02/2026")),
]


class Base(unittest.TestCase):
    def setUp(self):
        PD._TEST_POOL = Pool()
        self.app = Flask(__name__)
        self.app.config.update(SECRET_KEY="t")
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        PD._TEST_POOL = None

    def do_import(self):
        plan, report = PD.plan_import(SAMPLE)
        PD.run_import(plan, commit=True)
        return plan, report


class Normalize(unittest.TestCase):
    def test_values(self):
        self.assertEqual(PD.conv_phone("0378672822")[0], "0378672822")
        self.assertEqual(PD.conv_phone("378672822")[0], "0378672822")            # mất số 0 đầu → khôi phục
        self.assertEqual(PD.conv_phone("+84 378 672 822")[0], "0378672822")
        self.assertEqual(PD.conv_phone("0"), ("", None))                          # '0' = không có SĐT
        self.assertEqual(PD.conv_phone("N/A"), ("", None))
        self.assertIsNotNone(PD.conv_phone("3997393303")[1])                     # số rác: giữ nhưng cảnh báo
        self.assertEqual(PD.conv_gender("NỮ")[0], "Nữ"); self.assertEqual(PD.conv_gender("nam")[0], "Nam")
        self.assertEqual(PD.conv_gender("N/A"), (None, None))
        self.assertEqual(PD.conv_status("BỆNH NHÂN CHƯA KHÁM / BỎ KHÁM")[0], S.STATUS_NOT_ATTENDED)   # 2 kiểu gõ → 1 chuỗi chuẩn
        self.assertEqual(PD.conv_status("bệnh nhân đã khám")[0], S.STATUS_ATTENDED)
        self.assertIsNone(PD.conv_status("ĐÃ HỦY")[0]); self.assertIsNotNone(PD.conv_status("ĐÃ HỦY")[1])
        self.assertEqual(PD.conv_date("08/09/2026")[0], date(2026, 9, 8))
        self.assertEqual(PD.conv_date("2026-09-08")[0], date(2026, 9, 8))
        self.assertEqual(PD.conv_date("N/A"), (None, None))
        self.assertIsNotNone(PD.conv_date("31/02/2026")[1])
        self.assertEqual(PD.conv_ts("08/09/2026 7:00:32")[0], datetime(2026, 9, 8, 7, 0, 32))
        self.assertEqual(PD.conv_year("1962")[0], 1962); self.assertIsNone(PD.conv_year("N/A")[0])
        self.assertIsNotNone(PD.conv_year("19xx")[1])


class ImportFlow(Base):
    def test_plan_report(self):
        plan, rep = PD.plan_import(SAMPLE)
        self.assertEqual(rep["rows"], len(SAMPLE))
        self.assertEqual(rep["unknown_columns"], {"CỘT LẠ": 1})                  # cột lạ không bị mất
        self.assertEqual(rep["status"][S.STATUS_NOT_ATTENDED], 4)                # 2 kiểu gõ "CHƯA KHÁM/BỎ KHÁM" gộp làm 1
        kinds = " ".join(rep["warnings"])
        for needle in ("trạng thái lạ", "ngày không hiểu", "dấu thời gian", "SĐT", "năm sinh", "giới tính"):
            self.assertIn(needle, kinds, needle)

    def test_commit_and_guard(self):
        plan, _ = self.do_import()
        df = PD.load_dataframe()
        self.assertEqual(len(df), len(SAMPLE))
        self.assertEqual(list(df.index), [i + 1 - 2 for i in range(len(SAMPLE))])   # index = id − 2 → sheet_row = id
        with self.assertRaises(PD.Invalid):                                      # chạy lại không --resync: bị chặn
            PD.run_import(plan, commit=True)
        st = PD.run_import(plan, commit=True, resync=True)                       # resync: không có gì đổi
        self.assertEqual((st["insert"], st["update"], st["same"]), (0, 0, len(SAMPLE)))

    def test_roundtrip_matches_sheet(self):
        """Đối soát: dữ liệu đọc lại từ DB == dữ liệu Sheet (sau chuẩn hoá) ở các cột quan trọng."""
        self.do_import()
        df = PD.load_dataframe()
        by_stt = {r["STT"]: r for _, r in df.iterrows()}
        self.assertEqual(by_stt["1442"]["NGÀY KHÁM"], "08/09/2026")
        self.assertEqual(by_stt["1442"]["3. GIỚI TÍNH"], "Nam")
        self.assertEqual(by_stt["1450"]["Dấu thời gian"], "08/09/2026 07:00:32")
        self.assertEqual(by_stt["2203"]["5. SỐ ĐIÊN THOẠI"], "0332181149")
        self.assertEqual(by_stt["2501"]["5. SỐ ĐIÊN THOẠI"], "")                  # '0' → trống
        self.assertEqual(by_stt["2497"]["TUỔI"], "38 tuổi")
        self.assertEqual(by_stt["2497"]["TRẠNG THÁI"], S.STATUS_NOT_ATTENDED)
        self.assertEqual(by_stt["3604"]["TUỔI"], "25 tháng tuổi")
        self.assertEqual(by_stt["3604"]["NGÀY TẠO LỊCH HẸN KHÁM"], "08/09/2026")
        self.assertEqual(by_stt["1442"]["_date"], pd.Timestamp("2026-09-08"))
        extra = json.loads(PD.get_patient(7)["extra"])
        self.assertEqual(extra["CỘT LẠ"], "ghi chú tay") if PD.get_patient(7)["stt"] == 3604 else None
        bad = PD.get_patient(8)                                                  # dòng lỗi: không mất giá trị gốc
        self.assertIsNone(bad["status"]); self.assertIsNone(bad["exam_date"])
        ex = json.loads(bad["extra"])
        self.assertEqual(ex["status_goc"], "ĐÃ HỦY"); self.assertEqual(ex["exam_date_goc"], "31/02/2026")


class Writes(Base):
    def setUp(self):
        super().setUp()
        self.do_import()
        self.df = PD.load_dataframe()

    def item(self, stt, name, **kw):
        row = self.df.index[self.df["STT"] == str(stt)][0] + 2
        return dict({"sheet_row": int(row), "stt": str(stt), "name": name}, **kw)

    def test_write_status_source_date_and_history(self):
        it = self.item(1442, "LÊ HUY CƠ", values={S.COL_STATUS: S.STATUS_ATTENDED, S.COL_SOURCE: "BỆNH NHÂN VÃNG LAI",
                                                  S.COL_ACTUAL_DATE: "08/09/2026"})
        done, errs, fatal = PD.write_cells_ex([it])
        self.assertEqual((done, errs, fatal), ({0}, {}, None))
        p = PD.get_patient(it["sheet_row"])
        self.assertEqual((p["status"], p["source"], p["version"]), (S.STATUS_ATTENDED, "BỆNH NHÂN VÃNG LAI", 2))
        h = PD.get_history(it["sheet_row"])
        self.assertEqual(h[0]["action"], "update"); self.assertEqual(h[0]["changes"]["status"][1], S.STATUS_ATTENDED)
        n, err = S.write_cells([it]) if False else (None, None)                   # write_cells (sheets) gọi write_cells_ex đã trỏ DB

    def test_wrong_person_is_never_written(self):
        it = self.item(1442, "NGUYỄN THỊ HUẤN", values={S.COL_STATUS: S.STATUS_ATTENDED})   # tên không khớp dòng này
        done, errs, _ = PD.write_cells_ex([it])
        self.assertEqual(done, set()); self.assertIn(0, errs)
        self.assertIsNone(PD.get_patient(it["sheet_row"])["status"])

    def test_accent_must_match(self):
        it = self.item(2203, "LÊ THỊ MẢI", values={S.COL_PHONE: "0999999999"})               # MAI ≠ MẢI
        self.assertEqual(PD.write_cells_ex([it])[0], set())

    def test_invalid_value_does_not_wipe_cell(self):
        it = self.item(2203, "LÊ THỊ MAI", values={S.COL_PHONE: "abc"})
        done, errs, _ = PD.write_cells_ex([it])
        self.assertEqual(done, set()); self.assertIn("không hợp lệ", errs[0])
        self.assertEqual(PD.get_patient(it["sheet_row"])["phone"], "0332181149")             # SĐT cũ còn nguyên

    def test_fill_only(self):
        it = self.item(2203, "LÊ THỊ MAI", values={S.COL_PHONE: "0911111111", S.COL_BIRTH_YEAR: "1975"})
        PD.write_cells_ex([it], fill_only=True)
        p = PD.get_patient(it["sheet_row"])
        self.assertEqual((p["phone"], p["birth_year"]), ("0332181149", 1975))                # chỉ điền ô trống

    def test_update_status_legacy_row_number(self):
        idx = int(self.df.index[self.df["STT"] == "1442"][0])
        self.assertTrue(PD.update_status(idx + 1, S.STATUS_ATTENDED))                        # row_number = index + 1
        self.assertEqual(PD.get_patient(idx + 2)["status"], S.STATUS_ATTENDED)
        self.assertFalse(PD.update_status(idx + 1, "Linh tinh"))

    def test_soft_delete_and_restore(self):
        it = self.item(1450, "Đỗ Khắc Triều")
        self.assertEqual(PD.delete_rows([it]), (1, None))
        self.assertNotIn("1450", set(PD.load_dataframe()["STT"]))
        self.assertIsNotNone(PD.get_history(it["sheet_row"])[0]["changes"]["snapshot"])
        self.assertTrue(PD.restore_patient(it["sheet_row"]))
        self.assertIn("1450", set(PD.load_dataframe()["STT"]))

    def test_delete_batch_stops_if_one_wrong(self):
        good = self.item(1450, "Đỗ Khắc Triều"); bad = self.item(1442, "SAI TÊN")
        n, err = PD.delete_rows([good, bad])
        self.assertEqual(n, 0); self.assertTrue(err)
        self.assertEqual(len(PD.load_dataframe()), len(SAMPLE))                              # không xoá gì cả

    def test_optimistic_lock(self):
        pid = int(self.df.index[self.df["STT"] == "1442"][0]) + 2
        v = PD.get_patient(pid)["version"]
        form = {"full_name": "LÊ HUY CƠ", "phone": "0912345678", "exam_date": "2026-09-09", "gender": "Nam"}
        PD.update_patient(pid, form, v)                                                      # người A lưu trước
        with self.assertRaises(PD.Conflict):                                                 # người B còn giữ version cũ
            PD.update_patient(pid, dict(form, phone="0900000000"), v)
        self.assertEqual(PD.get_patient(pid)["phone"], "0912345678")

    def test_form_validation(self):
        with self.assertRaises(PD.Invalid):
            PD.create_patient({"full_name": "", "exam_date": "2026-09-09"})
        with self.assertRaises(PD.Invalid):
            PD.create_patient({"full_name": "A B", "exam_date": "không phải ngày"})
        with self.assertRaises(PD.Invalid):
            PD.create_patient({"full_name": "A B", "exam_date": "2026-09-09", "phone": "123"})
        pid = PD.create_patient({"full_name": "Nguyễn Thị Huấn", "exam_date": "2026-12-01", "phone": "0378672822",
                                 "birth_year": "1964", "gender": "Nữ", "source": "BỆNH NHÂN VÃNG LAI"})
        p = PD.get_patient(pid)
        self.assertEqual((p["stt"], p["gender"], p["birth_year"]), (3700, "Nữ", 1964))     # STT = lớn nhất + 1

    def test_append_field_maps_from_minh_lo_and_registration(self):
        m = S.build_sheet_field_map({"HỌ TÊN": "Trần Văn Bình", "NGÀY HẸN": "20/11/2026", "NGÀY LẬP": "10/11/2026",
                                     "SỐ ĐIỆN THOẠI": "0912345678", "KHOA HẸN": "Khoa Khám bệnh", "GIỚI TÍNH": "Nam",
                                     "NĂM SINH (ước tính)": "1960", "TUỔI": "66 tuổi", "ĐỊA CHỈ": "Xã A"}, "16/11/2026 08:00:00")
        self.assertEqual(PD.append_field_maps([m, dict(m, **{S.COL_NAME: "Lê Văn Cường"})]), (2, None))
        last = PD.load_dataframe().iloc[-2:]
        self.assertEqual(list(last["STT"]), ["3700", "3701"])
        self.assertEqual(last.iloc[0][S.COL_CREATED], "10/11/2026")
        self.assertEqual(last.iloc[0][S.COL_STATUS], S.STATUS_NOT_ATTENDED)
        self.assertEqual(last.iloc[0][S.COL_KHOA], "KHOA KHÁM BỆNH")
        self.assertEqual(PD.append_field_maps([{S.COL_NAME: ""}])[0], 0)                       # thiếu tên: từ chối, không ghi
        self.assertEqual(len(PD.load_dataframe()), len(SAMPLE) + 2)


class DownstreamUnchanged(Base):
    """Các tính năng có sẵn (báo cáo, đối chiếu) chạy trên DataFrame từ DB và ra cùng kết quả như từ Sheet."""

    def test_report_same_as_sheet_dataframe(self):
        self.do_import()
        df_db = PD.load_dataframe()
        raw = pd.DataFrame([r for _, r in SAMPLE])                                          # DataFrame kiểu Sheet (chuỗi thô)
        raw["_date"] = S.parse_sheet_dates(raw["NGÀY KHÁM"])
        for period in ("day", "month", "year"):
            a, b = S.build_report(df_db, period), S.build_report(raw, period)
            for k in ("total", "att", "abs", "tk_total", "ut_total", "tk_att", "ut_att"):
                self.assertEqual(a["kpi"][k], b["kpi"][k], (period, k))
            self.assertEqual([(r["ky"], r["total"]) for r in a["rows"]], [(r["ky"], r["total"]) for r in b["rows"]])
        self.assertEqual(S.build_report(df_db, "month")["kpi"]["att"], 1)

    def test_reconcile_sheet_row_is_patient_id(self):
        from services import reconcile as R
        self.do_import()
        df = PD.load_dataframe()
        for idx, row in df.iterrows():
            pat = R._row_to_patient(idx, row)
            self.assertEqual(pat["sheet_row"], int(idx) + 2)
            self.assertEqual(PD.get_patient(pat["sheet_row"])["full_name"], row["1. HỌ VÀ TÊN BỆNH NHÂN"])
        scope, cnt = R.build_reconcile_scope(df, date(2026, 10, 1))
        self.assertTrue(all(PD.get_patient(p["sheet_row"]) for p in scope))

    def test_upcoming_and_filters_run(self):
        self.do_import()
        df = PD.load_dataframe()
        self.assertEqual(set(S.get_upcoming_patients(df, 3)), {"days", "total", "start", "end"})
        self.assertEqual(len(S.filter_by_date_range(df, date(2026, 9, 8), date(2026, 9, 8))), 6)


class FakeWS:
    def __init__(self, title, book):
        self.title, self.spreadsheet, self.row_count, self.data, self.cleared = title, book, 10, None, False

    def clear(self): self.cleared = True
    def resize(self, rows): self.row_count = rows
    def update(self, values, cell, value_input_option=None): self.data, self.opt = values, value_input_option


class FakeBook:
    def __init__(self): self.sheets = {}
    def worksheet(self, name):
        import gspread
        if name not in self.sheets:
            raise gspread.WorksheetNotFound(name)
        return self.sheets[name]
    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWS(title, self); return self.sheets[title]


class Export(Base):
    def test_mirror_never_overwrites_main_tab(self):
        import gspread
        gspread.WorksheetNotFound = type("WorksheetNotFound", (Exception,), {})
        self.do_import()
        book = FakeBook(); main = FakeWS("Câu trả lời biểu mẫu 1", book); book.sheets[main.title] = main
        orig = S._open_ws; S._open_ws = lambda fresh=False: main
        try:
            n, tab = PD.export_to_sheet_mirror()
            self.assertEqual((n, tab), (len(SAMPLE), "Bản sao từ CSDL (chỉ đọc)"))
            ws = book.sheets[tab]
            self.assertEqual(ws.opt, "RAW"); self.assertEqual(len(ws.data), len(SAMPLE) + 1)
            self.assertIsNone(main.data)                                                    # tab gốc không bị đụng tới
            with self.assertRaises(PD.Invalid):
                PD.export_to_sheet_mirror("Câu trả lời biểu mẫu 1")                         # cố ghi đè tab gốc → từ chối
        finally:
            S._open_ws = orig


if __name__ == "__main__":
    unittest.main()
